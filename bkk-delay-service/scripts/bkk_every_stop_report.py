"""
Our model vs BKK's own prediction on the SAME stop visits (added
2026-09-27) - the fair head-to-head that bkk_comparison_report.py can't
give: that one scores the auto-sampler's predictions (random vehicles at
random moments: cold starts, long look-aheads, length-biased), where both
sides do worse. Here the population is the every-stop job's (every
vehicle's next stop, predicted at its arrival at the previous stop), and
BKK's prediction comes from main.py's bkk_stop_sample_loop, which asks BKK
for a random ~3% of those visits within seconds of the same moment.

Pairs are kept only when both sides refer to the same arrival (same
reference_time) and BKK was asked at most MAX_FETCH_LAG_SECONDS after it -
later, BKK would know more than our model did. Graded predictions only,
|actual| <= 3600 s, known collector backlogs excluded. "Accurate" = the
MBTA standard (main.py's prediction_accurate). 95% interval for the MAE
difference: bootstrap over (service date, hour) blocks.

Read-only. Usage (from bkk-delay-service/, host venv):
    python scripts/bkk_every_stop_report.py
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

MAX_FETCH_LAG_SECONDS = 20
MAX_ABS_DELAY_SECONDS = 3600

QUERY = f"""
    SELECT sp.model_version, sp.service_date, sp.vehicle_route_type, sp.reference_time, sp.actual_recorded_at,
           sp.predicted_delay_seconds, sp.upstream_delay_seconds, sp.actual_delay_seconds,
           b.fetched_at, b.bkk_predicted_delay_seconds
    FROM bkk_stop_samples b
    JOIN stop_predictions sp
      ON sp.trip_id = b.trip_id AND sp.service_date = b.service_date AND sp.stop_sequence = b.stop_sequence
    WHERE sp.actual_delay_seconds IS NOT NULL AND abs(sp.actual_delay_seconds) <= {MAX_ABS_DELAY_SECONDS}
      AND abs(extract(epoch FROM sp.reference_time - b.reference_time)) < 1
      AND {backlog_sql_filter(["sp.reference_time", "sp.actual_recorded_at"])}
"""


def block_ci(d: pd.DataFrame, a: str, b: str, reps: int = 1000, seed: int = 26) -> tuple[float, float]:
    ea, eb = (d[a] - d["actual"]).abs(), (d[b] - d["actual"]).abs()
    blocks = pd.DataFrame({"a": ea, "b": eb, "n": 1}).groupby([d["service_date"], d["hour"]]).sum().to_numpy()
    if len(blocks) < 2:
        return float("nan"), float("nan")
    s = blocks[np.random.default_rng(seed).integers(0, len(blocks), (reps, len(blocks)))].sum(axis=1)
    return tuple(np.percentile((s[:, 0] - s[:, 1]) / s[:, 2], [2.5, 97.5]))


def main() -> None:
    with psycopg2.connect(**DB_CONFIG) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*), count(bkk_predicted_delay_seconds) FROM bkk_stop_samples")
        n_samples, n_answered = cur.fetchone()
        cur.execute(QUERY)
        d = pd.DataFrame(cur.fetchall(), columns=["model", "service_date", "vtype", "reference", "arrived", "ours",
                                                  "persist", "actual", "fetched", "bkk"])
    print(f"bkk_stop_samples: {n_samples:,} rows, BKK answered {n_answered:,}; "
          f"joined to graded every-stop predictions: {len(d):,}")
    if d.empty:
        return
    for c in ["ours", "persist", "actual", "bkk"]:
        d[c] = d[c].astype(float)
    d["lag"] = (d["fetched"] - d["reference"]).dt.total_seconds()
    d["horizon"] = (d["arrived"] - d["reference"]).dt.total_seconds()
    d["hour"] = pd.to_datetime(d["reference"], utc=True).dt.tz_convert("Europe/Budapest").dt.hour
    late = d["lag"] > MAX_FETCH_LAG_SECONDS
    print(f"BKK asked after our prediction moment: median {d['lag'].median():.1f} s, "
          f"p90 {d['lag'].quantile(.9):.1f} s; dropped {int(late.sum())} pairs asked > {MAX_FETCH_LAG_SECONDS} s later")
    d = d[~late]

    for model, g in d.groupby("model"):
        has = g["bkk"].notna()
        h = g[has]
        print(f"\nmodel {model}: {len(g):,} pairs, {g['service_date'].nunique()} service day(s); "
              f"BKK had a prediction for {100 * has.mean():.1f}%")
        if not has.all():
            print(f"  where BKK had none (n={int((~has).sum())}): our MAE {(g.loc[~has, 'ours'] - g.loc[~has, 'actual']).abs().mean():.1f} s")
        if h.empty:
            continue
        rows = []
        for name, col in [("ours", "ours"), ("BKK", "bkk"), ("no model", "persist")]:
            err = (h[col] - h["actual"]).abs()
            acc = np.mean([prediction_accurate(p, a, t) for p, a, t in zip(h[col], h["actual"], h["horizon"])])
            rows.append(f"  {name:9s} MAE {err.mean():6.1f} s  median {err.median():5.1f} s  accurate {100 * acc:5.1f}%")
        print("\n".join(rows))
        diff = (h["ours"] - h["actual"]).abs().mean() - (h["bkk"] - h["actual"]).abs().mean()
        lo, hi = block_ci(h, "ours", "bkk")
        closer = ((h["ours"] - h["actual"]).abs() < (h["bkk"] - h["actual"]).abs()).mean()
        print(f"  ours - BKK: {diff:+.1f} s [{lo:+.1f}, {hi:+.1f}]; ours closer in {100 * closer:.0f}% "
              f"(n={len(h):,}{'; few days - interval covers within-day variation only' if h['service_date'].nunique() < 3 else ''})")


if __name__ == "__main__":
    main()
