"""
Turns raw vehicle_position_snapshots rows into (features, delay_seconds)
training rows for stage 5's delay-prediction model.

The core idea: a snapshot with status=STOPPED_AT and stop_distance_percent=100
is an empirical "this vehicle was physically at this stop at this real time"
observation - that's the *actual* side of delay. The *scheduled* side comes
from static GTFS stop_times.txt, joined on (trip_id, stop_sequence), which
together uniquely identify one scheduled stop visit within one trip.

delay_seconds = actual_arrival - scheduled_arrival
  positive => vehicle was late, negative => vehicle was early.

This does NOT use BKK's own predictedArrivalTime/predictedDepartureTime
(available from their trip-details.json) - the whole point is to compute a
ground-truth label from our own collected AVL data, not relay BKK's own
estimate.

Known limitation: because we only poll every ~15s (see stage 4's
poll-interval-ms reasoning), the first STOPPED_AT sighting for a given
(trip_id, stop_sequence) can lag the true arrival moment by up to ~15s.
That's a bounded, roughly-symmetric noise source on the label - acceptable
for a first model, worth remembering if the model's error later looks
suspiciously close to that same magnitude.
"""

import csv
import os
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import psycopg2

BUDAPEST_TZ = ZoneInfo("Europe/Budapest")

# Rows beyond this are dropped as outliers, not delay. Investigated a batch
# of these by hand (2026-08-29, ~107k rows): the extreme ones aren't random
# per-stop noise - they're a whole trip shifted by one large, consistent
# offset (e.g. 5 consecutive stops all ~2h50m off, with the *relative*
# spacing between them still matching the schedule exactly). That pattern
# means the join itself (trip_id + stop_sequence) is pairing correctly -
# what's actually happening is BKK reusing a trip_id for a real-world run
# at a materially different time than our static stop_times.txt snapshot
# says (a dispatch reassignment or intraday schedule change), not a bug
# here. Only ~0.03% of rows hit this, so a simple threshold is enough -
# no need for anything smarter than "drop it" yet.
MAX_ABS_DELAY_SECONDS = 3600

GTFS_DIR = Path(__file__).resolve().parent.parent.parent / "bkk-backend" / "gtfs-data" / "raw"
STOP_TIMES_PATH = GTFS_DIR / "stop_times.txt"

OUTPUT_DIR = Path(__file__).resolve().parent.parent / "data"
OUTPUT_PATH = OUTPUT_DIR / "delay_labels.csv"

DB_CONFIG = {
    "host": "localhost",
    "port": 5432,
    "dbname": "bkk_transit",
    "user": "bkk_app",
    # Same fallback the Java side uses (application.properties) - fine for
    # local dev, not a real secret.
    "password": os.environ.get("DB_PASSWORD", "bkk_dev_pw"),
}


def fetch_actual_arrivals() -> pd.DataFrame:
    """
    One row per (trip_id, stop_sequence) actually observed, with the
    earliest recorded_at among its STOPPED_AT/100% sightings - the first
    poll that caught the vehicle already parked at the stop is the closest
    proxy we have to the true arrival instant.
    """
    query = """
        SELECT trip_id,
               stop_id,
               stop_sequence,
               route_id,
               vehicle_route_type,
               service_date,
               MIN(recorded_at) AS actual_arrival
        FROM vehicle_position_snapshots
        WHERE trip_id IS NOT NULL
          AND status = 'STOPPED_AT'
          AND stop_distance_percent = 100
        GROUP BY trip_id, stop_id, stop_sequence, route_id, vehicle_route_type, service_date
    """
    with psycopg2.connect(**DB_CONFIG) as conn:
        df = pd.read_sql_query(query, conn)

    # BKK's real-time tripId is "BKK_" + static GTFS trip_id (same prefix
    # convention already found for stopId vs stopCode back in stage 3).
    df["gtfs_trip_id"] = df["trip_id"].str.removeprefix("BKK_")
    return df


def fetch_scheduled_times(trip_ids: set[str]) -> pd.DataFrame:
    """
    Streams stop_times.txt (387MB - too big to casually load whole) in
    chunks, keeping only rows whose trip_id is one we actually observed.
    Our observed trip_id set is a tiny fraction of a full day's scheduled
    trips, so this is far cheaper than a full in-memory load.
    """
    matches = []
    usecols = ["trip_id", "stop_sequence", "arrival_time"]
    for chunk in pd.read_csv(STOP_TIMES_PATH, usecols=usecols, dtype=str, chunksize=500_000):
        chunk = chunk[chunk["trip_id"].isin(trip_ids)]
        if not chunk.empty:
            matches.append(chunk)

    if not matches:
        return pd.DataFrame(columns=usecols)

    scheduled = pd.concat(matches, ignore_index=True)
    scheduled["stop_sequence"] = scheduled["stop_sequence"].astype(int)
    return scheduled


def to_scheduled_datetime(service_date: str, arrival_time: str) -> datetime:
    """
    GTFS arrival_time is "HH:MM:SS" civil time *relative to service_date*,
    and GTFS deliberately allows hours >= 24 for trips past midnight (e.g.
    "25:10:00" for 01:10 the following day) rather than rolling the date -
    so we parse it as a plain (hours, minutes, seconds) offset added onto
    service_date's midnight, not as a wall-clock time directly.
    """
    hours, minutes, seconds = (int(part) for part in arrival_time.split(":"))
    midnight = datetime.strptime(service_date, "%Y%m%d").replace(tzinfo=BUDAPEST_TZ)
    return midnight + timedelta(hours=hours, minutes=minutes, seconds=seconds)


def build_dataset() -> pd.DataFrame:
    actual = fetch_actual_arrivals()
    print(f"Actual arrival observations: {len(actual)}")

    scheduled = fetch_scheduled_times(set(actual["gtfs_trip_id"]))
    print(f"Matching static schedule rows found: {len(scheduled)}")

    merged = actual.merge(
        scheduled,
        left_on=["gtfs_trip_id", "stop_sequence"],
        right_on=["trip_id", "stop_sequence"],
        how="inner",
        suffixes=("", "_static"),
    )
    print(f"Joined rows (have both actual and scheduled time): {len(merged)}")

    merged["scheduled_arrival"] = merged.apply(
        lambda row: to_scheduled_datetime(row["service_date"], row["arrival_time"]), axis=1
    )
    # actual_arrival comes back from psycopg2 as tz-aware (timestamptz) -
    # normalize to Europe/Budapest so the subtraction below compares two
    # timestamps in the same civil timezone rather than relying on UTC
    # offsets happening to line up.
    merged["actual_arrival"] = pd.to_datetime(merged["actual_arrival"], utc=True).dt.tz_convert(BUDAPEST_TZ)

    merged["delay_seconds"] = (
        merged["actual_arrival"] - merged["scheduled_arrival"]
    ).dt.total_seconds()

    before = len(merged)
    merged = merged[merged["delay_seconds"].abs() <= MAX_ABS_DELAY_SECONDS]
    dropped = before - len(merged)
    if dropped:
        print(f"Dropped {dropped} outlier rows (|delay| > {MAX_ABS_DELAY_SECONDS}s) - see MAX_ABS_DELAY_SECONDS comment")

    return merged[[
        "gtfs_trip_id", "route_id", "vehicle_route_type", "stop_id", "stop_sequence",
        "service_date", "scheduled_arrival", "actual_arrival", "delay_seconds",
    ]].rename(columns={"gtfs_trip_id": "trip_id"})


def main() -> None:
    dataset = build_dataset()

    OUTPUT_DIR.mkdir(exist_ok=True)
    dataset.to_csv(OUTPUT_PATH, index=False, quoting=csv.QUOTE_MINIMAL)
    print(f"Wrote {len(dataset)} labeled rows to {OUTPUT_PATH}")

    if not dataset.empty:
        print("\ndelay_seconds summary:")
        print(dataset["delay_seconds"].describe())
        print("\nBy vehicle_route_type (median delay, seconds):")
        print(dataset.groupby("vehicle_route_type")["delay_seconds"].median())


if __name__ == "__main__":
    main()
