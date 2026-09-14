"""
Reconciles google_benchmark_samples (see google_benchmark_sampler.py)
against what actually happened, and reports our model vs. Google's Routes
API head to head - the real point of the whole exercise: not "do we beat
our own dumb baseline" (already covered by train_model.py's walk-forward
comparison), but "are we competitive with what a rider's own Google Maps
would have told them," a genuinely external, credible benchmark.

Run any time after google_benchmark_sampler.py has been run a few times
and enough real time has passed for the sampled trips to actually reach
their target stops.
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

# Same "earliest STOPPED_AT/100% sighting" definition of actual arrival
# used everywhere else in this project (build_delay_dataset.py,
# main.py's /scoreboard) - DISTINCT ON picks it directly rather than a
# separate GROUP BY + MIN pass.
RECONCILE_QUERY = """
    SELECT DISTINCT ON (gbs.id)
           gbs.id, gbs.trip_id, gbs.stop_sequence, gbs.service_date,
           gbs.route_id, gbs.our_predicted_delay_seconds, gbs.google_predicted_delay_seconds,
           vs.recorded_at AS actual_recorded_at
    FROM google_benchmark_samples gbs
    JOIN vehicle_position_snapshots vs
      ON vs.trip_id = gbs.trip_id
     AND vs.stop_id = gbs.stop_id
     AND vs.stop_sequence = gbs.stop_sequence
     AND vs.service_date = gbs.service_date
     AND vs.status = 'STOPPED_AT'
     AND vs.stop_distance_percent = 100
    ORDER BY gbs.id, vs.recorded_at ASC
"""


def main() -> None:
    schedule_lookup = ScheduleLookup()

    with psycopg2.connect(**DB_CONFIG) as conn:
        total_samples = pd.read_sql_query("SELECT count(*) AS n FROM google_benchmark_samples", conn)["n"].iloc[0]
        reconciled = pd.read_sql_query(RECONCILE_QUERY, conn)

    print(f"{total_samples} total samples logged, {len(reconciled)} reconciled so far "
          f"(the rest haven't reached their target stop yet)")
    if reconciled.empty:
        print("Nothing to compare yet - run google_benchmark_sampler.py again later once more trips complete.")
        return

    def actual_delay(row) -> float | None:
        scheduled = schedule_lookup.scheduled_arrival(
            row["trip_id"].removeprefix("BKK_"), row["stop_sequence"], row["service_date"]
        )
        if scheduled is None:
            return None
        return (row["actual_recorded_at"].astimezone(BUDAPEST_TZ) - scheduled).total_seconds()

    reconciled["actual_delay_seconds"] = reconciled.apply(actual_delay, axis=1)
    reconciled = reconciled.dropna(subset=["actual_delay_seconds"])

    reconciled["our_error"] = (reconciled["our_predicted_delay_seconds"] - reconciled["actual_delay_seconds"]).abs()
    reconciled["google_error"] = (
        reconciled["google_predicted_delay_seconds"] - reconciled["actual_delay_seconds"]
    ).abs()
    reconciled["we_won"] = reconciled["our_error"] < reconciled["google_error"]

    print(f"\n{len(reconciled)} comparable reconciled predictions:")
    print(f"  Our model    - mean absolute error: {reconciled['our_error'].mean():.1f}s "
          f"(median {reconciled['our_error'].median():.1f}s)")
    print(f"  Google Routes API - mean absolute error: {reconciled['google_error'].mean():.1f}s "
          f"(median {reconciled['google_error'].median():.1f}s)")
    print(f"  We were closer in {reconciled['we_won'].sum()}/{len(reconciled)} "
          f"({reconciled['we_won'].mean() * 100:.0f}%) of comparisons")

    print("\nPer-comparison detail:")
    print(reconciled[[
        "route_id", "our_predicted_delay_seconds", "google_predicted_delay_seconds",
        "actual_delay_seconds", "our_error", "google_error",
    ]].to_string(index=False, float_format=lambda x: f"{x:.1f}"))


if __name__ == "__main__":
    main()
