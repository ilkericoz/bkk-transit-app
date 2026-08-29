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

from pathlib import Path

import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.compose import ColumnTransformer
from sklearn.linear_model import LinearRegression
from sklearn.metrics import mean_absolute_error, root_mean_squared_error
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder

DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "delay_labels.csv"

CATEGORICAL_FEATURES = ["route_id", "vehicle_route_type", "stop_id"]
NUMERIC_FEATURES = ["hour", "day_of_week", "stop_sequence"]
ALL_FEATURES = CATEGORICAL_FEATURES + NUMERIC_FEATURES
TARGET = "delay_seconds"

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
    # HistGradientBoostingRegressor's native categorical handling packs
    # category codes into a uint8, capping cardinality at 255 - route_id
    # (~320 values) and stop_id (thousands) both blow past that. So instead
    # of one-hot/native-categorical for those two, mean-target-encode them:
    # replace each id with its *train-only* average delay (a standard trick
    # for high-cardinality categoricals in gradient boosting - conceptually
    # the same per-group average the baseline model uses, just handed to
    # the tree as one more numeric feature rather than the whole answer).
    # Unseen ids in val fall back to the train-set global mean.
    global_mean = train[TARGET].mean()
    train_x, val_x = train[ALL_FEATURES].copy(), val[ALL_FEATURES].copy()
    for col in ["route_id", "stop_id"]:
        id_mean_delay = train.groupby(col)[TARGET].mean()
        encoded = f"{col}_mean_delay"
        train_x[encoded] = train_x[col].map(id_mean_delay)
        val_x[encoded] = val_x[col].map(id_mean_delay).fillna(global_mean)
        train_x.drop(columns=col, inplace=True)
        val_x.drop(columns=col, inplace=True)

    # vehicle_route_type only has 4 distinct values (BUS/TRAM/TROLLEYBUS/
    # SUBURBAN_RAILWAY) - comfortably under the 255 cap, so it gets true
    # native categorical handling instead of mean-encoding.
    categories = pd.CategoricalDtype(categories=train_x["vehicle_route_type"].dropna().unique())
    train_x["vehicle_route_type"] = train_x["vehicle_route_type"].astype(categories)
    val_x["vehicle_route_type"] = val_x["vehicle_route_type"].astype(categories)

    model = HistGradientBoostingRegressor(categorical_features="from_dtype", random_state=42)
    model.fit(train_x, train[TARGET])
    return model.predict(val_x)


def main() -> None:
    df = load_features()
    train, val = time_based_split(df)

    print("\nModel comparison on held-out (most recent) validation slice - lower is better:")
    evaluate("baseline (per-route avg)", val[TARGET], baseline_predict(train, val))
    evaluate("linear regression", val[TARGET], linear_regression_predict(train, val))
    evaluate("gradient boosted trees", val[TARGET], gradient_boosted_predict(train, val))


if __name__ == "__main__":
    main()
