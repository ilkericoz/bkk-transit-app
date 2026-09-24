"""
FastAPI serving layer for the stage-5 delay-prediction model - the sidecar
from the original staged plan (see project notes): a small, separate
model-serving service the Spring Boot backend calls over HTTP, rather than
folding ML serving into the Java app itself.

Run locally with: uvicorn main:app --reload --port 8000
(requires models/delay_model.joblib to already exist - run
scripts/train_model.py first if it doesn't.)

To reach this from another device on the same LAN (e.g. testing from a
phone), add --host 0.0.0.0 to the command above - uvicorn binds to
localhost only by default, unlike Spring Boot's embedded Tomcat which
already listens on all interfaces out of the box. Also requires a Windows
Firewall inbound rule for TCP 8000 (created 2026-08-29, scoped to the
192.168.1.0/24 home LAN subnet rather than opening it broadly - see
"BKK dev - FastAPI 8000 (LAN only)" in Windows Defender Firewall if it
ever needs recreating). Then hit http://<this-machine's-LAN-IP>:8000/docs
from the other device. The Spring Boot map (port 8080) has the same
LAN-only firewall rule and needs no code change to be reachable the
same way.
"""

import asyncio
import logging
import os
import random
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import psycopg2
import requests
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from delay_model import BaseDelayModel
from gtfs_schedule import ScheduleLookup
from weather import LiveWeather

MODEL_PATH = Path(__file__).resolve().parent / "models" / "delay_model.joblib"
BUDAPEST_TZ = ZoneInfo("Europe/Budapest")

# Same Postgres this project's ingestion pipeline writes to - this service
# never needed it before the upstream-delay feature (2026-09-12), since
# serving used to be pure file+model, no live data lookups. host is
# overridable (DB_HOST) for stage 6's sake, same idea as DELAY_SERVICE_URL
# on the Java side - a container reaches the native Postgres via
# host.docker.internal, not localhost.
DB_CONFIG = {
    "host": os.environ.get("DB_HOST", "localhost"),
    "port": int(os.environ.get("DB_PORT", "5432")),
    "dbname": "bkk_transit",
    "user": "bkk_app",
    "password": os.environ.get("DB_PASSWORD", "bkk_dev_pw"),
}

# BKK's own arrival predictions (added 2026-09-21), logged next to ours on
# every /predict/from-vehicle call so the two can be compared against the
# same real outcome later - BKK publishes no history of its predictions, so
# this data can only be collected going forward, never backfilled. Optional:
# without BKK_API_KEY in the environment the lookup is simply skipped and
# predictions still work exactly as before.
BKK_API_KEY = os.environ.get("BKK_API_KEY", "")
BKK_TRIP_DETAILS_URL = "https://futar.bkk.hu/api/query/v1/ws/otp/api/where/trip-details.json"
BKK_LOOKUP_TIMEOUT_SECONDS = 2
logger = logging.getLogger("uvicorn.error")

# Whichever candidate (baseline/linear/gbt) train_model.py's walk-forward
# comparison picked as the winner - joblib pickles the concrete class along
# with the object, so this doesn't need to know in advance which one it is.
model: BaseDelayModel | None = None

# Loaded once at startup (see lifespan below) - resolves the /predict/from-
# vehicle endpoint's scheduled_arrival from the live feed's own
# (tripId, stopSequence, serviceDate) fields, since the Java side never
# imported stop_times.txt itself (see that endpoint's docstring for why).
schedule_lookup: ScheduleLookup | None = None

# Cached current-conditions reading (see weather.py) - one instance shared
# across all requests, not per-request, so its 30-min cache actually saves
# repeated Open-Meteo calls.
live_weather = LiveWeather()


PREDICTION_LOG_DDL = """
    CREATE TABLE IF NOT EXISTS prediction_log (
        id BIGSERIAL PRIMARY KEY,
        trip_id VARCHAR(255) NOT NULL,
        stop_id VARCHAR(255) NOT NULL,
        stop_sequence INTEGER NOT NULL,
        service_date VARCHAR(255) NOT NULL,
        route_id VARCHAR(255),
        vehicle_route_type VARCHAR(255),
        predicted_delay_seconds DOUBLE PRECISION NOT NULL,
        model_type VARCHAR(255),
        predicted_at TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    CREATE INDEX IF NOT EXISTS idx_prediction_log_lookup
        ON prediction_log (trip_id, stop_id, stop_sequence, service_date);
    -- Added 2026-09-21: BKK's own predicted delay for the same trip/stop at
    -- the same moment (NULL when the lookup was unavailable). Additive and
    -- nullable on purpose, so existing rows and readers are unaffected.
    ALTER TABLE prediction_log ADD COLUMN IF NOT EXISTS bkk_predicted_delay_seconds DOUBLE PRECISION;
    ALTER TABLE prediction_log ADD COLUMN IF NOT EXISTS bkk_predicted_arrival_epoch BIGINT;
    -- Added 2026-09-21: where a prediction came from. Every existing row is a
    -- real map click, so the constant default labels them correctly (a
    -- metadata-only change in Postgres, instant on any table size). Lets a
    -- future automatic sampling job (more comparison data) be kept apart from
    -- the click-based scoreboard - see /scoreboard's WHERE clause.
    ALTER TABLE prediction_log ADD COLUMN IF NOT EXISTS source VARCHAR(16) NOT NULL DEFAULT 'click';
"""

# vehicle_position_snapshots is Java's table (Hibernate ddl-auto=update owns
# its columns), but /scoreboard's join is this service's own query, so the
# index it needs to not time out is provisioned from here rather than
# reaching into the Java side's schema management. Only ever grew slow once
# the table crossed ~100M rows (idx_snapshot_trip_id alone left too much for
# Postgres to filter row-by-row after the index scan) - IF NOT EXISTS makes
# this a no-op on an already-patched DB; on a fresh one the table starts
# empty so a plain (non-CONCURRENTLY) build here is instant either way.
SCOREBOARD_INDEX_DDL = """
    CREATE INDEX IF NOT EXISTS idx_snapshot_scoreboard_join
        ON vehicle_position_snapshots (trip_id, stop_id, stop_sequence, service_date, recorded_at)
        WHERE status = 'STOPPED_AT' AND stop_distance_percent = 100;
"""


@asynccontextmanager
async def lifespan(app: FastAPI):
    global model, schedule_lookup
    if not MODEL_PATH.exists():
        raise RuntimeError(f"No trained model at {MODEL_PATH} - run scripts/train_model.py first.")
    model = BaseDelayModel.load(MODEL_PATH)
    schedule_lookup = ScheduleLookup()

    # Self-provisioning rather than a manual migration step - this table is
    # this service's own concern (Java's Hibernate ddl-auto=update doesn't
    # know about it, and shouldn't need to), so the sidecar creates it
    # itself on startup if it isn't already there.
    with psycopg2.connect(**DB_CONFIG) as conn, conn.cursor() as cur:
        cur.execute(PREDICTION_LOG_DDL)
        cur.execute(SCOREBOARD_INDEX_DDL)
        conn.commit()

    sampler = asyncio.create_task(auto_sample_loop()) if AUTO_SAMPLE_ENABLED else None
    yield
    if sampler is not None:
        sampler.cancel()


app = FastAPI(title="BKK Delay Prediction Service", lifespan=lifespan)


class DelayPredictionRequest(BaseModel):
    route_id: str = Field(examples=["BKK_3020"])
    stop_id: str = Field(examples=["BKK_F00969"])
    vehicle_route_type: str = Field(examples=["TRAM"])
    stop_sequence: int = Field(ge=0)
    # When the vehicle is scheduled to arrive at this stop. hour/day_of_week
    # are derived from this here, rather than accepted directly, so callers
    # send real trip data instead of pre-computed model features. Naive
    # datetimes are assumed to already be Europe/Budapest civil time
    # (matching GTFS's own convention); timezone-aware ones are converted.
    scheduled_arrival: datetime
    # Optional: this endpoint has no trip_id, so it can't look the live
    # upstream delay up itself the way /predict/from-vehicle does - a
    # caller who has it (e.g. manual testing against a known scenario) can
    # pass it directly. Left unset, this is treated as "no live reading
    # available" (has_upstream_delay=False), same as a trip's first stop.
    upstream_delay_seconds: float = 0.0
    has_upstream_delay: bool = False
    # Optional, same reasoning as upstream_delay_seconds above: this
    # endpoint has no inherent "now", so it can't assume current weather is
    # right for whatever scheduled_arrival was passed in. Left unset,
    # falls back to actual current conditions (live_weather.current()) -
    # a reasonable default for a scheduled_arrival that's close to now,
    # less so for one that's deliberately hypothetical/far off.
    temperature_2m: float | None = None
    precipitation: float | None = None
    wind_speed_10m: float | None = None
    deviated: bool = False


class UpstreamDelayReading(BaseModel):
    """
    Not just a model feature - also returned to the caller (see
    DelayPredictionResponse) so the map can show "this vehicle was last
    confirmed Xs late, Y minutes ago" right next to the prediction for its
    upcoming stop. Genuine ground truth (an actual observed arrival),
    unlike the prediction itself - a good sanity-check reference point for
    someone eyeballing whether a prediction looks reasonable.
    """

    delay_seconds: float
    # None for a manually-supplied value (see /predict) - there's no real
    # "how long ago" for something the caller just typed in, unlike a live
    # lookup's actual recorded_at.
    minutes_ago: float | None = None


class DelayPredictionResponse(BaseModel):
    predicted_delay_seconds: float
    # This trip's last CONFIRMED delay (ground truth, not a prediction) -
    # None for /predict when the caller didn't supply one, or for a trip's
    # genuinely first observed stop.
    last_confirmed_delay: UpstreamDelayReading | None = None


class LiveVehiclePredictionRequest(BaseModel):
    """
    Mirrors the fields BKK's own live vehicle-position feed returns (see
    the Java side's VehiclePosition record) - the caller (Spring Boot)
    just forwards what it already has from its last /api/vehicles poll,
    rather than needing to know anything about GTFS schedules itself.
    """

    trip_id: str = Field(examples=["BKK_D19380125"])
    route_id: str = Field(examples=["BKK_3020"])
    stop_id: str = Field(examples=["BKK_F00969"])
    vehicle_route_type: str = Field(examples=["TRAM"])
    stop_sequence: int = Field(ge=0)
    service_date: str = Field(examples=["20260912"], description="GTFS serviceDate, YYYYMMDD")
    deviated: bool = False


def fetch_upstream_delay(gtfs_trip_id: str, stop_sequence: int, service_date: str) -> UpstreamDelayReading | None:
    """
    This trip's own delay at the most recent earlier stop actually observed
    today - the live-lookup equivalent of build_delay_dataset.py's batch
    upstream_delay_seconds (a groupby+shift there; a single targeted query
    here, since a live request only ever needs one trip's answer). See
    delay_model.py's NUMERIC_FEATURES comment for why this feature exists.
    None means no earlier stop has been observed *by us* today - not
    necessarily this trip's actual first stop. Confirmed via real data
    (2026-09-16): plenty of currently-tracked vehicles sit at stop_sequence
    15-25+ with zero earlier confirmed stops, most likely because the
    vehicle entered our 25km tracking radius or our tracking window
    mid-trip (a relief/substitute vehicle taking over an in-progress trip,
    or the route's earlier stops simply being outside the polled area).

    Opens a fresh connection per call rather than pooling - this endpoint
    is click-triggered from the map (see app.js), not a hot path, so the
    extra ~10-20ms of connection setup isn't worth the added complexity of
    a pool at this project's current scale.
    """
    # A trip's first stop (stop_sequence 1) is measured by its DEPARTURE -
    # the last sighting there - matching build_delay_dataset.py's label, so
    # the model gets the same upstream value live as it was trained on.
    query = """
        SELECT stop_sequence,
               CASE WHEN stop_sequence = 1 THEN MAX(recorded_at) ELSE MIN(recorded_at) END AS recorded_at
        FROM vehicle_position_snapshots
        WHERE trip_id = %s
          AND service_date = %s
          AND status = 'STOPPED_AT'
          AND stop_distance_percent = 100
          AND stop_sequence < %s
        GROUP BY stop_sequence
        ORDER BY stop_sequence DESC
        LIMIT 1
    """
    # trip_id is stored with its real-time "BKK_" prefix in Postgres - only
    # the static-schedule side (ScheduleLookup) needs it stripped.
    with psycopg2.connect(**DB_CONFIG) as conn, conn.cursor() as cur:
        cur.execute(query, (f"BKK_{gtfs_trip_id}", service_date, stop_sequence))
        row = cur.fetchone()

    if row is None:
        return None  # this trip's first observed stop - nothing upstream yet.

    found_stop_sequence, recorded_at = row
    scheduled = schedule_lookup.scheduled_arrival(gtfs_trip_id, found_stop_sequence, service_date)
    if scheduled is None:
        return None

    recorded_at = recorded_at.astimezone(BUDAPEST_TZ)
    delay_seconds = (recorded_at - scheduled).total_seconds()
    minutes_ago = (datetime.now(BUDAPEST_TZ) - recorded_at).total_seconds() / 60
    return UpstreamDelayReading(delay_seconds=delay_seconds, minutes_ago=minutes_ago)


def fetch_bkk_prediction(trip_id: str, stop_sequence: int, service_date: str) -> tuple[float, int] | None:
    """
    BKK's own predicted delay (seconds) and predicted arrival (epoch seconds)
    for this trip at this stop, right now: predictedArrivalTime minus the
    scheduled arrivalTime, both from the FUTAR trip-details endpoint (a
    ~4KB response per trip, matched on stopSequence). None on any problem
    (no key, timeout, unexpected shape, no prediction for that stop) - this
    is a bonus measurement and must never break or slow the real prediction
    beyond the short timeout. Only the exception TYPE is logged: a requests
    error message can contain the full URL, which includes the API key.
    """
    if not BKK_API_KEY:
        return None
    try:
        response = requests.get(
            BKK_TRIP_DETAILS_URL,
            params={"tripId": trip_id, "date": service_date, "includeReferences": "false", "key": BKK_API_KEY},
            timeout=BKK_LOOKUP_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        for stop_time in response.json()["data"]["entry"]["stopTimes"]:
            if stop_time.get("stopSequence") == stop_sequence:
                scheduled, predicted = stop_time.get("arrivalTime"), stop_time.get("predictedArrivalTime")
                if scheduled is None or predicted is None:
                    return None
                return float(predicted - scheduled), int(predicted)
    except Exception as exc:  # noqa: BLE001 - see docstring: never let this break a prediction
        logger.warning("BKK trip-details lookup failed (%s)", type(exc).__name__)
    return None


def log_prediction(
    trip_id: str, route_id: str, stop_id: str, vehicle_route_type: str,
    stop_sequence: int, service_date: str, predicted_delay_seconds: float,
    bkk_predicted_delay_seconds: float | None = None,
    bkk_predicted_arrival_epoch: int | None = None,
    source: str = "click",
) -> None:
    """
    Records a live prediction so /scoreboard can later reconcile it against
    what actually happened - see prediction_log's DDL above. Only called
    from /predict/from-vehicle (a real prediction against a genuine future
    stop visit), not /predict (a manual/testing call that may not even
    correspond to a real trip).
    """
    query = """
        INSERT INTO prediction_log
            (trip_id, route_id, stop_id, vehicle_route_type, stop_sequence,
             service_date, predicted_delay_seconds, model_type,
             bkk_predicted_delay_seconds, bkk_predicted_arrival_epoch, source)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """
    with psycopg2.connect(**DB_CONFIG) as conn, conn.cursor() as cur:
        cur.execute(query, (
            trip_id, route_id, stop_id, vehicle_route_type, stop_sequence,
            service_date, predicted_delay_seconds, model.name if model else None,
            bkk_predicted_delay_seconds, bkk_predicted_arrival_epoch, source,
        ))
        conn.commit()


class ScoreboardEntry(BaseModel):
    route_id: str | None
    vehicle_route_type: str | None
    predicted_delay_seconds: float
    actual_delay_seconds: float
    error_seconds: float
    predicted_at: datetime


class Scoreboard(BaseModel):
    reconciled_count: int
    mean_absolute_error_seconds: float | None
    best: list[ScoreboardEntry]


# The headline MAE is averaged over every reconciled prediction, not a
# rolling window - a rolling window shows a different subset on every poll
# as old entries fall out of it, which reads as the number "fluctuating"
# for no visible reason. An all-time average over a large, growing N barely
# moves per new sample, which is both a truer "how good is the model"
# number and a visibly calmer one. (Flagged directly: 2026-09-16, the
# rolling-window number looked misleading/noisy to a viewer.)
SCOREBOARD_BEST_DISPLAY = 5  # how many of the most-accurate reconciled predictions to surface as examples
# When first stops started being predicted and scored by DEPARTURE (the
# model retrained on departure labels went live). Earlier stop-1 predictions
# were made by a model trained on the old ARRIVAL label, so they're still
# scored against the arrival - each prediction is judged against the event
# it was actually predicting. (Last old-model prediction 13:55:49, first new
# one 13:58:38, per prediction_log.model_type.)
FIRST_STOP_DEPARTURE_SINCE = "2026-09-23 13:56:30+02:00"

MAX_ABS_RECONCILED_DELAY_SECONDS = 1500  # see the skip below - deliberately tighter than build_delay_dataset.py's 3600s


@app.get("/scoreboard", response_model=Scoreboard)
def scoreboard() -> Scoreboard:
    """
    Reconciles logged predictions against what actually happened, computed
    on read rather than via a background job - prediction_log's volume
    (a few hundred click rows plus a bounded 50-per-5-minutes auto-sampling
    job) is small enough that there's no real cost to just joining at
    request time, which is a lot simpler than maintaining a separate
    reconciliation process. DISTINCT ON picks the earliest STOPPED_AT/100%
    sighting per logged prediction, same "first poll that caught it
    arrived" definition of actual arrival used everywhere else in this
    project (see build_delay_dataset.py's fetch_actual_arrivals).

    Counts both click and auto-sampled predictions (see run_prediction's
    source argument). Originally this counted only source='click', so the
    then-brand-new auto-sampling job (2026-09-21) couldn't change what the
    live number meant on day one. That precaution is no longer needed:
    click volume stayed tiny (a few hundred rows, mostly from before the
    09-14 feature-engineering work) while auto now has thousands - a more
    representative population, not a different one, so it's the honest
    default going forward.

    Deliberately does NOT filter to rows where BKK also has a comparable
    prediction (bkk_predicted_delay_seconds IS NOT NULL) even though that
    would raise this number - measured 2026-09-22: 56.3s MAE on the ~90%
    of reconciled rows BKK could also predict vs. 155.2s on the ~10% it
    couldn't (closely matching the ~153s cold-start MAE found 2026-09-14
    on trips with no upstream-delay reading yet - the same hard subgroup,
    corroborated two different ways). Excluding them would flatter this
    panel by selecting for the easier cases; this number should reflect
    real prediction quality across everything actually served, not just
    the subset a second source happens to agree is comparable. This
    widened the gap against the offline walk-forward MAE (~44-50s in
    steady state) rather than closing it, contrary to the original
    expectation when this change was made - the click-only number (~63s)
    turned out to be closer to reality mostly by chance of a small,
    stale sample, not because it was measuring something different.

    Unlike the no-BKK-prediction rows above, predictions for a stop the
    vehicle had already reached ARE excluded (2026-09-23, see the NOT
    EXISTS in the query) - not because they're hard, but because their
    real outcome happened before the prediction and can't be measured by
    this join at all. That took the number from 66.7s to ~53s.
    """
    query = """
        SELECT DISTINCT ON (pl.id)
               pl.id, pl.trip_id, pl.route_id, pl.vehicle_route_type,
               pl.stop_sequence, pl.service_date,
               pl.predicted_delay_seconds, pl.predicted_at,
               vs.recorded_at AS actual_recorded_at
        FROM prediction_log pl
        JOIN vehicle_position_snapshots vs
          ON vs.trip_id = pl.trip_id
         AND vs.stop_id = pl.stop_id
         AND vs.stop_sequence = pl.stop_sequence
         AND vs.service_date = pl.service_date
         AND vs.status = 'STOPPED_AT'
         AND vs.stop_distance_percent = 100
         -- A trip_id + stop_sequence isn't always unique within a service_date:
         -- BKK's live feed can reuse a trip_id across more than one real dispatch
         -- of a frequent/looping route on the same day. Without this bound, the
         -- "earliest matching snapshot" could be an arrival from BEFORE the
         -- prediction was even made - an outcome the prediction couldn't have
         -- been "wrong" about, but that still counted as a (huge, nonsensical)
         -- error. Only an arrival that happened after the prediction counts as
         -- what the prediction was actually judged against.
         AND vs.recorded_at > pl.predicted_at
        -- Both real map clicks and the automatic sampler (see the docstring
        -- above for why these are no longer kept separate).
        WHERE pl.source IN ('click', 'auto')
          -- Not applied to first-stop predictions scored on DEPARTURE (see
          -- the ORDER BY and FIRST_STOP_DEPARTURE_SINCE): a vehicle already
          -- waiting there hasn't departed yet - the departure is still ahead
          -- of the prediction, so it's a fair thing to score.
          --
          -- Skip predictions for a stop the vehicle had ALREADY reached when
          -- the prediction was made. The sampler only picks IN_TRANSIT_TO
          -- vehicles, but BKK's status can flip back to IN_TRANSIT_TO while
          -- the vehicle is still standing at the stop. The real arrival then
          -- happened before predicted_at, so the join above can only find a
          -- later sighting of the same visit - scoring against a moment the
          -- vehicle was just still standing there, not its arrival. Measured
          -- 2026-09-23: 1,753 of 10,224 rows (17 percent, median 0.5 min after the
          -- real arrival, so the same visit rather than trip_id reuse); they
          -- scored 131s MAE vs. 53s for the rest, with a mean "actual" delay
          -- of +83s as scored vs. -167s at the real first arrival.
          AND ((pl.stop_sequence = 1 AND pl.predicted_at >= %(departure_since)s) OR NOT EXISTS (
              SELECT 1 FROM vehicle_position_snapshots prior
              WHERE prior.trip_id = pl.trip_id
                AND prior.stop_id = pl.stop_id
                AND prior.stop_sequence = pl.stop_sequence
                AND prior.service_date = pl.service_date
                AND prior.status = 'STOPPED_AT'
                AND prior.stop_distance_percent = 100
                AND prior.recorded_at <= pl.predicted_at
          ))
        -- Earliest sighting after the prediction = the arrival; except for a
        -- first-stop prediction made since FIRST_STOP_DEPARTURE_SINCE, where
        -- the LATEST sighting = the departure, the event first stops are now
        -- labelled by in training (build_delay_dataset.py). Otherwise the
        -- CASE is NULL on every row, so recorded_at ASC decides as before.
        ORDER BY pl.id,
                 CASE WHEN pl.stop_sequence = 1 AND pl.predicted_at >= %(departure_since)s
                      THEN vs.recorded_at END DESC NULLS LAST,
                 vs.recorded_at ASC
    """
    with psycopg2.connect(**DB_CONFIG) as conn, conn.cursor() as cur:
        cur.execute(query, {"departure_since": FIRST_STOP_DEPARTURE_SINCE})
        rows = cur.fetchall()

    entries = []
    for (_id, trip_id, route_id, vehicle_route_type, stop_sequence, service_date,
         predicted_delay_seconds, predicted_at, actual_recorded_at) in rows:
        scheduled = schedule_lookup.scheduled_arrival(trip_id.removeprefix("BKK_"), stop_sequence, service_date)
        if scheduled is None:
            continue
        actual_delay_seconds = (actual_recorded_at.astimezone(BUDAPEST_TZ) - scheduled).total_seconds()
        if abs(actual_delay_seconds) > MAX_ABS_RECONCILED_DELAY_SECONDS:
            # Same trip_id-reuse family of bug as 6b9af0a: BKK can dispatch a
            # trip_id more than once a day, or dwell at one stop for many
            # minutes straight (a terminus/layover), so "the next STOPPED_AT
            # snapshot after this prediction" can occasionally be a real but
            # unrelated visit rather than the one actually predicted against.
            # 1500s chosen from evidence, not guessed: checked the full
            # distribution of reconciled |actual_delay_seconds| across all
            # 336 rows on 2026-09-16 - p99 was 807.5s with the next-highest
            # value already at 1906.2s, a genuine gap, not an arbitrary cut.
            # Deliberately tighter than build_delay_dataset.py's 3600s (which
            # is calibrated for a multi-million-row *training* corpus, where
            # a rare true 30-40min disruption is fine to keep). This isn't
            # about window size (mean_absolute_error_seconds is all-time now,
            # not a 50-sample rolling window, see 2026-09-16's scoreboard
            # rework) - these are *proven mismatches*, not genuine model
            # error, so they'd corrupt what the metric means at any sample
            # count. Still not negligible even averaged over hundreds: one
            # leftover ~5900s mismatch biases a 350-sample all-time MAE by
            # roughly 5900/350 =~ 17s. Direct inspection of the
            # two rows this excluded (2026-09-16) found both showed the
            # concrete signature of a mismatch (a multi-dispatch trip_id gap
            # in one case, an implausible schedule join in the other), not
            # proof this can never wrongly exclude a real extreme delay.
            continue
        entries.append((
            (trip_id, stop_sequence, service_date),
            ScoreboardEntry(
                route_id=route_id,
                vehicle_route_type=vehicle_route_type,
                predicted_delay_seconds=predicted_delay_seconds,
                actual_delay_seconds=actual_delay_seconds,
                error_seconds=abs(predicted_delay_seconds - actual_delay_seconds),
                predicted_at=predicted_at,
            ),
        ))

    entries.sort(key=lambda pair: pair[1].predicted_at, reverse=True)
    # Two predictions can target the exact same real-world arrival - someone
    # reopening a vehicle's popup just after the 15s frontend cache expired
    # (see PREDICTION_TTL_MS in app.js) logs a second, near-identical
    # prediction_log row for the same (trip_id, stop_sequence, service_date).
    # Both then reconcile against the same single real outcome, so counting
    # both double-weights that one event in a 50-sample average. Confirmed
    # real, not theoretical (2026-09-16): 9 such groups found, all exactly 2
    # rows, all 16-142s apart - a double-click pattern, not genuinely spaced-
    # out re-predictions. Keep only the latest (already sorted above) per
    # target, same DISTINCT-style dedup already used elsewhere in this
    # project rather than a new one-off pattern.
    seen_targets = set()
    entries = [pair for pair in entries if not (pair[0] in seen_targets or seen_targets.add(pair[0]))]
    entries = [entry for _key, entry in entries]

    mean_absolute_error = (
        sum(e.error_seconds for e in entries) / len(entries) if entries else None
    )
    best = sorted(entries, key=lambda e: e.error_seconds)[:SCOREBOARD_BEST_DISPLAY]
    return Scoreboard(
        reconciled_count=len(entries),
        mean_absolute_error_seconds=mean_absolute_error,
        best=best,
    )


@app.get("/health")
def health() -> dict:
    try:
        current_weather = live_weather.current()
    except Exception:
        # A weather-API hiccup shouldn't make the whole health check fail -
        # this just means no reading has ever succeeded yet.
        current_weather = None
    return {
        "status": "ok",
        "model_loaded": model is not None,
        "model_type": model.name if model else None,
        "schedule_loaded": schedule_lookup is not None,
        "current_weather": current_weather,
    }


class CurrentDelaysRequest(BaseModel):
    trip_ids: list[str] = Field(examples=[["BKK_D19380125", "BKK_D20075241"]])
    service_date: str = Field(examples=["20260914"])


# When the map should stop trusting a confirmed delay and show "no recent
# data" instead (added 2026-09-24). Never sooner than STALE_MIN_MINUTES
# (buses skip stops nobody uses, so a missing confirmation at the very next
# stop is normal), but later when the timetable says the next stop is far
# away - GRACE minutes past when the vehicle, running at its current delay,
# should have reached it. A fixed 10-min cutoff greyed real night buses on
# long stretches (9 of 181 vehicles on the first night check).
STALE_MIN_MINUTES = 10
STALE_GRACE_MINUTES = 5


class CurrentDelayReading(BaseModel):
    delay_seconds: float
    minutes_ago: float
    # Show this delay only while minutes_ago <= stale_after_minutes.
    stale_after_minutes: float


class CurrentDelaysResponse(BaseModel):
    # Keyed by tripId - a trip missing from this dict means "no confirmed
    # arrival for it yet today" (its very first stop hasn't happened), not
    # an error; the caller (the map) should render that as "no data yet"
    # rather than treat it as a failure.
    delays: dict[str, CurrentDelayReading]


@app.post("/vehicles/current-delays", response_model=CurrentDelaysResponse)
def vehicles_current_delays(request: CurrentDelaysRequest) -> CurrentDelaysResponse:
    """
    The map's real-time coloring feed (added 2026-09-14, at Mustafa's
    request for a way to *see* delay at a glance instead of only on
    click) - each vehicle's most recent CONFIRMED delay today, for
    potentially hundreds of vehicles in one call. Deliberately real
    ground truth, not a fresh model prediction per vehicle - that would
    mean running full inference (plus its own DB/weather lookups) for
    every tracked vehicle on every ~10s map poll, the same cost problem
    that made per-click prediction lazy in the first place. This is one
    bulk query instead, cheap enough to run every poll.

    Unlike fetch_upstream_delay (used for a single vehicle's *own*
    prediction, which deliberately only looks *before* its current stop
    to avoid leaking the very thing being predicted), this wants the
    freshest confirmed STOP for the whole trip today, including its
    current stop if that's already been confirmed STOPPED_AT/100% -
    there's no prediction to leak into here, just a live status display.

    The delay at that stop is measured at the ARRIVAL (first sighting
    there), or the DEPARTURE (last sighting) at a first stop - the same
    definition as the training labels (build_delay_dataset.py). Until
    2026-09-23 it used the latest sighting at any stop, so a vehicle
    standing still (typically at the end of the line in the evening, still
    showing its last trip) got one second "later" every second it stood
    there: measured live, 33 red markers of which 26 were this, e.g. +61
    min for a bus whose last real arrival was +3 min.
    """
    if schedule_lookup is None:
        raise HTTPException(status_code=503, detail="Schedule not loaded")
    if not request.trip_ids:
        return CurrentDelaysResponse(delays={})

    query = """
        WITH latest AS (
            SELECT DISTINCT ON (trip_id) trip_id, stop_sequence
            FROM vehicle_position_snapshots
            WHERE trip_id = ANY(%(trip_ids)s) AND service_date = %(service_date)s
              AND status = 'STOPPED_AT' AND stop_distance_percent = 100
            ORDER BY trip_id, recorded_at DESC
        )
        SELECT l.trip_id, l.stop_sequence,
               CASE WHEN l.stop_sequence = 1 THEN MAX(v.recorded_at) ELSE MIN(v.recorded_at) END
        FROM latest l
        JOIN vehicle_position_snapshots v
          ON v.trip_id = l.trip_id AND v.stop_sequence = l.stop_sequence
         AND v.service_date = %(service_date)s
         AND v.status = 'STOPPED_AT' AND v.stop_distance_percent = 100
        GROUP BY l.trip_id, l.stop_sequence
    """
    with psycopg2.connect(**DB_CONFIG) as conn, conn.cursor() as cur:
        cur.execute(query, {"trip_ids": request.trip_ids, "service_date": request.service_date})
        rows = cur.fetchall()

    delays: dict[str, CurrentDelayReading] = {}
    now = datetime.now(BUDAPEST_TZ)
    for trip_id, stop_sequence, recorded_at in rows:
        gtfs_trip_id = trip_id.removeprefix("BKK_")
        scheduled = schedule_lookup.scheduled_arrival(gtfs_trip_id, stop_sequence, request.service_date)
        if scheduled is None:
            continue
        recorded_at = recorded_at.astimezone(BUDAPEST_TZ)
        delay = timedelta(seconds=(recorded_at - scheduled).total_seconds())
        # Stop numbers run 1..n without gaps in every BKK trip (checked
        # 2026-09-24), so the next stop is simply +1; None = last stop.
        next_scheduled = schedule_lookup.scheduled_arrival(gtfs_trip_id, stop_sequence + 1, request.service_date)
        stale_after = STALE_MIN_MINUTES
        if next_scheduled is not None:
            minutes_to_next = (next_scheduled + delay - recorded_at).total_seconds() / 60
            stale_after = max(STALE_MIN_MINUTES, minutes_to_next + STALE_GRACE_MINUTES)
        delays[trip_id] = CurrentDelayReading(
            delay_seconds=delay.total_seconds(),
            minutes_ago=(now - recorded_at).total_seconds() / 60,
            stale_after_minutes=stale_after,
        )
    return CurrentDelaysResponse(delays=delays)


@app.post("/predict", response_model=DelayPredictionResponse)
def predict(request: DelayPredictionRequest) -> DelayPredictionResponse:
    if model is None:
        raise HTTPException(status_code=503, detail="Model not loaded")

    arrival = request.scheduled_arrival
    arrival = arrival.replace(tzinfo=BUDAPEST_TZ) if arrival.tzinfo is None else arrival.astimezone(BUDAPEST_TZ)

    weather = live_weather.current()
    predicted_delay = model.predict_one(
        route_id=request.route_id,
        stop_id=request.stop_id,
        vehicle_route_type=request.vehicle_route_type,
        stop_sequence=request.stop_sequence,
        hour=arrival.hour,
        day_of_week=arrival.weekday(),
        upstream_delay_seconds=request.upstream_delay_seconds,
        has_upstream_delay=int(request.has_upstream_delay),
        temperature_2m=request.temperature_2m if request.temperature_2m is not None else weather["temperature_2m"],
        precipitation=request.precipitation if request.precipitation is not None else weather["precipitation"],
        wind_speed_10m=request.wind_speed_10m if request.wind_speed_10m is not None else weather["wind_speed_10m"],
        deviated=int(request.deviated),
    )
    last_confirmed_delay = (
        UpstreamDelayReading(delay_seconds=request.upstream_delay_seconds)
        if request.has_upstream_delay
        else None
    )
    return DelayPredictionResponse(predicted_delay_seconds=predicted_delay, last_confirmed_delay=last_confirmed_delay)


@app.post("/predict/from-vehicle", response_model=DelayPredictionResponse)
def predict_from_vehicle(request: LiveVehiclePredictionRequest) -> DelayPredictionResponse:
    """
    The endpoint that actually wires the two services together (added
    2026-09-12): /predict above needs scheduled_arrival already known,
    which the Java side has no way to compute - it never imported
    stop_times.txt (387MB, ~5.08M rows) into Postgres, since nothing
    needed it there before now. Rather than duplicate that import into a
    second database just for this, this endpoint accepts the live feed's
    raw fields and resolves scheduled_arrival itself via ScheduleLookup,
    which already holds the whole static schedule in memory.

    404s (not a fault worth logging as an error) for the real, expected
    case where this trip/stop isn't on our imported static schedule at
    all - a MÁV-START/Volánbusz vehicle BKK's live feed surfaces (see the
    2026-08-28 route-name investigation), or one running off a schedule
    version we don't have. The Spring Boot side is expected to turn this
    into a normal "prediction unavailable" response, not a browser-visible
    error.
    """
    return run_prediction(request, source="click")


def run_prediction(request: LiveVehiclePredictionRequest, source: str) -> DelayPredictionResponse:
    """
    The one prediction path shared by map clicks (source "click") and the
    automatic sampling job (source "auto") - kept as a single function so the
    two can never drift apart in how a live vehicle is turned into a
    prediction. Logs the prediction (with BKK's own, when available) tagged
    with its source.
    """
    if model is None or schedule_lookup is None:
        raise HTTPException(status_code=503, detail="Model or schedule not loaded")

    gtfs_trip_id = request.trip_id.removeprefix("BKK_")
    scheduled_arrival = schedule_lookup.scheduled_arrival(
        gtfs_trip_id, request.stop_sequence, request.service_date
    )
    if scheduled_arrival is None:
        raise HTTPException(
            status_code=404,
            detail="No scheduled arrival found for this trip/stop - not on the imported static GTFS schedule",
        )

    upstream = fetch_upstream_delay(gtfs_trip_id, request.stop_sequence, request.service_date)
    weather = live_weather.current()

    predicted_delay = model.predict_one(
        route_id=request.route_id,
        stop_id=request.stop_id,
        vehicle_route_type=request.vehicle_route_type,
        stop_sequence=request.stop_sequence,
        hour=scheduled_arrival.hour,
        day_of_week=scheduled_arrival.weekday(),
        upstream_delay_seconds=upstream.delay_seconds if upstream else 0.0,
        has_upstream_delay=int(upstream is not None),
        temperature_2m=weather["temperature_2m"],
        precipitation=weather["precipitation"],
        wind_speed_10m=weather["wind_speed_10m"],
        deviated=int(request.deviated),
    )
    bkk_prediction = fetch_bkk_prediction(request.trip_id, request.stop_sequence, request.service_date)
    log_prediction(
        trip_id=request.trip_id, route_id=request.route_id, stop_id=request.stop_id,
        vehicle_route_type=request.vehicle_route_type, stop_sequence=request.stop_sequence,
        service_date=request.service_date, predicted_delay_seconds=predicted_delay,
        bkk_predicted_delay_seconds=bkk_prediction[0] if bkk_prediction else None,
        bkk_predicted_arrival_epoch=bkk_prediction[1] if bkk_prediction else None,
        source=source,
    )
    return DelayPredictionResponse(predicted_delay_seconds=predicted_delay, last_confirmed_delay=upstream)


# ---------------------------------------------------------------- automatic sampling
# Added 2026-09-21. Map clicks alone log only a handful of predictions on most
# days, far too few for a fair comparison against BKK's own predictions. This
# job periodically takes a random sample of vehicles currently heading to a
# stop and runs them through the SAME prediction path a click uses
# (run_prediction), logging source='auto' so the live scoreboard - which only
# counts source='click' - is unaffected. Off unless AUTO_SAMPLE_ENABLED=1.
AUTO_SAMPLE_ENABLED = os.environ.get("AUTO_SAMPLE_ENABLED", "0") == "1"
AUTO_SAMPLE_INTERVAL_SECONDS = int(os.environ.get("AUTO_SAMPLE_INTERVAL_SECONDS", "300"))
AUTO_SAMPLE_SIZE = int(os.environ.get("AUTO_SAMPLE_SIZE", "50"))
AUTO_SAMPLE_PAUSE_SECONDS = 0.25  # between vehicles: at most ~4 BKK trip-details calls per second

# The most recent snapshot per vehicle from the last 2 minutes that is heading
# to a stop on a trip, and for which no prediction (from anyone) has been
# logged yet for that same trip/stop/date - so each real arrival is sampled at
# most once, not re-predicted on every cycle. Same fields the Java side
# forwards on a click.
AUTO_SAMPLE_CANDIDATES_QUERY = """
    SELECT DISTINCT ON (s.vehicle_id)
           s.trip_id, s.route_id, s.stop_id, s.vehicle_route_type,
           s.stop_sequence, s.service_date, COALESCE(s.deviated, false)
    FROM vehicle_position_snapshots s
    WHERE s.recorded_at > now() - interval '2 minutes'
      AND s.trip_id IS NOT NULL
      AND s.stop_sequence IS NOT NULL
      AND s.status = 'IN_TRANSIT_TO'
      AND s.route_id IS NOT NULL
      AND s.stop_id IS NOT NULL
      AND s.vehicle_route_type IS NOT NULL
      AND COALESCE(s.stale, false) = false
      AND NOT EXISTS (
          SELECT 1 FROM prediction_log pl
          WHERE pl.trip_id = s.trip_id
            AND pl.stop_sequence = s.stop_sequence
            AND pl.service_date = s.service_date
      )
    ORDER BY s.vehicle_id, s.recorded_at DESC
"""


def auto_sample_once() -> dict:
    """One sampling cycle. Blocking (DB + HTTP), so the loop runs it in a
    worker thread. Returns counts for the log line."""
    with psycopg2.connect(**DB_CONFIG) as conn, conn.cursor() as cur:
        cur.execute(AUTO_SAMPLE_CANDIDATES_QUERY)
        candidates = cur.fetchall()

    chosen = random.sample(candidates, min(AUTO_SAMPLE_SIZE, len(candidates)))
    predicted = skipped = failed = 0
    for trip_id, route_id, stop_id, vehicle_route_type, stop_sequence, service_date, deviated in chosen:
        request = LiveVehiclePredictionRequest(
            trip_id=trip_id, route_id=route_id, stop_id=stop_id, vehicle_route_type=vehicle_route_type,
            stop_sequence=stop_sequence, service_date=service_date, deviated=bool(deviated),
        )
        try:
            run_prediction(request, source="auto")
            predicted += 1
        except HTTPException:
            skipped += 1  # e.g. a Volán/MÁV trip that is not on BKK's static schedule
        except Exception as exc:  # noqa: BLE001 - one bad vehicle must not stop the cycle
            failed += 1
            logger.warning("auto-sample: prediction failed (%s)", type(exc).__name__)
        time.sleep(AUTO_SAMPLE_PAUSE_SECONDS)
    return {"candidates": len(candidates), "sampled": len(chosen), "predicted": predicted,
            "skipped": skipped, "failed": failed}


async def auto_sample_loop() -> None:
    logger.info("auto-sample: enabled, %d vehicles every %ds", AUTO_SAMPLE_SIZE, AUTO_SAMPLE_INTERVAL_SECONDS)
    while True:
        try:
            counts = await asyncio.to_thread(auto_sample_once)
            logger.info("auto-sample: %s", counts)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - keep the loop alive across DB/network hiccups
            logger.warning("auto-sample: cycle failed (%s)", type(exc).__name__)
        await asyncio.sleep(AUTO_SAMPLE_INTERVAL_SECONDS)
