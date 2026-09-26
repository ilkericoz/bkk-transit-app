"""
Live before/after comparison of two production models, from the every-stop
job's graded predictions (stop_predictions, see main.py's
stop_predictions_once): every vehicle's next stop, predicted when its
previous stop is confirmed, graded on arrival - the same population as the
offline walk-forward test (thesis-key-facts 5p), tagged with model_version.

The two models ran at different times, so a plain MAE per model would mix
the model change with the time-of-day mix (night is harder, rush hour
differs). So:
  - rows are grouped into cells (hour of day, optionally also day type),
    and both models are averaged with the NEW model's cell weights, only
    over cells where both have enough rows;
  - persistence (the delay at the previous stop, carried forward) is scored
    on each model's own rows. It doesn't depend on the model, so it shows
    whether the new model's days were simply easier or harder. "Skill" =
    model MAE - persistence MAE; the change in skill is the least
    confounded single number here;
  - a 95% interval comes from a block bootstrap over 10-minute blocks
    within each cell: predictions close in time share traffic and weather,
    so resampling single rows would be far too optimistic. It still
    understates day-to-day variation, which with few days per model is the
    bigger uncertainty - hence the warning below 2 days per model;
  - predictions touching a known collector backlog are dropped (see
    data_quality.COLLECTOR_BACKLOGS).

Read-only. Usage (from bkk-delay-service/, host venv):
    python scripts/live_model_comparison.py --old 378f437fd810 --new 7f90a3bdd515
    python scripts/live_model_comparison.py --old ... --new ... --by daytype_hour
"""

import argparse
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import psycopg2

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from data_quality import COLLECTOR_BACKLOGS

DB_CONFIG = {
    "host": os.environ.get("DB_HOST", "localhost"),
    "port": int(os.environ.get("DB_PORT", "5432")),
    "dbname": "bkk_transit",
    "user": "bkk_app",
    "password": os.environ.get("DB_PASSWORD", "bkk_dev_pw"),
}

# Same outlier limit as the training labels (build_delay_dataset's
# MAX_ABS_DELAY_SECONDS) and the 5p analysis.
MAX_ABS_DELAY_SECONDS = 3600

# Bootstrap block length (see the module docstring).
BLOCK_MINUTES = 10

QUERY = """
    SELECT model_version, service_date, reference_time, actual_recorded_at, vehicle_route_type,
           predicted_delay_seconds, upstream_delay_seconds, actual_delay_seconds
    FROM stop_predictions
    WHERE model_version = ANY(%(versions)s) AND actual_delay_seconds IS NOT NULL
"""


def load(versions: list[str]) -> pd.DataFrame:
    with psycopg2.connect(**DB_CONFIG) as conn, conn.cursor() as cur:
        cur.execute(QUERY, {"versions": versions})
        df = pd.DataFrame(cur.fetchall(), columns=[c.name for c in cur.description])
    df = df[df["actual_delay_seconds"].abs() <= MAX_ABS_DELAY_SECONDS].copy()
    df["reference_time"] = pd.to_datetime(df["reference_time"], utc=True)
    df["actual_recorded_at"] = pd.to_datetime(df["actual_recorded_at"], utc=True)
    backlog = pd.Series(False, index=df.index)
    for start, end in COLLECTOR_BACKLOGS:
        start, end = pd.Timestamp(start), pd.Timestamp(end)
        backlog |= df["reference_time"].between(start, end) | df["actual_recorded_at"].between(start, end)
    if backlog.any():
        print(f"dropped {int(backlog.sum()):,} predictions touching a collector backlog (COLLECTOR_BACKLOGS)")
    df = df[~backlog].copy()
    local = df["reference_time"].dt.tz_convert("Europe/Budapest")
    df["block"] = local.dt.floor(f"{BLOCK_MINUTES}min")
    df["hour"] = local.dt.hour
    df["daytype"] = local.dt.dayofweek.map(lambda d: "Sat" if d == 5 else "Sun" if d == 6 else "Mon-Fri")
    df["err"] = (df["predicted_delay_seconds"] - df["actual_delay_seconds"]).abs()
    df["persist_err"] = (df["upstream_delay_seconds"] - df["actual_delay_seconds"]).abs()
    return df


def blocks(df: pd.DataFrame, cell_cols: list[str]) -> pd.DataFrame:
    """Per (model, cell, 10-minute block): row count and error sums - the
    unit the bootstrap resamples."""
    keys = ["model_version", *cell_cols, "block"]
    return df.groupby(keys).agg(n=("err", "size"), err=("err", "sum"), persist=("persist_err", "sum")).reset_index()


def matched(b: pd.DataFrame, old: str, new: str, cell_cols: list[str], min_rows: int,
            reps: int, rng: np.random.Generator) -> dict | None:
    """Cell-matched MAE / skill for both models, plus bootstrap intervals for
    the differences (new - old)."""
    per_cell = b.groupby(["model_version", *cell_cols])["n"].sum().unstack("model_version")
    if old not in per_cell or new not in per_cell:
        return None
    cells = per_cell[(per_cell[old] >= min_rows) & (per_cell[new] >= min_rows)].index
    if len(cells) == 0:
        return None
    weights = per_cell.loc[cells, new] / per_cell.loc[cells, new].sum()

    def estimate(samples: dict) -> dict:
        out = {}
        for model in (old, new):
            mae = sum(weights[c] * samples[(model, c)][1] / samples[(model, c)][0] for c in cells)
            persist = sum(weights[c] * samples[(model, c)][2] / samples[(model, c)][0] for c in cells)
            out[model] = (mae, persist)
        return out

    # (model, cell) -> one row per block: [n, error sum, persistence sum]
    grouped = {}
    for key, g in b.groupby(["model_version", *cell_cols]):
        cell = key[1] if len(cell_cols) == 1 else tuple(key[1:])
        grouped[(key[0], cell)] = g[["n", "err", "persist"]].to_numpy()
    point = estimate({k: v.sum(axis=0) for k, v in grouped.items()})

    diffs = np.empty((reps, 2))
    for r in range(reps):
        sample = {}
        for m in (old, new):
            for c in cells:
                g = grouped[(m, c)]
                sample[(m, c)] = g[rng.integers(0, len(g), len(g))].sum(axis=0)
        est = estimate(sample)
        diffs[r] = [est[new][0] - est[old][0],
                    (est[new][0] - est[new][1]) - (est[old][0] - est[old][1])]
    lo, hi = np.percentile(diffs, [2.5, 97.5], axis=0)
    return {"cells": len(cells), "rows_old": int(per_cell.loc[cells, old].sum()),
            "rows_new": int(per_cell.loc[cells, new].sum()),
            "mae_old": point[old][0], "mae_new": point[new][0],
            "persist_old": point[old][1], "persist_new": point[new][1],
            "d_mae": point[new][0] - point[old][0], "d_mae_ci": (lo[0], hi[0]),
            "d_skill": (point[new][0] - point[new][1]) - (point[old][0] - point[old][1]),
            "d_skill_ci": (lo[1], hi[1])}


def print_summary(label: str, res: dict | None, old: str, new: str) -> None:
    if res is None:
        print(f"{label}: not enough overlapping data")
        return
    print(f"{label}: {res['cells']} matched cells, {res['rows_old']:,} old / {res['rows_new']:,} new rows")
    print(f"  MAE          {old[:8]} {res['mae_old']:6.1f}s   {new[:8]} {res['mae_new']:6.1f}s   "
          f"change {res['d_mae']:+5.2f}s [{res['d_mae_ci'][0]:+.2f}, {res['d_mae_ci'][1]:+.2f}]")
    print(f"  persistence  {old[:8]} {res['persist_old']:6.1f}s   {new[:8]} {res['persist_new']:6.1f}s   "
          f"(same yardstick on each model's rows - differs only because the days differ)")
    print(f"  skill (MAE - persistence) change {res['d_skill']:+5.2f}s "
          f"[{res['d_skill_ci'][0]:+.2f}, {res['d_skill_ci'][1]:+.2f}]  <- least confounded number")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--old", required=True, help="model_version before the change")
    ap.add_argument("--new", required=True, help="model_version after the change")
    ap.add_argument("--by", choices=["hour", "daytype_hour"], default="hour",
                    help="matching cells: hour of day, or day type (Mon-Fri/Sat/Sun) x hour")
    ap.add_argument("--min-rows", type=int, default=500, help="rows per model needed for a cell to count")
    ap.add_argument("--reps", type=int, default=1000, help="bootstrap repetitions")
    args = ap.parse_args()
    rng = np.random.default_rng(26)
    cell_cols = ["hour"] if args.by == "hour" else ["daytype", "hour"]

    df = load([args.old, args.new])
    for m in (args.old, args.new):
        d = df[df["model_version"] == m]
        if d.empty:
            print(f"{m}: no graded rows"); continue
        span = d["reference_time"].dt.tz_convert("Europe/Budapest")
        print(f"{m}: {len(d):,} graded rows, {d['service_date'].nunique()} service days, "
              f"{span.min():%a %d %b %H:%M} -> {span.max():%a %d %b %H:%M}; "
              f"unmatched MAE {d['err'].mean():.1f}s, persistence {d['persist_err'].mean():.1f}s")
    if (df.groupby("model_version")["service_date"].nunique() < 2).any():
        print("WARNING: a model has under 2 service days - the intervals below only cover variation "
              "within those hours, not between days; treat any difference as provisional")
    print()

    per_hour = df.groupby(["hour", "model_version"]).agg(n=("err", "size"), mae=("err", "mean"),
                                                          persist=("persist_err", "mean")).unstack("model_version")
    print(f"{'hour':>4} {'n old':>8} {'n new':>8} {'MAE old':>8} {'MAE new':>8} {'pers old':>9} {'pers new':>9}")
    for h, r in per_hour.iterrows():
        def g(col, m, fmt):
            v = r.get((col, m))
            return format(v, fmt) if pd.notna(v) else "-"
        print(f"{h:>4} {g('n', args.old, ',.0f'):>8} {g('n', args.new, ',.0f'):>8} "
              f"{g('mae', args.old, '.1f'):>8} {g('mae', args.new, '.1f'):>8} "
              f"{g('persist', args.old, '.1f'):>9} {g('persist', args.new, '.1f'):>9}")
    print()

    b = blocks(df, cell_cols)
    print_summary(f"ALL (matched by {args.by})", matched(b, args.old, args.new, cell_cols, args.min_rows, args.reps, rng),
                  args.old, args.new)
    for vtype, d in df.groupby("vehicle_route_type"):
        print()
        print_summary(f"{vtype}", matched(blocks(d, cell_cols), args.old, args.new, cell_cols,
                                          max(50, args.min_rows // 5), args.reps, rng), args.old, args.new)


if __name__ == "__main__":
    main()
