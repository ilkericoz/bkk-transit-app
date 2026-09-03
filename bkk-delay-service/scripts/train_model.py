"""
Compares the three candidate delay-prediction models (see delay_model.py)
via walk-forward validation, then automatically saves whichever one
actually won - so the FastAPI service always serves the model the evidence
currently supports, with no manual "which one do we ship" step to forget.

Validation is WALK-FORWARD across multiple day-folds, not a single
most-recent-slice split. History: the first version of this script did one
80/20 chronological split and reported GBT as the clear winner (~96s MAE vs
~112s baseline). Once more data piled up, that same single-split approach
suddenly showed GBT *losing* to the baseline (120.9s vs 111.1s) - digging in
(2026-09-01) found the single split had, by chance, put a weekend-heavy
period in train and a weekday-rush-heavy period in val - two genuinely
different delay distributions - and GBT, being more flexible than the
simpler models, had overfit to the training regime rather than learning
something that transfers. A single split can't distinguish "this model is
actually better" from "this model got lucky/unlucky with which regime
landed in train vs val". Walk-forward folds - train on every day up to
day N, validate on day N, for each day N in turn - averages out that luck
and also surfaces the day-to-day variance directly, which is itself useful
signal about how much to trust the comparison yet.

Automatic model selection is a direct consequence of that finding: on the
same day this walk-forward scheme was built, GBT lost to even the "dumb"
baseline in 2 of 3 folds, while linear regression won consistently. Rather
than hardcoding "ship linear" (which would itself go stale once GBT
eventually earns its keep with more day-diversity), the winner each run is
whichever candidate actually has the lowest mean walk-forward MAE.

Run history (mean walk-forward MAE, lower is better) - kept here rather
than only in chat, per the project's "don't leave things as tribal
knowledge" convention:
  2026-09-01, 1,123,511 rows, 3 folds:
    baseline ~111s / linear ~97s (std 7.0s) / gbt ~241s (std 141s, blew up
    on evening-only->weekend and weekend->weekday-rush folds). Winner: linear.
  2026-09-03, 1,503,915 rows, 6 folds:
    baseline 106.5s / linear 97.6s (std 6.2s) / gbt 171.1s (std 122.1s,
    still wrecked by one thin-data 08-29 fold at 429s, but its other folds
    -88.9s/205.4s/107.1s/102.6s/93.5s- are visibly converging toward
    linear's range). Winner: linear again, but gbt is trending competitive
    as more day-diversity accumulates - worth rerunning again once another
    week or two has passed to see if it flips.
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.metrics import mean_absolute_error, root_mean_squared_error

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from delay_model import TARGET, CANDIDATE_MODELS

DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "delay_labels.csv"
MODEL_PATH = Path(__file__).resolve().parent.parent / "models" / "delay_model.joblib"

# A day with fewer than this many rows is too thin a slice to trust as a
# validation fold (e.g. the first partial evening of collection, or a day
# that's barely started) - it's still used as *training* data for later
# folds once it's no longer the most recent day, just never validated on.
MIN_VAL_ROWS = 20_000


def load_features() -> pd.DataFrame:
    df = pd.read_csv(DATA_PATH, parse_dates=["scheduled_arrival", "actual_arrival"])
    # hour/day_of_week are known in advance for any future trip, unlike
    # e.g. actual_arrival - that's the line between a legitimate predictive
    # feature and one that would leak the label.
    df["hour"] = df["scheduled_arrival"].dt.hour
    df["day_of_week"] = df["scheduled_arrival"].dt.dayofweek  # 0=Monday
    df["date"] = df["scheduled_arrival"].dt.date
    return df


def walk_forward_folds(df: pd.DataFrame) -> list[tuple[object, pd.DataFrame, pd.DataFrame]]:
    """Expanding-window folds: for each calendar day (after the first),
    train on every earlier day and validate on that day. Skips days too
    small to be a meaningful validation target (see MIN_VAL_ROWS), but
    still folds them into training once a later day needs them."""
    dates = sorted(df["date"].unique())
    folds = []
    for i in range(1, len(dates)):
        val_date = dates[i]
        val = df[df["date"] == val_date]
        if len(val) < MIN_VAL_ROWS:
            continue
        train = df[df["date"].isin(dates[:i])]
        folds.append((val_date, train, val))
    return folds


def evaluate_fold(train: pd.DataFrame, val: pd.DataFrame) -> dict:
    result = {"val_date": val.iloc[0]["date"], "train_rows": len(train), "val_rows": len(val)}
    for model_name, model_cls in CANDIDATE_MODELS.items():
        fitted = model_cls.fit(train)
        train_pred, val_pred = fitted.predict(train), fitted.predict(val)
        result[f"{model_name}_train_mae"] = mean_absolute_error(train[TARGET], train_pred)
        result[f"{model_name}_mae"] = mean_absolute_error(val[TARGET], val_pred)
        result[f"{model_name}_rmse"] = root_mean_squared_error(val[TARGET], val_pred)
    return result


def print_fold_table(fold_results: list[dict]) -> None:
    print(f"{len(fold_results)} walk-forward fold(s):\n")
    names = list(CANDIDATE_MODELS)
    header = f"{'val_date':12} {'train_n':>9} {'val_n':>8} " + " ".join(f"{n:>10}" for n in names)
    print(header)
    for r in fold_results:
        row = f"{str(r['val_date']):12} {r['train_rows']:>9} {r['val_rows']:>8} "
        row += " ".join(f"{r[f'{n}_mae']:>9.1f}s" for n in names)
        print(row)


def pick_winner(fold_results: list[dict]) -> tuple[str, dict[str, float]]:
    mean_mae = {}
    for name in CANDIDATE_MODELS:
        maes = np.array([r[f"{name}_mae"] for r in fold_results])
        mean_mae[name] = maes.mean()
        std = maes.std()
        gap = np.array([r[f"{name}_mae"] - r[f"{name}_train_mae"] for r in fold_results])
        print(f"  {name:10s} mean_mae={mean_mae[name]:7.1f}s  std={std:6.1f}s  "
              f"train->val gap per fold: {[f'{g:.1f}s' for g in gap]}")
    winner = min(mean_mae, key=mean_mae.get)
    return winner, mean_mae


def main() -> None:
    df = load_features()
    folds = walk_forward_folds(df)
    if not folds:
        print("Not enough distinct days with >= MIN_VAL_ROWS yet to form a single walk-forward fold.")
        print("Re-run once at least two days meet that threshold.")
        return

    fold_results = [evaluate_fold(train, val) for _, train, val in folds]
    print_fold_table(fold_results)

    print("\nMean MAE across folds - lower is better, and lower std/gap means the comparison is more trustworthy:")
    winner, mean_mae = pick_winner(fold_results)
    runner_up = sorted(mean_mae, key=mean_mae.get)[1] if len(mean_mae) > 1 else None
    print(f"\nWinner: '{winner}' (mean MAE {mean_mae[winner]:.1f}s)"
          + (f", ahead of '{runner_up}' ({mean_mae[runner_up]:.1f}s)" if runner_up else ""))

    # Refit the winner on ALL available labeled data (no more need to hold
    # anything back once nothing is being validated against) and save it as
    # the one artifact main.py loads - whichever candidate that is.
    print(f"Refitting '{winner}' on full dataset ({len(df)} rows) and saving to {MODEL_PATH}")
    final_model = CANDIDATE_MODELS[winner].fit(df)
    final_model.save(MODEL_PATH)


if __name__ == "__main__":
    main()
