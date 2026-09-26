"""
How often did the backend's RabbitMQ consumer fall behind? (2026-09-26)

vehicle_position_snapshots.recorded_at is set when the consumer saves a row
(VehiclePositionConsumer: Instant.now()), not when BKK reported the
position (last_update_time, BKK's clock, epoch seconds). Normally the two
are ~11 s apart; during a consumer backlog the gap grows (up to ~19 min on
26 Sep 16:26-16:58), and every arrival recorded in that window is late by
that much - in the live grading and in the training labels.

Scans the whole table one hour at a time (each slice is an index range on
recorded_at) and writes, per minute: row count and median lag
(recorded_at - last_update_time). A median, because single vehicles can
report stale positions for minutes (BKK's side) - a backlog shifts every
row. Before each slice it checks that the live collector is keeping up
and waits if not, so the scan can't cause the problem it is looking for.
Resumable: hours already in the output file are skipped.

Read-only. Usage (from bkk-delay-service/, host venv):
    python scripts/collector_lag_scan.py            # scan / resume
    python scripts/collector_lag_scan.py --report   # summarise the output
"""

import argparse
import csv
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import psycopg2

sys.path.insert(0, str(Path(__file__).resolve().parent))
from live_model_comparison import DB_CONFIG

OUTPUT_PATH = Path(__file__).resolve().parent.parent / "data" / "collector_lag_by_minute.csv"

# A minute counts as backlogged when its median lag exceeds this. Normal is
# ~10-12 s; BKK's own feed refresh adds up to a few tens of seconds.
BACKLOG_LAG_SECONDS = 60
# Pause the scan while the live collector's median lag is above this.
LIVE_LAG_PAUSE_SECONDS = 30

SLICE_QUERY = """
    SELECT date_trunc('minute', recorded_at) AS minute, count(*) AS rows,
           percentile_cont(0.5) WITHIN GROUP (ORDER BY extract(epoch FROM recorded_at) - last_update_time) AS median_lag
    FROM vehicle_position_snapshots
    WHERE recorded_at >= %(start)s AND recorded_at < %(end)s
    GROUP BY 1 ORDER BY 1
"""
LIVE_LAG_QUERY = """
    SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY extract(epoch FROM recorded_at) - last_update_time)
    FROM vehicle_position_snapshots WHERE recorded_at >= now() - interval '1 minute'
"""


def scan() -> None:
    done = set()
    if OUTPUT_PATH.exists():
        done = set(pd.read_csv(OUTPUT_PATH, usecols=["hour"])["hour"])
    new_file = not OUTPUT_PATH.exists()
    with psycopg2.connect(**DB_CONFIG) as conn, conn.cursor() as cur, open(OUTPUT_PATH, "a", newline="") as f:
        out = csv.writer(f)
        if new_file:
            out.writerow(["hour", "minute", "rows", "median_lag"])
        cur.execute("SELECT min(recorded_at), max(recorded_at) FROM vehicle_position_snapshots")
        first, last = cur.fetchone()
        hour = first.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
        # Stop before the current hour: it's still being written.
        stop = last.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
        started, scanned = time.time(), 0
        while hour < stop:
            key = hour.isoformat()
            if key not in done:
                while True:
                    cur.execute(LIVE_LAG_QUERY)
                    live_lag = cur.fetchone()[0]
                    if live_lag is None or live_lag <= LIVE_LAG_PAUSE_SECONDS:
                        break
                    print(f"live collector lag {live_lag:.0f}s - pausing 60 s", flush=True)
                    time.sleep(60)
                cur.execute(SLICE_QUERY, {"start": hour, "end": hour + timedelta(hours=1)})
                for minute, rows, lag in cur.fetchall():
                    out.writerow([key, minute.astimezone(timezone.utc).isoformat(), rows,
                                  "" if lag is None else round(lag, 1)])
                f.flush()
                conn.rollback()  # end the read transaction between slices
                scanned += 1
                if scanned % 24 == 0:
                    print(f"{hour:%Y-%m-%d %H:%M} UTC  ({scanned} hours in {time.time() - started:.0f} s, "
                          f"live lag {live_lag or 0:.0f}s)", flush=True)
                time.sleep(0.2)
            hour += timedelta(hours=1)
    print(f"done: {scanned} new hours scanned -> {OUTPUT_PATH}")


def report() -> None:
    df = pd.read_csv(OUTPUT_PATH, parse_dates=["minute"])
    df["minute"] = df["minute"].dt.tz_convert("Europe/Budapest")
    df = df.sort_values("minute").reset_index(drop=True)
    bad = df["median_lag"] > BACKLOG_LAG_SECONDS
    print(f"{len(df):,} minutes scanned, {df['minute'].min():%d %b %H:%M} -> {df['minute'].max():%d %b %H:%M}; "
          f"median lag overall {df['median_lag'].median():.1f}s")
    print(f"minutes with median lag > {BACKLOG_LAG_SECONDS}s: {int(bad.sum()):,} "
          f"({100 * bad.mean():.2f}%), holding {int(df.loc[bad, 'rows'].sum()):,} of {int(df['rows'].sum()):,} rows "
          f"({100 * df.loc[bad, 'rows'].sum() / df['rows'].sum():.2f}%)")
    # Group backlogged minutes into episodes (gaps of up to 5 min merged).
    episodes, current = [], None
    for i in df.index[bad]:
        m = df.at[i, "minute"]
        if current and (m - current["end"]) <= pd.Timedelta(minutes=5):
            current.update(end=m, peak=max(current["peak"], df.at[i, "median_lag"]), rows=current["rows"] + df.at[i, "rows"])
        else:
            current = {"start": m, "end": m, "peak": df.at[i, "median_lag"], "rows": df.at[i, "rows"]}
            episodes.append(current)
    print(f"\n{len(episodes)} backlog episode(s):")
    print(f"{'start':>16} {'end':>6} {'minutes':>8} {'peak lag':>9} {'rows':>9}")
    for e in episodes:
        mins = int((e["end"] - e["start"]).total_seconds() / 60) + 1
        print(f"{e['start']:%a %d %b %H:%M} {e['end']:%H:%M} {mins:>8} {e['peak']:>8.0f}s {e['rows']:>9,}")
    # Gaps: minutes with no rows at all don't appear in the scan - list long ones.
    gaps = df["minute"].diff()
    long_gaps = df.loc[gaps > pd.Timedelta(minutes=5)]
    print(f"\n{len(long_gaps)} gap(s) of more than 5 min with no rows at all (collector down):")
    for i in long_gaps.index:
        print(f"  {df.at[i - 1, 'minute']:%a %d %b %H:%M} -> {df.at[i, 'minute']:%H:%M} ({gaps[i]})")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--report", action="store_true", help="summarise the output file instead of scanning")
    report() if ap.parse_args().report else scan()
