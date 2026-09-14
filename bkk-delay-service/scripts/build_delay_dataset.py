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
import sys
from pathlib import Path

import pandas as pd
import psycopg2

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from gtfs_schedule import BUDAPEST_TZ, STOP_TIMES_PATH, to_scheduled_datetime
from weather import fetch_historical_hourly

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
    One row per (trip_id, stop_sequence) actually observed, from the
    earliest STOPPED_AT/100% sighting - the first poll that caught the
    vehicle already parked at the stop is the closest proxy we have to the
    true arrival instant. DISTINCT ON (rather than GROUP BY + MIN) so
    `deviated` also comes from that exact earliest row, not an ambiguous
    aggregate over however many polls caught the vehicle still parked.
    """
    query = """
        SELECT DISTINCT ON (trip_id, stop_id, stop_sequence, route_id, vehicle_route_type, service_date)
               trip_id,
               stop_id,
               stop_sequence,
               route_id,
               vehicle_route_type,
               service_date,
               recorded_at AS actual_arrival,
               deviated
        FROM vehicle_position_snapshots
        WHERE trip_id IS NOT NULL
          AND status = 'STOPPED_AT'
          AND stop_distance_percent = 100
        ORDER BY trip_id, stop_id, stop_sequence, route_id, vehicle_route_type, service_date, recorded_at ASC
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

    # upstream_delay_seconds (added 2026-09-12): this trip's own delay at the
    # most recent *earlier* stop we actually have an observation for - real
    # delay propagates (a bus running 4 minutes late tends to still be
    # running late a few stops on), and unlike route/stop/time-of-day
    # averages this is a live, per-vehicle signal rather than a historical
    # one. Computed after outlier filtering so a dropped bad row doesn't
    # leak a distorted value into its neighbor's feature.
    #
    # Grouped by (trip_id, service_date) - not trip_id alone - since the
    # same trip_id runs again on every subsequent day. shift(1) on the
    # sorted-by-stop_sequence group gives "the previous stop we observed",
    # which may skip a stop_sequence or two if that stop had no STOPPED_AT
    # sighting - correct, since that's exactly what would be knowable live
    # too (see main.py's /predict/from-vehicle, which runs the equivalent
    # query against Postgres directly).
    #
    # has_upstream_delay flags rows with no earlier observation yet (a
    # trip's first observed stop) so the model can distinguish "known to be
    # on-time so far" from "no live reading available" instead of silently
    # treating both as delay=0.
    merged = merged.sort_values(["gtfs_trip_id", "service_date", "stop_sequence"])
    merged["upstream_delay_seconds"] = merged.groupby(
        ["gtfs_trip_id", "service_date"]
    )["delay_seconds"].shift(1)
    merged["has_upstream_delay"] = merged["upstream_delay_seconds"].notna().astype(int)
    merged["upstream_delay_seconds"] = merged["upstream_delay_seconds"].fillna(0.0)

    # route_recent_delay_seconds/has_route_recent_delay (added 2026-09-14):
    # unlike upstream_delay_seconds (this SAME trip's own recent history),
    # this is "how are OTHER vehicles on this route doing right now" - a
    # live signal that exists even for a trip's very first observed stop,
    # exactly the ~7% of rows (has_upstream_delay=0) upstream_delay can't
    # help with. Computed via 10-minute time buckets per route rather than
    # a per-row nearest-other-vehicle search (which would be far slower
    # over 6M+ rows) - each row looks at the *previous* bucket's average
    # delay for its route, so it only ever uses genuinely earlier
    # observations, never data from its own time window.
    merged["time_bucket"] = merged["actual_arrival"].dt.floor("10min")
    bucket_stats = (
        merged.groupby(["route_id", "time_bucket"])["delay_seconds"]
        .agg(["mean", "count"])
        .reset_index()
        .sort_values(["route_id", "time_bucket"])
    )
    bucket_stats["route_recent_delay_seconds"] = bucket_stats.groupby("route_id")["mean"].shift(1)
    bucket_stats["route_recent_delay_count"] = bucket_stats.groupby("route_id")["count"].shift(1)

    merged = merged.merge(
        bucket_stats[["route_id", "time_bucket", "route_recent_delay_seconds", "route_recent_delay_count"]],
        on=["route_id", "time_bucket"],
        how="left",
    )
    merged["has_route_recent_delay"] = (merged["route_recent_delay_count"].fillna(0) > 0).astype(int)
    merged["route_recent_delay_seconds"] = merged["route_recent_delay_seconds"].fillna(0.0)

    # Weather (added 2026-09-14): backfilled once for the whole date range
    # this dataset spans, joined by hour against scheduled_arrival (what the
    # weather was like around when this trip was *supposed* to happen, not
    # when it actually did - the schedule is what a real prediction would
    # know in advance, so that's the honest join key, same reasoning as
    # hour/day_of_week being derived from scheduled_arrival in
    # train_model.py). One historical API call for the whole range rather
    # than one per row - Open-Meteo's archive endpoint is a single request
    # for an entire date range, not something worth calling per-row.
    weather = fetch_historical_hourly(
        merged["scheduled_arrival"].min().date(),
        merged["scheduled_arrival"].max().date(),
    )
    merged["hour_bucket"] = merged["scheduled_arrival"].dt.floor("h")
    merged = merged.merge(weather, on="hour_bucket", how="left")
    missing_weather = merged["temperature_2m"].isna().sum()
    if missing_weather:
        print(f"Warning: {missing_weather} rows have no matching weather hour (schedule outside the fetched range?)")

    # deviated (added 2026-09-14): BKK's own "this vehicle is off its normal
    # route" flag - already collected since the 2026-08-28 field audit but
    # never actually used as a model feature until now. Missing/null (rare)
    # treated as "not known to be deviated" rather than dropped.
    merged["deviated"] = merged["deviated"].fillna(False).astype(int)

    return merged[[
        "gtfs_trip_id", "route_id", "vehicle_route_type", "stop_id", "stop_sequence",
        "service_date", "scheduled_arrival", "actual_arrival", "delay_seconds",
        "upstream_delay_seconds", "has_upstream_delay",
        "route_recent_delay_seconds", "has_route_recent_delay",
        "temperature_2m", "precipitation", "wind_speed_10m",
        "deviated",
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
