"""
One-off Google Routes API benchmark sampler (2026-09-14) - NOT part of the
regular pipeline. A small, deliberately bounded script meant to be run
periodically (manually, or via a background loop for a while) to build up
a real comparison between our own model's live predictions and Google's
own live transit ETA, for the same real vehicles at the same moment.

Why this can't just replay already-collected historical data: Google's
Routes API answers "what's the ETA if departing at time T", which for
TRANSIT reflects routing computed *now* - there's no meaningful way to ask
it "what would you have told me three weeks ago." So this has to run
forward in time, sampling real in-progress trips, and wait for their real
outcomes to be recorded by the normal ingestion pipeline before a
comparison is possible - see google_benchmark_report.py.

Usage: python scripts/google_benchmark_sampler.py
Requires GOOGLE_MAPS_API_KEY set (with the Routes API enabled on that
project - see project notes, 2026-09-14, for the console setup steps).
"""

import os
import random
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import psycopg2
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from gtfs_schedule import GTFS_DIR, ScheduleLookup

GOOGLE_API_KEY = os.environ["GOOGLE_MAPS_API_KEY"]
BACKEND_BASE_URL = os.environ.get("BACKEND_BASE_URL", "http://localhost:8080")
CENTER_LAT, CENTER_LON, RADIUS_METERS = 47.4979, 19.0402, 25000  # matches the rest of the project

MAX_SAMPLES_PER_RUN = 15  # keeps each run's Google API usage small and predictable -
# well inside the 10,000/month free cap even run often over several days.

# Compare against a stop several stops further ahead, not the vehicle's
# immediate next one. Found empirically (2026-09-14): querying Google for
# the immediate next stop almost always got back zero transit steps -
# Google's engine correctly judges that walking one more stop is often
# faster than waiting to reboard, so it just recommends walking instead of
# riding the tracked vehicle. A further-ahead target makes continuing to
# ride clearly the faster option, which is what actually makes this a
# meaningful "same trip, same question" comparison.
LOOKAHEAD_STOPS = 5

DB_CONFIG = {
    "host": os.environ.get("DB_HOST", "localhost"),
    "port": int(os.environ.get("DB_PORT", "5432")),
    "dbname": "bkk_transit",
    "user": "bkk_app",
    "password": os.environ.get("DB_PASSWORD", "bkk_dev_pw"),
}

DDL = """
    CREATE TABLE IF NOT EXISTS google_benchmark_samples (
        id BIGSERIAL PRIMARY KEY,
        trip_id VARCHAR(255) NOT NULL,
        stop_id VARCHAR(255) NOT NULL,
        stop_sequence INTEGER NOT NULL,
        service_date VARCHAR(255) NOT NULL,
        route_id VARCHAR(255),
        our_predicted_delay_seconds DOUBLE PRECISION,
        google_predicted_delay_seconds DOUBLE PRECISION,
        sampled_at TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    CREATE INDEX IF NOT EXISTS idx_google_benchmark_lookup
        ON google_benchmark_samples (trip_id, stop_id, stop_sequence, service_date);
"""


def load_stops_static() -> pd.DataFrame:
    """
    Indexed by the *static* GTFS stop_id - the lookahead target stop is
    resolved entirely within the static schedule's own id space (via
    ScheduleLookup.stop_id_at), so there's no live-feed stop_code mismatch
    to worry about for finding its coordinates. stop_code is kept as a
    column anyway, to translate the resolved target back into the live
    "BKK_"+stop_code format our own /predict/from-vehicle expects (the
    same stopId/stopCode mismatch already found back in stage 3).
    """
    return pd.read_csv(
        GTFS_DIR / "stops.txt",
        usecols=["stop_id", "stop_code", "stop_lat", "stop_lon"],
        dtype={"stop_id": str, "stop_code": str},
    ).set_index("stop_id")


def load_route_short_names() -> dict[str, str]:
    """Keyed by the static route_id (BKK's live routeId minus its "BKK_"
    prefix) - used to check whether Google's chosen transit route is
    actually the same line as the vehicle we're tracking, not some other
    route it happened to also serve that corridor."""
    routes = pd.read_csv(GTFS_DIR / "routes.txt", usecols=["route_id", "route_short_name"], dtype=str)
    return dict(zip(routes["route_id"], routes["route_short_name"]))


def fetch_candidate_vehicles() -> list[dict]:
    """
    Only vehicles currently IN_TRANSIT_TO a known stop are useful samples -
    we need a real "predict the ETA to this specific upcoming stop"
    question to ask both models, which a STOPPED_AT vehicle (already
    there) or one missing trip/stop data can't give us. Filtered to
    "BKK_"-prefixed trips upfront (excludes Volánbusz/MÁV-START vehicles
    BKK's live feed also surfaces - see the 2026-08-28 route-name
    investigation) rather than letting those waste a slot in the sample
    only to get skipped later for having no static-schedule match.
    """
    resp = requests.get(
        f"{BACKEND_BASE_URL}/api/vehicles",
        params={"lat": CENTER_LAT, "lon": CENTER_LON, "radius": RADIUS_METERS},
        timeout=15,
    )
    resp.raise_for_status()
    vehicles = resp.json()
    return [
        v for v in vehicles
        if (v.get("tripId") or "").startswith("BKK_") and v.get("stopId") and v.get("stopSequence") is not None
        and v.get("serviceDate") and v.get("routeId") and v.get("status") == "IN_TRANSIT_TO"
    ]


def query_google_transit_eta(origin_lat: float, origin_lon: float, dest_lat: float, dest_lon: float) -> list[dict]:
    """
    Returns the transitDetails of every transit leg/step in Google's chosen
    route - usually one, sometimes a transfer chain (bus then metro, seen
    in the initial test call). The caller matches against the specific
    route we're tracking rather than assuming the first/only step is it -
    Google is free to suggest a completely different way to get there.
    """
    departure_time = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    response = requests.post(
        "https://routes.googleapis.com/directions/v2:computeRoutes",
        headers={
            "Content-Type": "application/json",
            "X-Goog-Api-Key": GOOGLE_API_KEY,
            "X-Goog-FieldMask": "routes.legs.steps.transitDetails",
        },
        json={
            "origin": {"location": {"latLng": {"latitude": origin_lat, "longitude": origin_lon}}},
            "destination": {"location": {"latLng": {"latitude": dest_lat, "longitude": dest_lon}}},
            "travelMode": "TRANSIT",
            "departureTime": departure_time,
        },
        timeout=15,
    )
    response.raise_for_status()
    data = response.json()
    if not data.get("routes"):
        return []
    steps = data["routes"][0]["legs"][0]["steps"]
    return [s["transitDetails"] for s in steps if "transitDetails" in s]


def fetch_our_prediction(trip_id: str, route_id: str, stop_id: str, vehicle_route_type: str,
                          stop_sequence: int, service_date: str, deviated: bool) -> float | None:
    """Goes through the real backend endpoint (not calling the sidecar's
    model directly) so this is exactly the same prediction a map click
    would get - no risk of the benchmark quietly testing a different code
    path than what's actually served."""
    resp = requests.post(
        f"{BACKEND_BASE_URL}/api/vehicles/delay-prediction",
        json={
            "tripId": trip_id, "routeId": route_id, "stopId": stop_id,
            "vehicleRouteType": vehicle_route_type, "stopSequence": stop_sequence,
            "serviceDate": service_date, "deviated": deviated,
        },
        timeout=15,
    )
    resp.raise_for_status()
    result = resp.json()
    return result["predictedDelaySeconds"] if result.get("available") else None


def resolve_lookahead_target(
    schedule_lookup: ScheduleLookup, stops_static: pd.DataFrame, gtfs_trip_id: str, current_stop_sequence: int
) -> tuple[int, str, tuple[float, float]] | None:
    """
    Finds a stop LOOKAHEAD_STOPS further down this same trip - falls back
    to the immediate next stop if the trip doesn't run that far (e.g.
    already near its terminus), which reintroduces the walking-preferred
    short-hop problem for that minority of cases but is still better than
    skipping them outright. Returns (target_stop_sequence, target_stop_id
    in live "BKK_"+stop_code format, (lat, lon)), or None if neither
    candidate resolves (off the static schedule, or a stop_code-less stop).
    """
    for target_stop_sequence in (current_stop_sequence + LOOKAHEAD_STOPS, current_stop_sequence + 1):
        target_stop_id = schedule_lookup.stop_id_at(gtfs_trip_id, target_stop_sequence)
        if target_stop_id is None or target_stop_id not in stops_static.index:
            continue
        row = stops_static.loc[target_stop_id]
        if pd.isna(row["stop_code"]):
            continue
        return target_stop_sequence, f"BKK_{row['stop_code']}", (row["stop_lat"], row["stop_lon"])
    return None


def main() -> None:
    stops_static = load_stops_static()
    route_short_names = load_route_short_names()
    schedule_lookup = ScheduleLookup()

    candidates = fetch_candidate_vehicles()
    print(f"{len(candidates)} candidate in-transit vehicles available")
    # Randomized, not just the first N - /api/vehicles returns them in a
    # fairly consistent order, so always taking the first N would sample
    # roughly the same handful of vehicles/routes on every run instead of
    # building a diverse cross-section over time.
    sample = random.sample(candidates, min(MAX_SAMPLES_PER_RUN, len(candidates)))

    with psycopg2.connect(**DB_CONFIG) as conn, conn.cursor() as cur:
        cur.execute(DDL)
        conn.commit()

        matched, skipped = 0, 0
        for vehicle in sample:
            gtfs_trip_id = vehicle["tripId"].removeprefix("BKK_")
            gtfs_route_id = vehicle["routeId"].removeprefix("BKK_")
            our_route_short_name = route_short_names.get(gtfs_route_id)
            if our_route_short_name is None:
                skipped += 1
                continue

            target = resolve_lookahead_target(schedule_lookup, stops_static, gtfs_trip_id, vehicle["stopSequence"])
            if target is None:
                skipped += 1
                continue
            target_stop_sequence, target_stop_id, dest_coords = target

            transit_steps = query_google_transit_eta(vehicle["lat"], vehicle["lon"], *dest_coords)
            matching_step = next(
                (s for s in transit_steps if s.get("transitLine", {}).get("nameShort") == our_route_short_name),
                None,
            )
            if matching_step is None:
                skipped += 1  # Google picked a different route/line (or walking) - not a comparable sample.
                continue

            scheduled = schedule_lookup.scheduled_arrival(gtfs_trip_id, target_stop_sequence, vehicle["serviceDate"])
            if scheduled is None:
                skipped += 1
                continue

            google_arrival = datetime.fromisoformat(
                matching_step["stopDetails"]["arrivalTime"].replace("Z", "+00:00")
            )
            google_predicted_delay = (google_arrival - scheduled).total_seconds()
            # Our own model's prediction for the SAME lookahead target stop
            # - not the vehicle's immediate next one, since that's what
            # we're actually asking Google about here.
            our_predicted_delay = fetch_our_prediction(
                vehicle["tripId"], vehicle["routeId"], target_stop_id, vehicle["vehicleRouteType"],
                target_stop_sequence, vehicle["serviceDate"], vehicle.get("deviated", False),
            )

            cur.execute(
                """INSERT INTO google_benchmark_samples
                   (trip_id, stop_id, stop_sequence, service_date, route_id,
                    our_predicted_delay_seconds, google_predicted_delay_seconds)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                (vehicle["tripId"], target_stop_id, target_stop_sequence, vehicle["serviceDate"],
                 vehicle["routeId"], our_predicted_delay, google_predicted_delay),
            )
            matched += 1
        conn.commit()

    print(f"Sampled {matched} comparable predictions, skipped {skipped} (no route match or missing data)")


if __name__ == "__main__":
    main()
