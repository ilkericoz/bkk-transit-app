"""
FastAPI serving layer for the stage-5 delay-prediction model - the sidecar
from the original staged plan (see project notes): a small, separate
model-serving service the Spring Boot backend calls over HTTP, rather than
folding ML serving into the Java app itself.

Run locally with: uvicorn main:app --reload --port 8000
(requires models/delay_model.joblib to already exist - run
scripts/train_model.py first if it doesn't.)
"""

from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from delay_model import DelayModel

MODEL_PATH = Path(__file__).resolve().parent / "models" / "delay_model.joblib"
BUDAPEST_TZ = ZoneInfo("Europe/Budapest")

model: DelayModel | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global model
    if not MODEL_PATH.exists():
        raise RuntimeError(f"No trained model at {MODEL_PATH} - run scripts/train_model.py first.")
    model = DelayModel.load(MODEL_PATH)
    yield


app = FastAPI(title="BKK Delay Prediction Service", lifespan=lifespan)


class DelayPredictionRequest(BaseModel):
    route_id: str = Field(examples=["BKK_3020"])
    stop_id: str = Field(examples=["BKK_F00969"])
    vehicle_route_type: str = Field(examples=["TRAM"])
    stop_sequence: int = Field(ge=0)
    # When the vehicle is scheduled to arrive at this stop. hour/day_of_week
    # are derived from this here, rather than accepted directly, so callers
    # send real trip data instead of pre-computed model features. Naive
    # datetimes are assumed to already be Europe/Budapest civil time
    # (matching GTFS's own convention); timezone-aware ones are converted.
    scheduled_arrival: datetime


class DelayPredictionResponse(BaseModel):
    predicted_delay_seconds: float


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "model_loaded": model is not None}


@app.post("/predict", response_model=DelayPredictionResponse)
def predict(request: DelayPredictionRequest) -> DelayPredictionResponse:
    if model is None:
        raise HTTPException(status_code=503, detail="Model not loaded")

    arrival = request.scheduled_arrival
    arrival = arrival.replace(tzinfo=BUDAPEST_TZ) if arrival.tzinfo is None else arrival.astimezone(BUDAPEST_TZ)

    predicted_delay = model.predict_one(
        route_id=request.route_id,
        stop_id=request.stop_id,
        vehicle_route_type=request.vehicle_route_type,
        stop_sequence=request.stop_sequence,
        hour=arrival.hour,
        day_of_week=arrival.weekday(),
    )
    return DelayPredictionResponse(predicted_delay_seconds=predicted_delay)
