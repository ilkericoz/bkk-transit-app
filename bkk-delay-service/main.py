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
import hashlib
import logging
import os
import random
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import psycopg2
from psycopg2.extras import execute_values
import requests
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from data_quality import backlog_sql_filter
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


# Every-stop predictions (added 2026-09-25): one row per (trip, stop) the
# vehicle was heading to, predicted from what was known when its previous stop
# was confirmed (reference_time) and graded once it arrives. Unlike
# prediction_log (map clicks + a random sample), this covers every vehicle, so
# it is both the map's "accuracy" colouring and an unbiased live measure that
# matches the offline walk-forward setup. A new table only - nothing existing
# changes. One row per target: re-processing the same event is a no-op.
STOP_PREDICTIONS_DDL = """
    CREATE TABLE IF NOT EXISTS stop_predictions (
        id BIGSERIAL PRIMARY KEY,
        trip_id VARCHAR(255) NOT NULL,
        route_id VARCHAR(255),
        vehicle_route_type VARCHAR(255),
        stop_id VARCHAR(255) NOT NULL,
        stop_sequence INTEGER NOT NULL,
        service_date VARCHAR(255) NOT NULL,
        reference_time TIMESTAMPTZ NOT NULL,
        predicted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        predicted_delay_seconds DOUBLE PRECISION NOT NULL,
        upstream_delay_seconds DOUBLE PRECISION,
        has_vehicle_ahead INTEGER,
        ahead_delay_seconds DOUBLE PRECISION,
        model_version VARCHAR(32),
        actual_recorded_at TIMESTAMPTZ,
        actual_delay_seconds DOUBLE PRECISION,
        UNIQUE (trip_id, service_date, stop_sequence)
    );
    -- Added 2026-09-25 with the recent-traffic feature (nullable, additive).
    ALTER TABLE stop_predictions ADD COLUMN IF NOT EXISTS has_segment_recent INTEGER;
    ALTER TABLE stop_predictions ADD COLUMN IF NOT EXISTS segment_recent_gain_seconds DOUBLE PRECISION;
    CREATE INDEX IF NOT EXISTS idx_stop_predictions_ungraded
        ON stop_predictions (reference_time) WHERE actual_recorded_at IS NULL;
"""

# First 12 hex chars of the model file's sha256 - stored with every
# stop_predictions row, so results can always be split by the exact model.
model_version: str | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global model, schedule_lookup, model_version
    if not MODEL_PATH.exists():
        raise RuntimeError(f"No trained model at {MODEL_PATH} - run scripts/train_model.py first.")
    model = BaseDelayModel.load(MODEL_PATH)
    model_version = hashlib.sha256(MODEL_PATH.read_bytes()).hexdigest()[:12]
    schedule_lookup = ScheduleLookup()

    # Self-provisioning rather than a manual migration step - this table is
    # this service's own concern (Java's Hibernate ddl-auto=update doesn't
    # know about it, and shouldn't need to), so the sidecar creates it
    # itself on startup if it isn't already there.
    with psycopg2.connect(**DB_CONFIG) as conn, conn.cursor() as cur:
        cur.execute(PREDICTION_LOG_DDL)
        cur.execute(SCOREBOARD_INDEX_DDL)
        cur.execute(STOP_PREDICTIONS_DDL)
        conn.commit()

    sampler = asyncio.create_task(auto_sample_loop()) if AUTO_SAMPLE_ENABLED else None
    stop_predictor = asyncio.create_task(stop_predictions_loop()) if STOP_PREDICTIONS_ENABLED else None
    yield
    for task in (sampler, stop_predictor):
        if task is not None:
            task.cancel()


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
    # Optional vehicle-ahead features (see fetch_vehicle_ahead) - this
    # endpoint has no trip to look them up for. Left unset = no vehicle ahead.
    has_vehicle_ahead: bool = False
    ahead_delay_seconds: float = 0.0
    minutes_since_ahead: float = 0.0
    scheduled_gap_minutes: float = 0.0
    # Optional recent-traffic features (see fetch_segment_recent); unset = none.
    has_segment_recent: bool = False
    segment_recent_gain_seconds: float = 0.0
    segment_recent_count: int = 0


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
    # When that upstream stop was confirmed - the reference moment for the
    # vehicle-ahead lookup. Internal only, not sent to callers.
    recorded_at: datetime | None = Field(default=None, exclude=True)


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
    return UpstreamDelayReading(delay_seconds=delay_seconds, minutes_ago=minutes_ago, recorded_at=recorded_at)


# Same limits as build_delay_dataset.py (VEHICLE_AHEAD_MAX_MINUTES,
# MAX_ABS_DELAY_SECONDS) - the live lookup has to see exactly what training saw.
VEHICLE_AHEAD_MAX_MINUTES = 60
VEHICLE_AHEAD_MAX_ABS_DELAY_SECONDS = 3600
NO_VEHICLE_AHEAD = {"has_vehicle_ahead": 0, "ahead_delay_seconds": 0.0,
                    "minutes_since_ahead": 0.0, "scheduled_gap_minutes": 0.0}


def fetch_vehicle_ahead(route_id: str, stop_id: str, gtfs_trip_id: str,
                        own_scheduled_arrival: datetime, reference_time: datetime) -> dict:
    """
    Live equivalent of build_delay_dataset.add_vehicle_ahead (added
    2026-09-24): the previous vehicle of the same route at the same stop
    that got there before `reference_time` - the moment this trip's
    upstream stop was confirmed, NOT now. Training used that moment, so
    using "now" here would feed the model inputs it never saw (the kind of
    train/serve mismatch that got route_recent_delay removed).

    "Got there" uses the label definition: first sighting at the stop, or
    the last one if that stop is the other trip's first stop (departure).
    Candidates without a timetable entry, or with |delay| beyond the
    training data's outlier limit, are skipped, as they never existed in
    the training data either. ~50 ms at night on the live table.
    """
    query = """
        SELECT trip_id, stop_sequence, service_date,
               CASE WHEN stop_sequence = 1 THEN MAX(recorded_at) ELSE MIN(recorded_at) END AS arrived
        FROM vehicle_position_snapshots
        WHERE route_id = %(route)s AND stop_id = %(stop)s
          AND status = 'STOPPED_AT' AND stop_distance_percent = 100
          AND recorded_at >= %(since)s
          AND trip_id <> %(own)s
        GROUP BY trip_id, stop_sequence, service_date
        ORDER BY arrived DESC
    """
    # Look a bit further back than the 60-min limit so a visit that started
    # just before the window still gets its true first sighting.
    since = reference_time - timedelta(minutes=VEHICLE_AHEAD_MAX_MINUTES + 15)
    with psycopg2.connect(**DB_CONFIG) as conn, conn.cursor() as cur:
        cur.execute(query, {"route": route_id, "stop": stop_id, "since": since, "own": f"BKK_{gtfs_trip_id}"})
        rows = cur.fetchall()
    return pick_vehicle_ahead(rows, f"BKK_{gtfs_trip_id}", own_scheduled_arrival, reference_time)


def pick_vehicle_ahead(candidates, own_trip_id: str, own_scheduled_arrival: datetime,
                       reference_time: datetime) -> dict:
    """
    The selection rule for the vehicle ahead, shared by the per-click lookup
    (fetch_vehicle_ahead) and the every-stop grading job (predict_new_stops)
    so the two can never drift apart. `candidates` are (trip_id,
    stop_sequence, service_date, arrived) visits at the target stop by the
    same route, newest first.
    """
    for trip_id, stop_sequence, service_date, arrived in candidates:
        if trip_id == own_trip_id:
            continue
        arrived = arrived.astimezone(BUDAPEST_TZ)
        if arrived >= reference_time:
            continue  # got there after our reference moment - not known yet
        minutes_since = (reference_time - arrived).total_seconds() / 60
        if minutes_since > VEHICLE_AHEAD_MAX_MINUTES:
            break  # sorted newest first: everything further down is older still
        scheduled = schedule_lookup.scheduled_arrival(trip_id.removeprefix("BKK_"), stop_sequence, service_date)
        if scheduled is None:
            continue
        delay = (arrived - scheduled).total_seconds()
        if abs(delay) > VEHICLE_AHEAD_MAX_ABS_DELAY_SECONDS:
            continue
        return {"has_vehicle_ahead": 1, "ahead_delay_seconds": delay, "minutes_since_ahead": minutes_since,
                "scheduled_gap_minutes": (own_scheduled_arrival - scheduled).total_seconds() / 60}
    return dict(NO_VEHICLE_AHEAD)


# Recent traffic on the stretch into the target stop (added 2026-09-25) -
# live equivalent of build_delay_dataset.add_segment_recent; the constants
# must match it.
SEGMENT_RECENT_MINUTES = 15
# Extra look-back so a visit that began before the window still gets its true
# first sighting (the previous stop can be well before the target stop).
SEGMENT_LOOKBACK_MARGIN = timedelta(minutes=30)
NO_SEGMENT_RECENT = {"has_segment_recent": 0, "segment_recent_gain_seconds": 0.0, "segment_recent_count": 0}

# "until" = the reference moment: later visits can't count anyway, and
# without it a lookup for a past moment scans everything since then (~30 s
# per lookup in the 2026-09-26 consistency check).
SEGMENT_VISITS_QUERY = """
    SELECT stop_id, trip_id, stop_sequence, service_date,
           CASE WHEN stop_sequence = 1 THEN MAX(recorded_at) ELSE MIN(recorded_at) END AS arrived
    FROM vehicle_position_snapshots
    WHERE recorded_at >= %(since)s AND recorded_at < %(until)s
      AND status = 'STOPPED_AT' AND stop_distance_percent = 100
      AND stop_id = ANY(%(stops)s) AND trip_id IS NOT NULL
    GROUP BY stop_id, trip_id, stop_sequence, service_date
"""


def index_stop_visits(rows) -> tuple[dict, dict]:
    """Visits from SEGMENT_VISITS_QUERY, indexed two ways: by stop (for the
    target stop) and by (trip, service_date, stop_sequence) (to find the same
    vehicle's visit to the stop before)."""
    by_stop: dict[str, list] = {}
    by_visit: dict[tuple, tuple] = {}
    for stop_id, trip_id, seq, service_date, arrived in rows:
        by_stop.setdefault(stop_id, []).append((trip_id, seq, service_date, arrived))
        by_visit[(trip_id, service_date, seq)] = (stop_id, arrived)
    return by_stop, by_visit


def pick_segment_recent(by_stop: dict, by_visit: dict, prev_stop_id: str | None, stop_id: str,
                        reference_time: datetime) -> dict:
    """
    Mean delay gain of vehicles (any route) on prev_stop_id -> stop_id that
    reached stop_id in the SEGMENT_RECENT_MINUTES before reference_time, each
    measured as its own delay here minus its delay at its previous stop
    (stop_sequence - 1, which must be prev_stop_id). Same selection as the
    training data: both visits on the timetable, both |delay| within the
    training data's outlier limit. Shared by the per-click lookup and the
    every-stop job so they can't drift apart.
    """
    if prev_stop_id is None:
        return dict(NO_SEGMENT_RECENT)
    window_start = reference_time - timedelta(minutes=SEGMENT_RECENT_MINUTES)
    gains = []
    for trip_id, seq, service_date, arrived in by_stop.get(stop_id, []):
        arrived = arrived.astimezone(BUDAPEST_TZ)
        if not (window_start <= arrived < reference_time):
            continue
        previous = by_visit.get((trip_id, service_date, seq - 1))
        if previous is None or previous[0] != prev_stop_id:
            continue  # only a consecutive pair on this exact stretch counts
        gtfs_trip_id = trip_id.removeprefix("BKK_")
        scheduled_here = schedule_lookup.scheduled_arrival(gtfs_trip_id, seq, service_date)
        scheduled_before = schedule_lookup.scheduled_arrival(gtfs_trip_id, seq - 1, service_date)
        if scheduled_here is None or scheduled_before is None:
            continue
        delay_here = (arrived - scheduled_here).total_seconds()
        delay_before = (previous[1].astimezone(BUDAPEST_TZ) - scheduled_before).total_seconds()
        if abs(delay_here) > VEHICLE_AHEAD_MAX_ABS_DELAY_SECONDS or abs(delay_before) > VEHICLE_AHEAD_MAX_ABS_DELAY_SECONDS:
            continue
        gains.append(delay_here - delay_before)
    if not gains:
        return dict(NO_SEGMENT_RECENT)
    return {"has_segment_recent": 1, "segment_recent_gain_seconds": sum(gains) / len(gains),
            "segment_recent_count": len(gains)}


def scheduled_prev_stop_id(gtfs_trip_id: str, stop_sequence: int) -> str | None:
    """The live-feed form ("BKK_" + stop_id) of the trip's scheduled previous stop."""
    previous = schedule_lookup.stop_id_at(gtfs_trip_id, stop_sequence - 1)
    return f"BKK_{previous}" if previous else None


def fetch_segment_recent(gtfs_trip_id: str, stop_sequence: int, stop_id: str, reference_time: datetime) -> dict:
    """Per-click version: one query for the two stops of this stretch."""
    prev_stop = scheduled_prev_stop_id(gtfs_trip_id, stop_sequence)
    if prev_stop is None:
        return dict(NO_SEGMENT_RECENT)
    since = reference_time - timedelta(minutes=SEGMENT_RECENT_MINUTES) - SEGMENT_LOOKBACK_MARGIN
    with psycopg2.connect(**DB_CONFIG) as conn, conn.cursor() as cur:
        cur.execute(SEGMENT_VISITS_QUERY, {"since": since, "until": reference_time, "stops": [prev_stop, stop_id]})
        by_stop, by_visit = index_stop_visits(cur.fetchall())
    return pick_segment_recent(by_stop, by_visit, prev_stop, stop_id, reference_time)


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


class SampledScoreboardEntry(BaseModel):
    route_id: str | None
    vehicle_route_type: str | None
    predicted_delay_seconds: float
    actual_delay_seconds: float
    error_seconds: float
    predicted_at: datetime


class SampledScoreboard(BaseModel):
    reconciled_count: int
    mean_absolute_error_seconds: float | None
    best: list[SampledScoreboardEntry]


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


@app.get("/scoreboard/sampled", response_model=SampledScoreboard)
def sampled_scoreboard() -> SampledScoreboard:
    """
    The map's scoreboard until 2026-09-26 (was /scoreboard), kept for the
    thesis: it scores the SAMPLED predictions (clicks + auto-sampler), a
    harder population than the every-stop predictions /scoreboard now uses
    (cold starts, longer look-aheads, length-biased sampling - see the
    README's 2026-09-26 entries). All models since the start, not just the
    current one. Slow (~4 s: joins the snapshots table).

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
            SampledScoreboardEntry(
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
    return SampledScoreboard(
        reconciled_count=len(entries),
        mean_absolute_error_seconds=mean_absolute_error,
        best=best,
    )


# When a prediction counts as "accurate" (added 2026-09-27): the MBTA's
# (Boston transit authority) published arrival-prediction standard,
# https://www.mbta.com/performance-metrics/arrival-prediction-accuracy -
# a window that widens with how far ahead the prediction was made. Rows:
# (horizon below, minutes; seconds EARLY allowed; seconds LATE allowed),
# early = the vehicle arrived before the predicted time. MBTA stops at 30
# min ahead; longer horizons (~0.5% of every-stop predictions) use the last
# window. A fixed 30 s / 1 min line ignored the horizon and painted ~19% of
# vehicles yellow that are accurate by this standard.
ACCURACY_WINDOWS = [(3, 60, 60), (6, 90, 120), (12, 150, 210), (30, 240, 360)]


def prediction_accurate(predicted_delay: float, actual_delay: float, horizon_seconds: float) -> bool:
    """True when the actual arrival fell inside the ACCURACY_WINDOWS window
    for a prediction made horizon_seconds before it."""
    early, late = next(((e, l) for below, e, l in ACCURACY_WINDOWS if horizon_seconds < below * 60),
                       ACCURACY_WINDOWS[-1][1:])
    return -early <= actual_delay - predicted_delay <= late


def accurate_sql(predicted: str, actual: str, horizon_seconds: str) -> str:
    """The same test as prediction_accurate, as a SQL boolean expression."""
    def bound(i: int) -> str:
        cases = " ".join(f"WHEN {horizon_seconds} < {below * 60} THEN {w[i]}"
                         for below, *w in ACCURACY_WINDOWS)
        return f"(CASE {cases} ELSE {ACCURACY_WINDOWS[-1][i + 1]} END)"
    return f"(({actual}) - ({predicted}) BETWEEN -{bound(0)} AND {bound(1)})"


class ScoreboardGroup(BaseModel):
    vehicle_route_type: str
    graded_count: int
    mean_absolute_error_seconds: float
    within_60s_share: float
    persistence_mae_seconds: float


class Scoreboard(BaseModel):
    model_version: str | None
    since: datetime | None
    graded_count: int
    mean_absolute_error_seconds: float | None
    within_60s_share: float | None
    # Carrying the delay at the previous stop forward unchanged, scored on
    # the same predictions - the "no model" yardstick for the number above.
    persistence_mae_seconds: float | None
    # Share accurate by ACCURACY_WINDOWS, for the model and for "no model".
    accurate_share: float | None
    persistence_accurate_share: float | None
    by_vehicle_type: list[ScoreboardGroup]


# The query scans the current model's rows of stop_predictions (~250k new
# rows a day, ~0.2 s at 380k rows); the map polls every 30 s per open page,
# so the result is shared for this long.
SCOREBOARD_CACHE_SECONDS = 60
_scoreboard_cache: tuple[float, Scoreboard] | None = None

_HORIZON = "extract(epoch FROM actual_recorded_at - reference_time)"
SCOREBOARD_QUERY = f"""
    SELECT GROUPING(vehicle_route_type) = 1 AS is_total, coalesce(vehicle_route_type, 'UNKNOWN'), count(*),
           avg(abs(predicted_delay_seconds - actual_delay_seconds)),
           avg((abs(predicted_delay_seconds - actual_delay_seconds) <= 60)::int)::float8,
           avg(abs(upstream_delay_seconds - actual_delay_seconds)),
           min(predicted_at),
           avg({accurate_sql("predicted_delay_seconds", "actual_delay_seconds", _HORIZON)}::int)::float8,
           avg({accurate_sql("upstream_delay_seconds", "actual_delay_seconds", _HORIZON)}::int)::float8
    FROM stop_predictions
    WHERE model_version = %(model_version)s AND actual_delay_seconds IS NOT NULL
      AND abs(actual_delay_seconds) <= %(max_abs_delay)s
      AND {backlog_sql_filter(["reference_time", "actual_recorded_at"])}
    GROUP BY ROLLUP (vehicle_route_type)
"""


@app.get("/scoreboard", response_model=Scoreboard)
def scoreboard() -> Scoreboard:
    """
    Live accuracy of the CURRENT model, from the every-stop job
    (stop_predictions): every vehicle's next stop, predicted when its
    previous stop is confirmed and graded on arrival - the same population
    as the offline walk-forward test, so the two numbers are comparable
    (README 2026-09-26: 27.5 s live vs 28.4 s offline). Replaced the
    sampled-predictions scoreboard (now /scoreboard/sampled) on 2026-09-26:
    that one mixed every model since the start and a harder, length-biased
    sample, so it showed ~52 s for a model that is ~27 s on this measure.

    Same outlier limit as the training labels (|actual| <= 3600 s) and
    without known collector backlogs (data_quality.COLLECTOR_BACKLOGS),
    where arrivals were recorded up to ~19 min late.
    """
    global _scoreboard_cache
    if _scoreboard_cache and time.time() - _scoreboard_cache[0] < SCOREBOARD_CACHE_SECONDS \
            and _scoreboard_cache[1].model_version == model_version:
        return _scoreboard_cache[1]

    with psycopg2.connect(**DB_CONFIG) as conn, conn.cursor() as cur:
        cur.execute(SCOREBOARD_QUERY, {"model_version": model_version,
                                       "max_abs_delay": VEHICLE_AHEAD_MAX_ABS_DELAY_SECONDS})
        rows = cur.fetchall()

    # ROLLUP adds the all-types row (is_total) - absent when nothing is graded yet.
    total = next((r[1:] for r in rows if r[0]), None)
    groups = [ScoreboardGroup(vehicle_route_type=vtype, graded_count=n, mean_absolute_error_seconds=mae,
                              within_60s_share=within, persistence_mae_seconds=persist)
              for is_total, vtype, n, mae, within, persist, _since, _acc, _pacc in rows if not is_total]
    result = Scoreboard(
        model_version=model_version,
        since=total[5] if total else None,
        graded_count=total[1] if total else 0,
        mean_absolute_error_seconds=total[2] if total else None,
        within_60s_share=total[3] if total else None,
        persistence_mae_seconds=total[4] if total else None,
        accurate_share=total[6] if total else None,
        persistence_accurate_share=total[7] if total else None,
        by_vehicle_type=sorted(groups, key=lambda g: g.graded_count, reverse=True),
    )
    _scoreboard_cache = (time.time(), result)
    return result


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

# A first-stop sighting this recent means the vehicle is still standing there
# (it's polled every ~15 s), i.e. it hasn't departed yet (added 2026-09-25).
FIRST_STOP_STILL_THERE = timedelta(seconds=60)


class CurrentDelayReading(BaseModel):
    delay_seconds: float
    minutes_ago: float
    # Show this delay only while minutes_ago <= stale_after_minutes.
    stale_after_minutes: float
    # The map's "accuracy" colouring (added 2026-09-25): predicted minus
    # actual delay of this trip's most recently graded every-stop prediction
    # (see stop_predictions_once), and how long ago it was graded. None when
    # no prediction for this trip has been graded yet.
    prediction_error_seconds: float | None = None
    prediction_graded_minutes_ago: float | None = None
    # Whether that prediction was accurate by ACCURACY_WINDOWS (added 2026-09-27).
    prediction_accurate: bool | None = None
    # For the popup (added 2026-09-25), so every number can be shown next to
    # the stop it belongs to: the stop the delay above was confirmed at, and
    # the graded prediction's stop, predicted and actual delay.
    stop_id: str | None = None
    graded_stop_id: str | None = None
    graded_predicted_seconds: float | None = None
    graded_actual_seconds: float | None = None


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
        cur.execute("""
            SELECT DISTINCT ON (trip_id) trip_id, predicted_delay_seconds - actual_delay_seconds, actual_recorded_at,
                   stop_id, predicted_delay_seconds, actual_delay_seconds,
                   extract(epoch FROM actual_recorded_at - reference_time)
            FROM stop_predictions
            WHERE trip_id = ANY(%(trip_ids)s) AND service_date = %(service_date)s AND actual_recorded_at IS NOT NULL
            ORDER BY trip_id, actual_recorded_at DESC
        """, {"trip_ids": request.trip_ids, "service_date": request.service_date})
        last_graded = {row[0]: row[1:] for row in cur.fetchall()}

    delays: dict[str, CurrentDelayReading] = {}
    now = datetime.now(BUDAPEST_TZ)
    for trip_id, stop_sequence, recorded_at in rows:
        gtfs_trip_id = trip_id.removeprefix("BKK_")
        scheduled = schedule_lookup.scheduled_arrival(gtfs_trip_id, stop_sequence, request.service_date)
        if scheduled is None:
            continue
        recorded_at = recorded_at.astimezone(BUDAPEST_TZ)
        delay = timedelta(seconds=(recorded_at - scheduled).total_seconds())
        if stop_sequence == 1 and now - recorded_at <= FIRST_STOP_STILL_THERE:
            # Still standing at its first stop, so it hasn't departed yet and
            # "last seen" is just "now": a bus waiting for an 18:50 departure
            # showed -102 s (blue, "early") at 18:48. Until it leaves, the
            # honest reading is the delay so far: 0 before the scheduled
            # departure, growing once that has passed.
            delay = max(timedelta(0), now - scheduled)
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
        static_stop = schedule_lookup.stop_id_at(gtfs_trip_id, stop_sequence)
        delays[trip_id].stop_id = f"BKK_{static_stop}" if static_stop else None
        if trip_id in last_graded:
            error, graded_at, graded_stop, predicted, actual, horizon = last_graded[trip_id]
            delays[trip_id].prediction_error_seconds = error
            delays[trip_id].prediction_graded_minutes_ago = (now - graded_at.astimezone(BUDAPEST_TZ)).total_seconds() / 60
            delays[trip_id].graded_stop_id = graded_stop
            delays[trip_id].graded_predicted_seconds = predicted
            delays[trip_id].graded_actual_seconds = actual
            delays[trip_id].prediction_accurate = prediction_accurate(predicted, actual, float(horizon))
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
        has_vehicle_ahead=int(request.has_vehicle_ahead),
        ahead_delay_seconds=request.ahead_delay_seconds,
        minutes_since_ahead=request.minutes_since_ahead,
        scheduled_gap_minutes=request.scheduled_gap_minutes,
        has_segment_recent=int(request.has_segment_recent),
        segment_recent_gain_seconds=request.segment_recent_gain_seconds,
        segment_recent_count=request.segment_recent_count,
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
    # No upstream stop = no reference moment, same as in training (rows
    # without an upstream stop never get a vehicle ahead there).
    ahead = (
        fetch_vehicle_ahead(request.route_id, request.stop_id, gtfs_trip_id, scheduled_arrival, upstream.recorded_at)
        if upstream is not None else dict(NO_VEHICLE_AHEAD)
    )
    segment = (
        fetch_segment_recent(gtfs_trip_id, request.stop_sequence, request.stop_id, upstream.recorded_at)
        if upstream is not None else dict(NO_SEGMENT_RECENT)
    )
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
        **ahead,
        **segment,
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



# ---------------------------------------------------------------- every-stop predictions
# Added 2026-09-25. Every STOP_PREDICTIONS_INTERVAL_SECONDS, for each stop
# visit confirmed since the last cycle (arrival = first sighting; at a first
# stop, the departure = last sighting once the vehicle has been gone for a
# minute), predict the vehicle's NEXT stop from exactly what was known at that
# moment, and grade earlier predictions whose target has now been reached.
# That is the offline walk-forward setup, live, for every vehicle - so its
# error is directly comparable to the offline figure (no sampling bias), and
# it gives the map an accuracy colour for nearly every vehicle. Bulk queries
# and one batch predict per cycle: at rush hour ~500 stops get confirmed per
# 30 s, far too many for one per-click lookup each.
STOP_PREDICTIONS_ENABLED = os.environ.get("STOP_PREDICTIONS_ENABLED", "0") == "1"
STOP_PREDICTIONS_INTERVAL_SECONDS = 30
# A confirmed event is processed only once it is this old: long enough for a
# first-stop departure to be final (the vehicle has not been seen there since).
STOP_PREDICTIONS_SETTLE = timedelta(seconds=60)
# How far back the event query looks, so a visit's first sighting (or a long
# wait at a first stop) is fully inside the window.
STOP_PREDICTIONS_LOOKBACK = timedelta(minutes=30)

_stop_predictions_watermark: datetime | None = None

STOP_EVENTS_QUERY = """
    SELECT trip_id, route_id, vehicle_route_type, stop_sequence, service_date,
           CASE WHEN stop_sequence = 1 THEN MAX(recorded_at) ELSE MIN(recorded_at) END AS event_time,
           bool_or(COALESCE(deviated, false)) AS deviated
    FROM vehicle_position_snapshots
    WHERE recorded_at > %(window_start)s
      AND status = 'STOPPED_AT' AND stop_distance_percent = 100
      AND trip_id IS NOT NULL AND route_id IS NOT NULL AND stop_sequence IS NOT NULL
      AND vehicle_route_type IS NOT NULL
    GROUP BY trip_id, route_id, vehicle_route_type, stop_sequence, service_date
    HAVING (CASE WHEN stop_sequence = 1 THEN MAX(recorded_at) ELSE MIN(recorded_at) END) > %(prev)s
       AND (CASE WHEN stop_sequence = 1 THEN MAX(recorded_at) ELSE MIN(recorded_at) END) <= %(until)s
"""

# Candidate vehicles ahead for all target stops of this cycle in one query -
# the same visits fetch_vehicle_ahead would find one stop at a time.
STOP_AHEAD_CANDIDATES_QUERY = """
    SELECT route_id, stop_id, trip_id, stop_sequence, service_date,
           CASE WHEN stop_sequence = 1 THEN MAX(recorded_at) ELSE MIN(recorded_at) END AS arrived
    FROM vehicle_position_snapshots
    WHERE recorded_at >= %(since)s
      AND status = 'STOPPED_AT' AND stop_distance_percent = 100
      AND route_id = ANY(%(routes)s) AND stop_id = ANY(%(stops)s)
    GROUP BY route_id, stop_id, trip_id, stop_sequence, service_date
"""

# Predictions whose target stop has now been reached: its first sighting
# after the reference moment (targets are never a first stop, so arrival).
STOP_GRADE_QUERY = """
    SELECT sp.id, sp.trip_id, sp.stop_sequence, sp.service_date, MIN(vs.recorded_at) AS arrived
    FROM stop_predictions sp
    JOIN vehicle_position_snapshots vs
      ON vs.trip_id = sp.trip_id AND vs.stop_id = sp.stop_id AND vs.stop_sequence = sp.stop_sequence
     AND vs.service_date = sp.service_date AND vs.status = 'STOPPED_AT' AND vs.stop_distance_percent = 100
     AND vs.recorded_at > sp.reference_time
    WHERE sp.actual_recorded_at IS NULL AND sp.reference_time > now() - interval '3 hours'
    GROUP BY sp.id, sp.trip_id, sp.stop_sequence, sp.service_date
"""


def stop_predictions_once() -> dict:
    """One cycle: predict the next stop for newly confirmed stop events, then
    grade whatever has been reached. Blocking (DB), run in a worker thread."""
    global _stop_predictions_watermark
    import pandas as pd

    until = datetime.now(BUDAPEST_TZ) - STOP_PREDICTIONS_SETTLE
    prev = _stop_predictions_watermark or until - timedelta(seconds=STOP_PREDICTIONS_INTERVAL_SECONDS)
    with psycopg2.connect(**DB_CONFIG) as conn, conn.cursor() as cur:
        cur.execute(STOP_EVENTS_QUERY, {"window_start": prev - STOP_PREDICTIONS_LOOKBACK, "prev": prev, "until": until})
        events = cur.fetchall()

        # The next-stop feature row for each event, built like a click's.
        targets = []
        for trip_id, route_id, vehicle_route_type, seq, service_date, event_time, deviated in events:
            gtfs_trip_id = trip_id.removeprefix("BKK_")
            scheduled_here = schedule_lookup.scheduled_arrival(gtfs_trip_id, seq, service_date)
            next_stop = schedule_lookup.stop_id_at(gtfs_trip_id, seq + 1)
            scheduled_next = schedule_lookup.scheduled_arrival(gtfs_trip_id, seq + 1, service_date)
            if scheduled_here is None or next_stop is None or scheduled_next is None:
                continue  # not on BKK's timetable, or this was the trip's last stop
            event_time = event_time.astimezone(BUDAPEST_TZ)
            upstream_delay = (event_time - scheduled_here).total_seconds()
            if abs(upstream_delay) > VEHICLE_AHEAD_MAX_ABS_DELAY_SECONDS:
                continue  # training drops such rows, so they never act as an upstream stop
            targets.append({"trip_id": trip_id, "route_id": route_id, "vehicle_route_type": vehicle_route_type,
                            "stop_id": f"BKK_{next_stop}", "stop_sequence": seq + 1, "service_date": service_date,
                            "reference_time": event_time, "scheduled_arrival": scheduled_next,
                            "upstream_delay_seconds": upstream_delay, "deviated": int(deviated)})

        predicted_count = 0
        if targets:
            cur.execute(STOP_AHEAD_CANDIDATES_QUERY, {
                "since": min(t["reference_time"] for t in targets) - timedelta(minutes=VEHICLE_AHEAD_MAX_MINUTES + 15),
                "routes": sorted({t["route_id"] for t in targets}),
                "stops": sorted({t["stop_id"] for t in targets}),
            })
            by_stop: dict[tuple[str, str], list] = {}
            for route_id, stop_id, trip_id, seq, service_date, arrived in cur.fetchall():
                by_stop.setdefault((route_id, stop_id), []).append((trip_id, seq, service_date, arrived))
            for visits in by_stop.values():
                visits.sort(key=lambda v: v[3], reverse=True)  # newest first, like fetch_vehicle_ahead

            # Recent traffic on each target's stretch: one query for all
            # previous/target stops of this cycle, then the shared selection.
            for t in targets:
                t["prev_stop_id"] = scheduled_prev_stop_id(t["trip_id"].removeprefix("BKK_"), t["stop_sequence"])
            segment_stops = sorted({t["stop_id"] for t in targets} | {t["prev_stop_id"] for t in targets if t["prev_stop_id"]})
            cur.execute(SEGMENT_VISITS_QUERY, {
                "since": min(t["reference_time"] for t in targets)
                         - timedelta(minutes=SEGMENT_RECENT_MINUTES) - SEGMENT_LOOKBACK_MARGIN,
                "until": max(t["reference_time"] for t in targets),
                "stops": segment_stops,
            })
            visits_by_stop, visits_by_key = index_stop_visits(cur.fetchall())

            weather = live_weather.current()
            rows = []
            for t in targets:
                ahead = pick_vehicle_ahead(by_stop.get((t["route_id"], t["stop_id"]), []), t["trip_id"],
                                           t["scheduled_arrival"], t["reference_time"])
                segment = pick_segment_recent(visits_by_stop, visits_by_key, t["prev_stop_id"], t["stop_id"],
                                              t["reference_time"])
                rows.append({**t, **ahead, **segment, "has_upstream_delay": 1,
                             "hour": t["scheduled_arrival"].hour, "day_of_week": t["scheduled_arrival"].weekday(),
                             "temperature_2m": weather["temperature_2m"], "precipitation": weather["precipitation"],
                             "wind_speed_10m": weather["wind_speed_10m"]})
            predicted = model.predict(pd.DataFrame(rows)).to_numpy()
            execute_values(cur, """
                INSERT INTO stop_predictions (trip_id, route_id, vehicle_route_type, stop_id, stop_sequence,
                    service_date, reference_time, predicted_delay_seconds, upstream_delay_seconds,
                    has_vehicle_ahead, ahead_delay_seconds, model_version,
                    has_segment_recent, segment_recent_gain_seconds)
                VALUES %s ON CONFLICT (trip_id, service_date, stop_sequence) DO NOTHING
            """, [(r["trip_id"], r["route_id"], r["vehicle_route_type"], r["stop_id"], r["stop_sequence"],
                   r["service_date"], r["reference_time"], float(p), r["upstream_delay_seconds"],
                   r["has_vehicle_ahead"], r["ahead_delay_seconds"], model_version,
                   r["has_segment_recent"], r["segment_recent_gain_seconds"])
                  for r, p in zip(rows, predicted)])
            conn.commit()
            predicted_count = len(rows)

        _stop_predictions_watermark = until
        return {"events": len(events), "predicted": predicted_count, **grade_stop_predictions(cur, conn)}


def grade_stop_predictions(cur, conn) -> dict:
    """Fill in the actual delay for predictions whose target stop was reached."""
    cur.execute(STOP_GRADE_QUERY)
    graded = []
    for pred_id, trip_id, seq, service_date, arrived in cur.fetchall():
        scheduled = schedule_lookup.scheduled_arrival(trip_id.removeprefix("BKK_"), seq, service_date)
        if scheduled is not None:
            graded.append((pred_id, arrived, (arrived.astimezone(BUDAPEST_TZ) - scheduled).total_seconds()))
    if graded:
        execute_values(cur, """
            UPDATE stop_predictions sp SET actual_recorded_at = g.arrived, actual_delay_seconds = g.delay
            FROM (VALUES %s) AS g(id, arrived, delay) WHERE sp.id = g.id
        """, graded, template="(%s, %s::timestamptz, %s::double precision)")
        conn.commit()
    return {"graded": len(graded)}


async def stop_predictions_loop() -> None:
    logger.info("stop-predictions: enabled, every %ds", STOP_PREDICTIONS_INTERVAL_SECONDS)
    while True:
        started = time.time()
        try:
            counts = await asyncio.to_thread(stop_predictions_once)
            logger.info("stop-predictions: %s in %.1fs", counts, time.time() - started)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - keep the loop alive across DB hiccups
            logger.warning("stop-predictions: cycle failed (%s: %s)", type(exc).__name__, exc)
        await asyncio.sleep(max(1.0, STOP_PREDICTIONS_INTERVAL_SECONDS - (time.time() - started)))
