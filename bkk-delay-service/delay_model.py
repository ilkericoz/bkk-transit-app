"""
The three candidate delay-prediction models, all sharing one interface
(fit/predict/predict_one/save/load) so train_model.py can pick whichever
one actually wins the walk-forward comparison and main.py can serve it
without caring which type it got.

  - BaselineDelayModel: per-route historical average delay. The "dumb"
    model everything else has to beat to justify its complexity.
  - LinearDelayModel: ordinary least squares on one-hot-encoded
    categoricals. Only learns additive effects, not interactions - but
    too simple to overfit hard on a small/skewed training window, which
    is exactly what has made it the most trustworthy candidate so far
    (see train_model.py's walk-forward results from 2026-09-01).
  - GbtDelayModel: gradient-boosted trees (HistGradientBoostingRegressor).
    Can learn interactions the linear model can't (e.g. "route X is bad
    specifically during evening rush"), but needs enough training-day
    diversity to do that instead of overfitting to whichever few days it
    was given - not there yet as of 2026-09-01.

Why one shared class hierarchy instead of three unrelated scripts: joblib
pickles the whole object, class and all, so whichever candidate
train_model.py decides to save is exactly what main.py loads back - no
separate "which kind of model is this" bookkeeping needed, and no risk of
serving-side code drifting from training-side code on how a feature gets
encoded (a classic source of silent bugs).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

import joblib
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.linear_model import LinearRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder

CATEGORICAL_FEATURES = ["route_id", "vehicle_route_type", "stop_id"]
NUMERIC_FEATURES = ["hour", "day_of_week", "stop_sequence"]
ALL_FEATURES = CATEGORICAL_FEATURES + NUMERIC_FEATURES
TARGET = "delay_seconds"


class BaseDelayModel(ABC):
    """Common interface every candidate model implements. `name` is the
    short label train_model.py's walk-forward report keys results by."""

    name: ClassVar[str]

    @classmethod
    @abstractmethod
    def fit(cls, train: pd.DataFrame) -> "BaseDelayModel":
        ...

    @abstractmethod
    def predict(self, df: pd.DataFrame) -> pd.Series:
        """Batch prediction over a DataFrame with the raw ALL_FEATURES columns."""

    def predict_one(self, **kwargs) -> float:
        row = pd.DataFrame([kwargs])
        return float(self.predict(row).iloc[0])

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        joblib.dump(self, path)

    @classmethod
    def load(cls, path: Path) -> "BaseDelayModel":
        return joblib.load(path)


@dataclass
class BaselineDelayModel(BaseDelayModel):
    name: ClassVar[str] = "baseline"
    route_mean_delay: pd.Series
    global_mean_delay: float

    @classmethod
    def fit(cls, train: pd.DataFrame) -> "BaselineDelayModel":
        return cls(
            route_mean_delay=train.groupby("route_id")[TARGET].mean(),
            global_mean_delay=float(train[TARGET].mean()),
        )

    def predict(self, df: pd.DataFrame) -> pd.Series:
        return df["route_id"].map(self.route_mean_delay).fillna(self.global_mean_delay)


@dataclass
class LinearDelayModel(BaseDelayModel):
    name: ClassVar[str] = "linear"
    pipeline: Pipeline

    @classmethod
    def fit(cls, train: pd.DataFrame) -> "LinearDelayModel":
        pipeline = Pipeline([
            ("encode", ColumnTransformer(
                [("cat", OneHotEncoder(handle_unknown="ignore"), CATEGORICAL_FEATURES)],
                remainder="passthrough",
            )),
            ("regress", LinearRegression()),
        ])
        pipeline.fit(train[ALL_FEATURES], train[TARGET])
        return cls(pipeline=pipeline)

    def predict(self, df: pd.DataFrame) -> pd.Series:
        return pd.Series(self.pipeline.predict(df[ALL_FEATURES]), index=df.index)


@dataclass
class GbtDelayModel(BaseDelayModel):
    name: ClassVar[str] = "gbt"
    model: HistGradientBoostingRegressor
    route_mean_delay: pd.Series
    stop_mean_delay: pd.Series
    global_mean_delay: float
    vehicle_route_type_categories: pd.CategoricalDtype

    @classmethod
    def fit(cls, train: pd.DataFrame) -> "GbtDelayModel":
        global_mean_delay = float(train[TARGET].mean())
        route_mean_delay = train.groupby("route_id")[TARGET].mean()
        stop_mean_delay = train.groupby("stop_id")[TARGET].mean()
        vehicle_route_type_categories = pd.CategoricalDtype(
            categories=train["vehicle_route_type"].dropna().unique()
        )

        instance = cls(
            model=HistGradientBoostingRegressor(categorical_features="from_dtype", random_state=42),
            route_mean_delay=route_mean_delay,
            stop_mean_delay=stop_mean_delay,
            global_mean_delay=global_mean_delay,
            vehicle_route_type_categories=vehicle_route_type_categories,
        )
        instance.model.fit(instance._encode(train), train[TARGET])
        return instance

    def _encode(self, df: pd.DataFrame) -> pd.DataFrame:
        """Turns raw feature columns into what the underlying model was
        actually fit on. route_id/stop_id are mean-target-encoded (train-only
        average delay per id) since both exceed HistGradientBoostingRegressor's
        native categorical cap of 255 codes; vehicle_route_type (only 4
        distinct values) gets true native categorical treatment."""
        return pd.DataFrame({
            "vehicle_route_type": df["vehicle_route_type"].astype(self.vehicle_route_type_categories),
            "hour": df["hour"],
            "day_of_week": df["day_of_week"],
            "stop_sequence": df["stop_sequence"],
            "route_id_mean_delay": df["route_id"].map(self.route_mean_delay).fillna(self.global_mean_delay),
            "stop_id_mean_delay": df["stop_id"].map(self.stop_mean_delay).fillna(self.global_mean_delay),
        })

    def predict(self, df: pd.DataFrame) -> pd.Series:
        return pd.Series(self.model.predict(self._encode(df)), index=df.index)


CANDIDATE_MODELS: dict[str, type[BaseDelayModel]] = {
    "baseline": BaselineDelayModel,
    "linear": LinearDelayModel,
    "gbt": GbtDelayModel,
}
