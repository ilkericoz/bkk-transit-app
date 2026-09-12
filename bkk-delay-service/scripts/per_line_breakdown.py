"""
Ad-hoc investigation (2026-09-12): the aggregate walk-forward MAE hides
per-route-type / per-line variance. This breaks the *last* fold (train on
every day up to but not including the most recent, validate on the most
recent day - same split train_model.py already uses for its final "today"
fold) down by vehicle_route_type and by individual route_id, using the
'linear' model that actually won and is currently being served.

Not wired into train_model.py's normal output on purpose - this is a
one-off diagnostic, not something that needs to run every training pass.
"""

import sys
from pathlib import Path

import pandas as pd
from sklearn.metrics import mean_absolute_error

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from delay_model import TARGET, CANDIDATE_MODELS

DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "delay_labels.csv"

df = pd.read_csv(DATA_PATH, parse_dates=["scheduled_arrival", "actual_arrival"])
df["hour"] = df["scheduled_arrival"].dt.hour
df["day_of_week"] = df["scheduled_arrival"].dt.dayofweek
df["date"] = df["scheduled_arrival"].dt.date

dates = sorted(df["date"].unique())
val_date = dates[-1]
train = df[df["date"] < val_date]
val = df[df["date"] == val_date].copy()

print(f"Train: {len(train)} rows (through {dates[-2]}) | Val: {len(val)} rows ({val_date})\n")

model = CANDIDATE_MODELS["linear"].fit(train)
val["pred"] = model.predict(val)
val["abs_err"] = (val["pred"] - val[TARGET]).abs()

print("=== By vehicle_route_type ===")
by_type = val.groupby("vehicle_route_type").agg(
    n=("abs_err", "size"),
    mae=("abs_err", "mean"),
    actual_median_delay=(TARGET, "median"),
).sort_values("n", ascending=False)
print(by_type.to_string(float_format=lambda x: f"{x:.1f}"))

print("\n=== Top 15 routes by volume ===")
by_route = val.groupby(["route_id", "vehicle_route_type"]).agg(
    n=("abs_err", "size"),
    mae=("abs_err", "mean"),
    actual_median_delay=(TARGET, "median"),
).reset_index().sort_values("n", ascending=False).head(15)
print(by_route.to_string(index=False, float_format=lambda x: f"{x:.1f}"))

print("\n=== Worst 15 routes by MAE (min 200 observations, to skip noise) ===")
by_route_all = val.groupby(["route_id", "vehicle_route_type"]).agg(
    n=("abs_err", "size"),
    mae=("abs_err", "mean"),
    actual_median_delay=(TARGET, "median"),
).reset_index()
worst = by_route_all[by_route_all["n"] >= 200].sort_values("mae", ascending=False).head(15)
print(worst.to_string(index=False, float_format=lambda x: f"{x:.1f}"))

print("\n=== Best 15 routes by MAE (min 200 observations) ===")
best = by_route_all[by_route_all["n"] >= 200].sort_values("mae", ascending=True).head(15)
print(best.to_string(index=False, float_format=lambda x: f"{x:.1f}"))
