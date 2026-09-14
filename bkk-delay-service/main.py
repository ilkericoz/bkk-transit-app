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

import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import psycopg2
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
        conn.commit()

    yield


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
    # Optional, same reasoning: this endpoint has no route_id-wide live
    # context to look up on its own. Defaults to "no reading" like a quiet
    # route with nothing else currently running.
    route_recent_delay_seconds: float = 0.0
    has_route_recent_delay: bool = False
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
    None means no earlier stop has been observed yet (a trip's first stop).

    Opens a fresh connection per call rather than pooling - this endpoint
    is click-triggered from the map (see app.js), not a hot path, so the
    extra ~10-20ms of connection setup isn't worth the added complexity of
    a pool at this project's current scale.
    """
    query = """
        SELECT stop_sequence, MIN(recorded_at) AS recorded_at
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


ROUTE_RECENT_WINDOW_MINUTES = 10  # matches build_delay_dataset.py's bucket size


def fetch_route_recent_delay(route_id: str, exclude_trip_id: str) -> tuple[float, bool]:
    """
    How OTHER vehicles on this same route have been running in roughly the
    last ROUTE_RECENT_WINDOW_MINUTES - the live equivalent of
    build_delay_dataset.py's route_recent_delay_seconds bucket average, and
    the one that actually helps a trip's very first observed stop, where
    fetch_upstream_delay (this trip's OWN history) has nothing to report
    yet. exclude_trip_id keeps this genuinely about *other* vehicles rather
    than accidentally re-reading the same trip's own upstream stop.
    """
    window_end = datetime.now(BUDAPEST_TZ)
    window_start = window_end - timedelta(minutes=ROUTE_RECENT_WINDOW_MINUTES)

    query = """
        SELECT trip_id, stop_sequence, service_date, recorded_at
        FROM vehicle_position_snapshots
        WHERE route_id = %s
          AND status = 'STOPPED_AT'
          AND stop_distance_percent = 100
          AND recorded_at >= %s AND recorded_at < %s
          AND trip_id != %s
    """
    with psycopg2.connect(**DB_CONFIG) as conn, conn.cursor() as cur:
        cur.execute(query, (route_id, window_start, window_end, exclude_trip_id))
        rows = cur.fetchall()

    delays = []
    for trip_id, stop_sequence, service_date, recorded_at in rows:
        scheduled = schedule_lookup.scheduled_arrival(trip_id.removeprefix("BKK_"), stop_sequence, service_date)
        if scheduled is not None:
            delays.append((recorded_at.astimezone(BUDAPEST_TZ) - scheduled).total_seconds())

    if not delays:
        return 0.0, False
    return sum(delays) / len(delays), True


def log_prediction(
    trip_id: str, route_id: str, stop_id: str, vehicle_route_type: str,
    stop_sequence: int, service_date: str, predicted_delay_seconds: float,
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
             service_date, predicted_delay_seconds, model_type)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
    """
    with psycopg2.connect(**DB_CONFIG) as conn, conn.cursor() as cur:
        cur.execute(query, (
            trip_id, route_id, stop_id, vehicle_route_type, stop_sequence,
            service_date, predicted_delay_seconds, model.name if model else None,
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
    recent: list[ScoreboardEntry]


SCOREBOARD_MAE_WINDOW = 50  # how many recent reconciled predictions the headline MAE is averaged over
SCOREBOARD_RECENT_DISPLAY = 10  # how many of those are actually shown in the list


@app.get("/scoreboard", response_model=Scoreboard)
def scoreboard() -> Scoreboard:
    """
    Reconciles logged predictions against what actually happened, computed
    on read rather than via a background job - prediction_log's volume
    (bounded by how often someone clicks a vehicle on the map) is small
    enough that there's no real cost to just joining at request time,
    which is a lot simpler than maintaining a separate reconciliation
    process. DISTINCT ON picks the earliest STOPPED_AT/100% sighting per
    logged prediction, same "first poll that caught it arrived" definition
    of actual arrival used everywhere else in this project (see
    build_delay_dataset.py's fetch_actual_arrivals).
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
        ORDER BY pl.id, vs.recorded_at ASC
    """
    with psycopg2.connect(**DB_CONFIG) as conn, conn.cursor() as cur:
        cur.execute(query)
        rows = cur.fetchall()

    entries = []
    for (_id, trip_id, route_id, vehicle_route_type, stop_sequence, service_date,
         predicted_delay_seconds, predicted_at, actual_recorded_at) in rows:
        scheduled = schedule_lookup.scheduled_arrival(trip_id.removeprefix("BKK_"), stop_sequence, service_date)
        if scheduled is None:
            continue
        actual_delay_seconds = (actual_recorded_at.astimezone(BUDAPEST_TZ) - scheduled).total_seconds()
        entries.append(ScoreboardEntry(
            route_id=route_id,
            vehicle_route_type=vehicle_route_type,
            predicted_delay_seconds=predicted_delay_seconds,
            actual_delay_seconds=actual_delay_seconds,
            error_seconds=abs(predicted_delay_seconds - actual_delay_seconds),
            predicted_at=predicted_at,
        ))

    entries.sort(key=lambda e: e.predicted_at, reverse=True)
    for_mae = entries[:SCOREBOARD_MAE_WINDOW]
    mean_absolute_error = (
        sum(e.error_seconds for e in for_mae) / len(for_mae) if for_mae else None
    )
    return Scoreboard(
        reconciled_count=len(entries),
        mean_absolute_error_seconds=mean_absolute_error,
        recent=entries[:SCOREBOARD_RECENT_DISPLAY],
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
        route_recent_delay_seconds=request.route_recent_delay_seconds,
        has_route_recent_delay=int(request.has_route_recent_delay),
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
    route_recent_delay_seconds, has_route_recent_delay = fetch_route_recent_delay(
        request.route_id, request.trip_id
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
        route_recent_delay_seconds=route_recent_delay_seconds,
        has_route_recent_delay=int(has_route_recent_delay),
        temperature_2m=weather["temperature_2m"],
        precipitation=weather["precipitation"],
        wind_speed_10m=weather["wind_speed_10m"],
        deviated=int(request.deviated),
    )
    log_prediction(
        trip_id=request.trip_id, route_id=request.route_id, stop_id=request.stop_id,
        vehicle_route_type=request.vehicle_route_type, stop_sequence=request.stop_sequence,
        service_date=request.service_date, predicted_delay_seconds=predicted_delay,
    )
    return DelayPredictionResponse(predicted_delay_seconds=predicted_delay, last_confirmed_delay=upstream)
