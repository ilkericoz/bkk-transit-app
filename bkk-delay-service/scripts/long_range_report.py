"""
Live accuracy of the long-range predictions (added 2026-09-27): graded rows
of long_range_predictions (3 / 5 / 10 stops ahead, see main.py's
predict_long_range), per model version and stops ahead, against "keep the
current delay" on the same rows (upstream_delay_seconds). Also by scheduled
minutes ahead in the MBTA buckets, to compare with the offline walk-forward
result (thesis-key-facts 5r: 26.1 / 40.4 / 56.2 / 78.0 s vs keep-delay
34.9 / 49.5 / 67.5 / 91.9 s at 0-3 / 3-6 / 6-12 / 12-30 min).

Same rules as the other live reports: |actual| <= 3600 s, known collector
backlogs excluded; "accurate" = the MBTA standard (main.py's
prediction_accurate). Read-only. Usage (from bkk-delay-service/, host venv):
    python scripts/long_range_report.py
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import psycopg2

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from data_quality import backlog_sql_filter  # noqa: E402
from live_model_comparison import DB_CONFIG  # noqa: E402
from main import prediction_accurate  # noqa: E402

QUERY = f"""
    SELECT model_version, stops_ahead, scheduled_minutes_ahead, vehicle_route_type, reference_time,
           actual_recorded_at, predicted_delay_seconds, upstream_delay_seconds, actual_delay_seconds
    FROM long_range_predictions
    WHERE actual_delay_seconds IS NOT NULL AND abs(actual_delay_seconds) <= 3600
      AND {backlog_sql_filter(["reference_time", "actual_recorded_at"])}
"""


def summarise(g: pd.DataFrame) -> dict:
    model_err = (g["pred"] - g["actual"]).abs()
    keep_err = (g["keep"] - g["actual"]).abs()
    return {
        "n": len(g), "model MAE": model_err.mean(), "keep MAE": keep_err.mean(),
        "gain %": 100 * (1 - model_err.mean() / keep_err.mean()),
        "model acc %": 100 * np.mean([prediction_accurate(p, a, h) for p, a, h in zip(g["pred"], g["actual"], g["horizon"])]),
        "keep acc %": 100 * np.mean([prediction_accurate(p, a, h) for p, a, h in zip(g["keep"], g["actual"], g["horizon"])]),
        "model closer %": 100 * (model_err < keep_err).mean(),
    }


def main() -> None:
    with psycopg2.connect(**DB_CONFIG) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*), count(actual_delay_seconds), min(predicted_at), max(predicted_at) FROM long_range_predictions")
        total, graded, first, last = cur.fetchone()
        cur.execute(QUERY)
        d = pd.DataFrame(cur.fetchall(), columns=["model", "k", "sched_min", "vtype", "reference", "arrived",
                                                  "pred", "keep", "actual"])
    print(f"long_range_predictions: {total:,} rows, {graded:,} graded ({first} -> {last})")
    if d.empty:
        return
    for c in ["sched_min", "pred", "keep", "actual"]:
        d[c] = d[c].astype(float)
    d["horizon"] = (d["arrived"] - d["reference"]).dt.total_seconds()
    d["bucket"] = pd.cut(d["sched_min"], [0, 3, 6, 12, 30], labels=["0-3 min", "3-6 min", "6-12 min", "12-30 min"])
    pd.set_option("display.width", 200)
    for model, g in d.groupby("model"):
        print(f"\nmodel {model}: {len(g):,} graded")
        by_k = pd.DataFrame({f"{k} stops": summarise(x) for k, x in g.groupby("k")} | {"all": summarise(g)}).T
        print(by_k.round(1).to_string())
        by_b = pd.DataFrame({b: summarise(x) for b, x in g.groupby("bucket", observed=True)}).T
        print(by_b.round(1).to_string())


if __name__ == "__main__":
    main()
