"""
Reconciles prediction_log's auto-sampled rows (source='auto', see
main.py's auto_sample_loop) against what actually happened, and reports
our model vs. BKK's own GTFS-RT TripUpdates prediction head to head -
the comparison flagged as not-yet-built when BKK-prediction logging was
added (2026-09-21).

Unlike the Google Routes API benchmark (google_benchmark_report.py),
which has to compare elapsed travel time because Google answers "a fresh
rider boarding now," BKK's trip-details.json is queried with this exact
tripId/date, so bkk_predicted_delay_seconds is already anchored to the
same trip instance we're tracking - a direct absolute-delay comparison
is valid, no travel-time reframing needed.

Read-only: does not touch prediction_log, vehicle_position_snapshots, or
/scoreboard's response shape (the Java side parses that separately).

Run any time after auto-sampling has been running a while (needs real
time to pass for sampled vehicles to actually reach their target stop).

Updated 2026-09-24 to match /scoreboard's current rules (predictions for a
stop the vehicle had already reached are excluded; first-stop predictions
made after the switch are scored on departure) and to report each
production model period separately - the model changed twice on
2026-09-23/24, and mixing periods would blur which model is being judged.
"""

import os
import sys
from pathlib import Path

import pandas as pd
import psycopg2

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from gtfs_schedule import BUDAPEST_TZ, ScheduleLookup

DB_CONFIG = {
    "host": os.environ.get("DB_HOST", "localhost"),
    "port": int(os.environ.get("DB_PORT", "5432")),
    "dbname": "bkk_transit",
    "user": "bkk_app",
    "password": os.environ.get("DB_PASSWORD", "bkk_dev_pw"),
}

# Same outlier guard as main.py's /scoreboard (MAX_ABS_RECONCILED_DELAY_SECONDS)
# and the same reasoning: BKK can dispatch a trip_id more than once a day, so
# an unbounded "next matching STOPPED_AT snapshot" can occasionally pair a
# prediction with an unrelated later/earlier real visit rather than the one
# actually predicted against. Chosen from evidence on 2026-09-16, not guessed.
MAX_ABS_RECONCILED_DELAY_SECONDS = 1500

# Same as main.py's FIRST_STOP_DEPARTURE_SINCE - when first stops started
# being predicted (and scored) by departure instead of arrival.
FIRST_STOP_DEPARTURE_SINCE = "2026-09-23 13:56:30+02:00"

# Production model periods, by deploy time (see the README changelog).
MODEL_PERIODS = [
    ("GBT, arrival labels", None, "2026-09-23 13:56:30+02:00"),
    ("linear, departure labels", "2026-09-23 13:56:30+02:00", "2026-09-24 02:08:40+02:00"),
    ("linear, without route's recent delay", "2026-09-24 02:08:40+02:00", "2026-09-24 16:52:06+02:00"),
    ("linear + vehicle ahead", "2026-09-24 16:52:06+02:00", None),
]

# Same reconciliation as main.py's /scoreboard: the earliest STOPPED_AT/100%
# sighting after the prediction (the arrival) - or, for a first-stop
# prediction made since FIRST_STOP_DEPARTURE_SINCE, the latest one (the
# departure) - skipping predictions for a stop already reached.
RECONCILE_QUERY = """
    SELECT DISTINCT ON (pl.id)
           pl.id, pl.trip_id, pl.route_id, pl.vehicle_route_type, pl.stop_sequence,
           pl.service_date, pl.predicted_delay_seconds, pl.bkk_predicted_delay_seconds,
           pl.predicted_at, vs.recorded_at AS actual_recorded_at
    FROM prediction_log pl
    JOIN vehicle_position_snapshots vs
      ON vs.trip_id = pl.trip_id
     AND vs.stop_id = pl.stop_id
     AND vs.stop_sequence = pl.stop_sequence
     AND vs.service_date = pl.service_date
     AND vs.status = 'STOPPED_AT'
     AND vs.stop_distance_percent = 100
     AND vs.recorded_at > pl.predicted_at
    WHERE pl.source = 'auto'
      AND pl.bkk_predicted_delay_seconds IS NOT NULL
      AND ((pl.stop_sequence = 1 AND pl.predicted_at >= %(departure_since)s) OR NOT EXISTS (
          SELECT 1 FROM vehicle_position_snapshots prior
          WHERE prior.trip_id = pl.trip_id AND prior.stop_id = pl.stop_id
            AND prior.stop_sequence = pl.stop_sequence AND prior.service_date = pl.service_date
            AND prior.status = 'STOPPED_AT' AND prior.stop_distance_percent = 100
            AND prior.recorded_at <= pl.predicted_at
      ))
    ORDER BY pl.id,
             CASE WHEN pl.stop_sequence = 1 AND pl.predicted_at >= %(departure_since)s
                  THEN vs.recorded_at END DESC NULLS LAST,
             vs.recorded_at ASC
"""


def main() -> None:
    schedule_lookup = ScheduleLookup()

    with psycopg2.connect(**DB_CONFIG) as conn:
        total_auto = pd.read_sql_query(
            "SELECT count(*) AS n FROM prediction_log WHERE source = 'auto'", conn
        )["n"].iloc[0]
        total_with_bkk = pd.read_sql_query(
            "SELECT count(*) AS n FROM prediction_log WHERE source = 'auto' "
            "AND bkk_predicted_delay_seconds IS NOT NULL", conn
        )["n"].iloc[0]
        reconciled = pd.read_sql_query(RECONCILE_QUERY, conn, params={"departure_since": FIRST_STOP_DEPARTURE_SINCE})

    print(f"{total_auto} auto-sampled rows logged, {total_with_bkk} carry a BKK prediction, "
          f"{len(reconciled)} reconciled so far (the rest haven't reached their target stop yet)")
    if reconciled.empty:
        print("Nothing to compare yet - let auto_sample_loop run longer and rerun this script.")
        return

    # Same double-count guard as /scoreboard: keep only the most recent
    # prediction per (trip_id, stop_sequence, service_date) target, in case
    # two auto-sample cycles happened to pick the same vehicle before it
    # reached its target stop.
    reconciled = reconciled.sort_values("predicted_at", ascending=False)
    reconciled = reconciled.drop_duplicates(subset=["trip_id", "stop_sequence", "service_date"], keep="first")

    def actual_delay(row) -> float | None:
        scheduled = schedule_lookup.scheduled_arrival(
            row["trip_id"].removeprefix("BKK_"), row["stop_sequence"], row["service_date"]
        )
        if scheduled is None:
            return None
        return (row["actual_recorded_at"].astimezone(BUDAPEST_TZ) - scheduled).total_seconds()

    reconciled["actual_delay_seconds"] = reconciled.apply(actual_delay, axis=1)
    reconciled = reconciled.dropna(subset=["actual_delay_seconds"])
    reconciled = reconciled[reconciled["actual_delay_seconds"].abs() <= MAX_ABS_RECONCILED_DELAY_SECONDS]

    reconciled["our_error"] = (reconciled["predicted_delay_seconds"] - reconciled["actual_delay_seconds"]).abs()
    reconciled["bkk_error"] = (reconciled["bkk_predicted_delay_seconds"] - reconciled["actual_delay_seconds"]).abs()
    reconciled["we_won"] = reconciled["our_error"] < reconciled["bkk_error"]

    predicted_at = pd.to_datetime(reconciled["predicted_at"], utc=True)
    for label, start, end in MODEL_PERIODS:
        in_period = pd.Series(True, index=reconciled.index)
        if start:
            in_period &= predicted_at >= pd.Timestamp(start)
        if end:
            in_period &= predicted_at < pd.Timestamp(end)
        period = reconciled[in_period]
        print(f"\n=== {label} ({start or 'start'} -> {end or 'now'}) ===")
        if period.empty:
            print("  no reconciled predictions yet")
            continue
        print(f"  {len(period)} comparable reconciled predictions (absolute delay vs. schedule):")
        for who, col in (("Our model", "our_error"), ("BKK's own prediction", "bkk_error")):
            err = period[col]
            print(f"  {who:22s} MAE {err.mean():6.1f}s  median {err.median():5.1f}s  within 60s {(err <= 60).mean() * 100:3.0f}%")
        print(f"  We were closer in {period['we_won'].sum()}/{len(period)} ({period['we_won'].mean() * 100:.0f}%)")
        by_type = period.groupby("vehicle_route_type").agg(
            n=("our_error", "size"),
            our_mae=("our_error", "mean"),
            bkk_mae=("bkk_error", "mean"),
            we_won_rate=("we_won", "mean"),
        ).sort_values("n", ascending=False)
        by_type["we_won_rate"] = (by_type["we_won_rate"] * 100).round(0)
        print(by_type.to_string(float_format=lambda x: f"{x:.1f}"))


if __name__ == "__main__":
    main()
