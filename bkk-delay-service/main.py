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


@asynccontextmanager
async def lifespan(app: FastAPI):
    global model, schedule_lookup
    if not MODEL_PATH.exists():
        raise RuntimeError(f"No trained model at {MODEL_PATH} - run scripts/train_model.py first.")
    model = BaseDelayModel.load(MODEL_PATH)
    schedule_lookup = ScheduleLookup()
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


class DelayPredictionResponse(BaseModel):
    predicted_delay_seconds: float


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


def fetch_upstream_delay(gtfs_trip_id: str, stop_sequence: int, service_date: str) -> tuple[float, bool]:
    """
    This trip's own delay at the most recent earlier stop actually observed
    today - the live-lookup equivalent of build_delay_dataset.py's batch
    upstream_delay_seconds (a groupby+shift there; a single targeted query
    here, since a live request only ever needs one trip's answer). See
    delay_model.py's NUMERIC_FEATURES comment for why this feature exists.

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
        return 0.0, False  # this trip's first observed stop - nothing upstream yet.

    found_stop_sequence, recorded_at = row
    scheduled = schedule_lookup.scheduled_arrival(gtfs_trip_id, found_stop_sequence, service_date)
    if scheduled is None:
        return 0.0, False

    recorded_at = recorded_at.astimezone(BUDAPEST_TZ)
    return (recorded_at - scheduled).total_seconds(), True


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
    return DelayPredictionResponse(predicted_delay_seconds=predicted_delay)


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

    upstream_delay_seconds, has_upstream_delay = fetch_upstream_delay(
        gtfs_trip_id, request.stop_sequence, request.service_date
    )
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
        upstream_delay_seconds=upstream_delay_seconds,
        has_upstream_delay=int(has_upstream_delay),
        route_recent_delay_seconds=route_recent_delay_seconds,
        has_route_recent_delay=int(has_route_recent_delay),
        temperature_2m=weather["temperature_2m"],
        precipitation=weather["precipitation"],
        wind_speed_10m=weather["wind_speed_10m"],
        deviated=int(request.deviated),
    )
    return DelayPredictionResponse(predicted_delay_seconds=predicted_delay)
