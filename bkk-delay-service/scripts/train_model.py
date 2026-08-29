"""
Trains and compares delay-prediction models on the labeled dataset built by
build_delay_dataset.py, in increasing sophistication:

  1. baseline - per-route historical average delay. The "dumb" model
                everything else must beat to be worth the added complexity.
  2. linear   - ordinary least squares on one-hot-encoded categoricals.
                Can only learn additive effects (e.g. "route X adds Ys on
                average"), not interactions.
  3. gbt      - gradient-boosted trees (HistGradientBoostingRegressor).
                Handles high-cardinality categoricals natively (no one-hot
                blowup) and can learn interactions a linear model can't,
                e.g. "route X is bad specifically during evening rush".

Split is time-based, not random: validation is the most recent slice of
scheduled_arrival. A random split would leak future information into
training (the i.i.d. assumption from stats courses breaks down for
time-series data - see the project memory for why this was flagged early).
"""

import sys
from pathlib import Path

import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.linear_model import LinearRegression
from sklearn.metrics import mean_absolute_error, root_mean_squared_error
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from delay_model import ALL_FEATURES, CATEGORICAL_FEATURES, NUMERIC_FEATURES, TARGET, DelayModel

DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "delay_labels.csv"
MODEL_PATH = Path(__file__).resolve().parent.parent / "models" / "delay_model.joblib"

VALIDATION_FRACTION = 0.2


def load_features() -> pd.DataFrame:
    df = pd.read_csv(DATA_PATH, parse_dates=["scheduled_arrival", "actual_arrival"])
    # hour/day_of_week are known in advance for any future trip, unlike
    # e.g. actual_arrival - that's the line between a legitimate predictive
    # feature and one that would leak the label.
    df["hour"] = df["scheduled_arrival"].dt.hour
    df["day_of_week"] = df["scheduled_arrival"].dt.dayofweek  # 0=Monday
    return df


def time_based_split(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    df = df.sort_values("scheduled_arrival").reset_index(drop=True)
    split_idx = int(len(df) * (1 - VALIDATION_FRACTION))
    train, val = df.iloc[:split_idx], df.iloc[split_idx:]
    print(f"Train: {len(train):>7} rows  [{train['scheduled_arrival'].min()} .. {train['scheduled_arrival'].max()}]")
    print(f"Val:   {len(val):>7} rows  [{val['scheduled_arrival'].min()} .. {val['scheduled_arrival'].max()}]")
    return train, val


def evaluate(name: str, y_true, y_pred) -> None:
    mae = mean_absolute_error(y_true, y_pred)
    rmse = root_mean_squared_error(y_true, y_pred)
    print(f"  {name:26s} MAE={mae:7.1f}s   RMSE={rmse:7.1f}s")


def baseline_predict(train: pd.DataFrame, val: pd.DataFrame):
    """Per-route historical average delay, falling back to the overall
    train-set average for any route never seen in training."""
    route_avg = train.groupby("route_id")[TARGET].mean()
    global_avg = train[TARGET].mean()
    return val["route_id"].map(route_avg).fillna(global_avg)


def linear_regression_predict(train: pd.DataFrame, val: pd.DataFrame):
    model = Pipeline([
        ("encode", ColumnTransformer(
            [("cat", OneHotEncoder(handle_unknown="ignore"), CATEGORICAL_FEATURES)],
            remainder="passthrough",
        )),
        ("regress", LinearRegression()),
    ])
    model.fit(train[ALL_FEATURES], train[TARGET])
    return model.predict(val[ALL_FEATURES])


def gradient_boosted_predict(train: pd.DataFrame, val: pd.DataFrame):
    # Encoding lives in delay_model.DelayModel (shared with the FastAPI
    # service) so training and serving can never encode a request
    # differently than the model was trained on.
    return DelayModel.fit(train).predict(val)


def main() -> None:
    df = load_features()
    train, val = time_based_split(df)

    print("\nModel comparison on held-out (most recent) validation slice - lower is better:")
    evaluate("baseline (per-route avg)", val[TARGET], baseline_predict(train, val))
    evaluate("linear regression", val[TARGET], linear_regression_predict(train, val))
    evaluate("gradient boosted trees", val[TARGET], gradient_boosted_predict(train, val))

    # The comparison above holds out the most recent slice to validate
    # against; once we trust the approach (which - see the training-window
    # caveat this was committed with - we don't fully yet), the production
    # artifact is refit on ALL available labeled data, not just `train`,
    # since there's no more need to hold anything back once nothing is
    # being validated anymore.
    print(f"\nRefitting on full dataset ({len(df)} rows) and saving to {MODEL_PATH}")
    final_model = DelayModel.fit(df)
    final_model.save(MODEL_PATH)


if __name__ == "__main__":
    main()
