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
from datetime import timedelta
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
           gbs.sampled_at,
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

    def travel_times(row) -> pd.Series:
        """
        Compares elapsed TRAVEL TIME from the moment we sampled, not
        absolute "implied delay against our schedule" - found empirically
        (2026-09-14) that the delay framing produces wildly inflated
        Google numbers (480s, 840s+) because Google's arrivalTime reflects
        whichever specific departure ITS engine assumes a fresh rider
        boards, which for an already-in-motion tracked vehicle is often a
        *later* service than the one actually being ridden - not a real
        accuracy gap, just two answers about different trip instances.
        Comparing "how long from query to arrival" sidesteps that
        entirely: it doesn't matter which specific service each side
        privately assumed, only how long each thought the wait+ride would
        take versus how long it actually took.
        """
        scheduled = schedule_lookup.scheduled_arrival(
            row["trip_id"].removeprefix("BKK_"), row["stop_sequence"], row["service_date"]
        )
        if scheduled is None:
            return pd.Series({"actual_travel_s": None, "our_travel_s": None, "google_travel_s": None})

        sampled_at = row["sampled_at"].astimezone(BUDAPEST_TZ)
        actual_arrival = row["actual_recorded_at"].astimezone(BUDAPEST_TZ)
        google_arrival = scheduled + timedelta(seconds=row["google_predicted_delay_seconds"])
        our_travel_s = None
        if pd.notna(row["our_predicted_delay_seconds"]):
            our_arrival = scheduled + timedelta(seconds=row["our_predicted_delay_seconds"])
            our_travel_s = (our_arrival - sampled_at).total_seconds()

        return pd.Series({
            "actual_travel_s": (actual_arrival - sampled_at).total_seconds(),
            "our_travel_s": our_travel_s,
            "google_travel_s": (google_arrival - sampled_at).total_seconds(),
        })

    reconciled = pd.concat([reconciled, reconciled.apply(travel_times, axis=1)], axis=1)
    reconciled = reconciled.dropna(subset=["actual_travel_s", "our_travel_s", "google_travel_s"])

    reconciled["our_error"] = (reconciled["our_travel_s"] - reconciled["actual_travel_s"]).abs()
    reconciled["google_error"] = (reconciled["google_travel_s"] - reconciled["actual_travel_s"]).abs()
    reconciled["we_won"] = reconciled["our_error"] < reconciled["google_error"]

    print(f"\n{len(reconciled)} comparable reconciled predictions "
          f"(all times below are elapsed travel time from the moment of the query, not absolute delay):")
    print(f"  Our model    - mean absolute error: {reconciled['our_error'].mean():.1f}s "
          f"(median {reconciled['our_error'].median():.1f}s)")
    print(f"  Google Routes API - mean absolute error: {reconciled['google_error'].mean():.1f}s "
          f"(median {reconciled['google_error'].median():.1f}s)")
    print(f"  We were closer in {reconciled['we_won'].sum()}/{len(reconciled)} "
          f"({reconciled['we_won'].mean() * 100:.0f}%) of comparisons")

    print("\nPer-comparison detail (seconds, elapsed travel time from query):")
    print(reconciled[[
        "route_id", "our_travel_s", "google_travel_s", "actual_travel_s", "our_error", "google_error",
    ]].to_string(index=False, float_format=lambda x: f"{x:.1f}"))


if __name__ == "__main__":
    main()
