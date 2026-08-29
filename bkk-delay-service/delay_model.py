"""
Shared delay-prediction model: the gradient-boosted-trees encoding from
train_model.py's comparison, pulled out into a class both training and
the FastAPI service import - so serving can never drift from training on
how route_id/stop_id get encoded (a classic source of silent bugs: model
trained with one encoding, served with a slightly different one).

Feature encoding recap (see train_model.py for why):
  - route_id, stop_id: mean-target-encoded (train-only average delay per
    id) - both exceed HistGradientBoostingRegressor's native categorical
    cap of 255 codes.
  - vehicle_route_type: native categorical (only 4 distinct values).
  - hour, day_of_week, stop_sequence: passed through as numeric.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import joblib
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor

CATEGORICAL_FEATURES = ["route_id", "vehicle_route_type", "stop_id"]
NUMERIC_FEATURES = ["hour", "day_of_week", "stop_sequence"]
ALL_FEATURES = CATEGORICAL_FEATURES + NUMERIC_FEATURES
TARGET = "delay_seconds"


@dataclass
class DelayModel:
    model: HistGradientBoostingRegressor
    route_mean_delay: pd.Series
    stop_mean_delay: pd.Series
    global_mean_delay: float
    vehicle_route_type_categories: pd.CategoricalDtype

    @classmethod
    def fit(cls, train: pd.DataFrame) -> "DelayModel":
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
        actually fit on. Column order matches DelayModel.fit exactly -
        HistGradientBoostingRegressor was fit on a DataFrame, so it
        validates feature *names* against this at predict time, not just
        position, but keeping the order identical avoids relying on that."""
        encoded = pd.DataFrame({
            "vehicle_route_type": df["vehicle_route_type"].astype(self.vehicle_route_type_categories),
            "hour": df["hour"],
            "day_of_week": df["day_of_week"],
            "stop_sequence": df["stop_sequence"],
            "route_id_mean_delay": df["route_id"].map(self.route_mean_delay).fillna(self.global_mean_delay),
            "stop_id_mean_delay": df["stop_id"].map(self.stop_mean_delay).fillna(self.global_mean_delay),
        })
        return encoded

    def predict(self, df: pd.DataFrame) -> pd.Series:
        """Batch prediction over a DataFrame with the raw ALL_FEATURES columns."""
        return pd.Series(self.model.predict(self._encode(df)), index=df.index)

    def predict_one(
        self,
        *,
        route_id: str,
        stop_id: str,
        vehicle_route_type: str,
        stop_sequence: int,
        hour: int,
        day_of_week: int,
    ) -> float:
        row = pd.DataFrame([{
            "route_id": route_id,
            "stop_id": stop_id,
            "vehicle_route_type": vehicle_route_type,
            "stop_sequence": stop_sequence,
            "hour": hour,
            "day_of_week": day_of_week,
        }])
        return float(self.predict(row).iloc[0])

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        joblib.dump(self, path)

    @classmethod
    def load(cls, path: Path) -> "DelayModel":
        return joblib.load(path)
