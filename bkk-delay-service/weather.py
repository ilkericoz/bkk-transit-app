"""
Budapest weather via Open-Meteo (open-meteo.com) - free, no API key needed.
Used both to backfill historical weather onto already-collected training
rows (build_delay_dataset.py, via fetch_historical_hourly) and to get a
cached "current conditions" reading at live-prediction time (main.py, via
LiveWeather).

One fixed reading for the whole metro area (same Deák Ferenc tér
coordinates already used elsewhere - see CENTER in app.js /
bkk.ingestion.center-lat/lon in application.properties) - a reasonable
simplification at this project's scale, not meant to capture
neighborhood-level variation (rain on one side of the city vs. the other).
"""

import time
from datetime import date
from zoneinfo import ZoneInfo

import pandas as pd
import requests

BUDAPEST_TZ = ZoneInfo("Europe/Budapest")
LAT, LON = 47.4979, 19.0402

# Precipitation is the one most likely to actually matter for delay (rain
# slows surface traffic); temperature/wind included too since they're free
# in the same request and plausibly relevant (wind for trams/trolleybuses,
# temperature for general conditions) - cheap to include, let the model
# decide via training whether they carry real weight.
HOURLY_FIELDS = ["temperature_2m", "precipitation", "wind_speed_10m"]


def fetch_historical_hourly(start_date: date, end_date: date) -> pd.DataFrame:
    """
    One row per hour in [start_date, end_date] (inclusive), Budapest local
    time - for backfilling weather onto already-collected training rows via
    an hour-bucket join. Open-Meteo's archive API only has data for hours
    that have already happened, which is exactly what training needs.
    """
    response = requests.get(
        "https://archive-api.open-meteo.com/v1/archive",
        params={
            "latitude": LAT,
            "longitude": LON,
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "hourly": ",".join(HOURLY_FIELDS),
            "timezone": "Europe/Budapest",
        },
        timeout=30,
    )
    response.raise_for_status()
    hourly = response.json()["hourly"]

    df = pd.DataFrame({"hour_bucket": pd.to_datetime(hourly["time"])} | {
        field: hourly[field] for field in HOURLY_FIELDS
    })
    # Open-Meteo returns local wall-clock strings (we asked for
    # timezone=Europe/Budapest) with no UTC offset attached, so pandas
    # parses them naive - localize explicitly so this is comparable to the
    # rest of the pipeline's tz-aware Budapest timestamps.
    df["hour_bucket"] = df["hour_bucket"].dt.tz_localize(BUDAPEST_TZ)
    return df


class LiveWeather:
    """
    Cached current-conditions reading for live prediction requests -
    weather doesn't change stop to stop, so there's no reason to call
    Open-Meteo on every single click. Same single-slot-cache idea as
    FutarClient.vehiclesNear() on the Java side, just with a much longer
    TTL matching how slowly weather actually changes rather than how often
    BKK's API refreshes.

    Falls back to the last successfully cached reading (even if stale) on
    a request failure rather than raising - a live prediction degrading to
    "slightly outdated weather" is a much better failure mode than the
    whole prediction breaking because a third-party weather API had a
    momentary hiccup.
    """

    CACHE_TTL_SECONDS = 1800  # 30 min - weather doesn't need to be fresher than this.

    def __init__(self):
        self._cached: dict[str, float] | None = None
        self._cached_at = 0.0

    def current(self) -> dict[str, float]:
        if self._cached is not None and time.time() - self._cached_at < self.CACHE_TTL_SECONDS:
            return self._cached

        try:
            response = requests.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": LAT,
                    "longitude": LON,
                    "current": ",".join(HOURLY_FIELDS),
                    "timezone": "Europe/Budapest",
                },
                timeout=10,
            )
            response.raise_for_status()
            current = response.json()["current"]
            self._cached = {field: float(current[field]) for field in HOURLY_FIELDS}
            self._cached_at = time.time()
        except requests.RequestException as e:
            if self._cached is None:
                raise  # no fallback available - first-ever call failed, nothing to degrade to.
            print(f"Weather fetch failed ({e}), serving stale cached reading from earlier")

        return self._cached
