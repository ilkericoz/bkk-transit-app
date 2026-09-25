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

Except at a trip's FIRST stop (stop_sequence 1 - true for every trip in
BKK's stop_times.txt, checked 2026-09-23), where the label is the DEPARTURE:
the LAST STOPPED_AT/100% sighting there. Vehicles turn up at the first stop
early and wait (mean first-sighting "delay" -238 s, 90% early), so the
first sighting measures when the vehicle arrived to wait, not when the trip
started - and a rider at a first stop only cares when it leaves. The
scheduled side needs no change: arrival_time == departure_time at every
first stop in the static schedule (also checked 2026-09-23). The
label_event column says which event each row's label measures.

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
from gtfs_schedule import BUDAPEST_TZ, read_stop_times, to_scheduled_datetime
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

    last_seen_at is the LATEST sighting of the same visit - the departure
    proxy, used only for first stops (see the module docstring). It lags
    the real departure by up to one poll (~15s), mirroring the first
    sighting's lag behind the real arrival.
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
               MAX(recorded_at) OVER (
                   PARTITION BY trip_id, stop_id, stop_sequence, route_id, vehicle_route_type, service_date
               ) AS last_seen_at,
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
    # Current timetable plus retired trips it no longer has (see
    # gtfs_schedule.RETIRED_STOP_TIMES_PATH) - past days must stay resolvable.
    return read_stop_times(["trip_id", "stop_sequence", "arrival_time"], trip_ids=trip_ids)


# A vehicle ahead that passed longer ago than this isn't "the bus in front"
# any more (e.g. the last run of the previous evening), so it's treated as
# no vehicle ahead.
VEHICLE_AHEAD_MAX_MINUTES = 60


def add_vehicle_ahead(merged: pd.DataFrame) -> pd.DataFrame:
    """
    Vehicle-ahead features (added 2026-09-24, Phase 1 candidate): the
    previous vehicle of the SAME route at the SAME stop (BKK stop_ids are
    per direction), counted only if it got there before this trip's
    upstream stop was confirmed (upstream_time) - the moment a live
    prediction for this stop would be made. The live lookup must use the
    same reference moment, not "now", or this becomes another train/serve
    mismatch like the removed route_recent_delay.

    ahead_delay_seconds    - that vehicle's delay at this very stop
    minutes_since_ahead    - how long before upstream_time it got there
    scheduled_gap_minutes  - timetable gap between the two trips here
    has_vehicle_ahead      - 0 when none within VEHICLE_AHEAD_MAX_MINUTES,
                             or when this row has no upstream stop yet

    Bunching ("am I closer to the bus ahead than planned?") is exactly
    upstream_delay_seconds - ahead_delay_seconds, so it needs no column.
    """
    arrivals = (
        merged[["route_id", "stop_id", "actual_arrival", "scheduled_arrival", "gtfs_trip_id", "delay_seconds"]]
        .rename(columns={"actual_arrival": "ahead_arrival", "scheduled_arrival": "ahead_scheduled",
                         "gtfs_trip_id": "ahead_trip_id", "delay_seconds": "ahead_delay_seconds"})
        .sort_values("ahead_arrival")
    )
    has_ref = merged["upstream_time"].notna()
    left = merged.loc[has_ref, ["route_id", "stop_id", "upstream_time", "gtfs_trip_id", "scheduled_arrival"]]
    left = left.reset_index().sort_values("upstream_time")
    found = pd.merge_asof(
        left, arrivals, left_on="upstream_time", right_on="ahead_arrival",
        by=["route_id", "stop_id"], direction="backward", allow_exact_matches=False,
    ).set_index("index")

    minutes_since = (found["upstream_time"] - found["ahead_arrival"]).dt.total_seconds() / 60
    valid = (
        found["ahead_arrival"].notna()
        & (found["ahead_trip_id"] != found["gtfs_trip_id"])  # a looping trip's own earlier visit
        & (minutes_since <= VEHICLE_AHEAD_MAX_MINUTES)
    )
    merged["has_vehicle_ahead"] = 0
    merged["ahead_delay_seconds"] = 0.0
    merged["minutes_since_ahead"] = 0.0
    merged["scheduled_gap_minutes"] = 0.0
    idx = found.index[valid]
    merged.loc[idx, "has_vehicle_ahead"] = 1
    merged.loc[idx, "ahead_delay_seconds"] = found.loc[idx, "ahead_delay_seconds"]
    merged.loc[idx, "minutes_since_ahead"] = minutes_since[valid]
    merged.loc[idx, "scheduled_gap_minutes"] = (
        (found.loc[idx, "scheduled_arrival"] - found.loc[idx, "ahead_scheduled"]).dt.total_seconds() / 60
    )
    return merged


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
    merged["last_seen_at"] = pd.to_datetime(merged["last_seen_at"], utc=True).dt.tz_convert(BUDAPEST_TZ)

    # First stops are labelled by departure, not arrival - see the module
    # docstring. actual_arrival keeps its name so every downstream reader
    # (train_model.py, the thesis scripts) works unchanged; label_event
    # records which event it actually is.
    first_stop = merged["stop_sequence"] == 1
    merged.loc[first_stop, "actual_arrival"] = merged.loc[first_stop, "last_seen_at"]
    merged["label_event"] = first_stop.map({True: "departure", False: "arrival"})

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
    # When this trip's upstream stop was confirmed = the moment a live
    # prediction for this stop could have been made (see add_vehicle_ahead).
    merged["upstream_time"] = merged.groupby(["gtfs_trip_id", "service_date"])["actual_arrival"].shift(1)
    merged = add_vehicle_ahead(merged)

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
        "deviated", "label_event",
        "has_vehicle_ahead", "ahead_delay_seconds", "minutes_since_ahead", "scheduled_gap_minutes",
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
