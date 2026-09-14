"""
Static GTFS stop_times.txt schedule lookup - shared by build_delay_dataset.py
(batch label-building) and main.py's live /predict/from-vehicle endpoint
(added 2026-09-12, wiring the Spring Boot backend to actually call the
model). Both need the exact same (trip_id, stop_sequence, service_date) ->
scheduled_arrival logic; pulled out here so that logic lives in one place
instead of a batch copy and a live-serving copy slowly drifting apart.
"""

import os
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

BUDAPEST_TZ = ZoneInfo("Europe/Budapest")

# Overridable via GTFS_DATA_DIR (stage 6 / Docker-compose: the sidecar
# container isn't laid out on disk as a sibling of bkk-backend the way the
# two services are on a dev machine, so it mounts the host's gtfs-data/raw
# wherever it likes and points this at that mount instead). Defaults to the
# same relative traversal that's always worked for native/local dev.
GTFS_DIR = Path(os.environ.get(
    "GTFS_DATA_DIR",
    str(Path(__file__).resolve().parent.parent / "bkk-backend" / "gtfs-data" / "raw"),
))
STOP_TIMES_PATH = GTFS_DIR / "stop_times.txt"


def to_scheduled_datetime(service_date: str, arrival_time: str) -> datetime:
    """
    GTFS arrival_time is "HH:MM:SS" civil time *relative to service_date*,
    and GTFS deliberately allows hours >= 24 for trips past midnight (e.g.
    "25:10:00" for 01:10 the following day) rather than rolling the date -
    so this is parsed as an (hours, minutes, seconds) offset added onto
    service_date's midnight, not as a wall-clock time directly.
    """
    hours, minutes, seconds = (int(part) for part in arrival_time.split(":"))
    midnight = datetime.strptime(service_date, "%Y%m%d").replace(tzinfo=BUDAPEST_TZ)
    return midnight + timedelta(hours=hours, minutes=minutes, seconds=seconds)


class ScheduleLookup:
    """
    In-memory index of the *entire* static stop_times.txt (~5.08M rows,
    ~370MB on disk), keyed by (trip_id, stop_sequence) -> arrival_time
    string. Built once at process startup and kept resident for the life
    of the process - fine for a single-worker dev sidecar; a real
    multi-worker deployment would want this built once and shared (e.g. a
    small Postgres table, which the Java side already has the pattern for)
    rather than re-loaded per worker.

    Unlike build_delay_dataset.py's fetch_scheduled_times(), which only
    keeps rows for an already-known, already-observed set of trip_ids (a
    tiny fraction of the file), this has to answer for *any* live vehicle
    someone clicks on the map - there's no way to know the trip_id in
    advance - so it indexes the whole file rather than filtering first.
    """

    def __init__(self):
        usecols = ["trip_id", "stop_id", "stop_sequence", "arrival_time"]
        df = pd.read_csv(
            STOP_TIMES_PATH, usecols=usecols, dtype={"trip_id": str, "stop_id": str, "arrival_time": str}
        )
        df["stop_sequence"] = df["stop_sequence"].astype(int)
        # trip_id + stop_sequence together uniquely identify one scheduled
        # stop visit within one trip (see build_delay_dataset.py's
        # docstring) - indexing by that pair gives fast lookups without the
        # per-entry Python-object overhead a plain dict keyed by 5M+ tuples
        # would carry.
        indexed = df.set_index(["trip_id", "stop_sequence"]).sort_index()
        self._by_key = indexed["arrival_time"]
        # stop_id (added 2026-09-14, for the Google benchmark sampler's
        # look-further-ahead-than-one-stop need) - the *static* GTFS
        # stop_id, not the live feed's "BKK_"+stop_code value; callers that
        # need to match it back against a live VehiclePosition.stopId must
        # convert via stops.txt's own stop_id->stop_code column themselves.
        self._stop_id_by_key = indexed["stop_id"]

    def scheduled_arrival(self, gtfs_trip_id: str, stop_sequence: int, service_date: str) -> datetime | None:
        """
        None if this (trip_id, stop_sequence) isn't on the static schedule
        at all - the expected outcome for vehicles BKK's live feed surfaces
        that aren't actually BKK trips (MÁV-START/Volánbusz, see the
        2026-08-28 route-name investigation) or ones running off-schedule -
        not a bug to raise on, the caller is expected to handle it.
        """
        arrival_time = self._lookup(self._by_key, gtfs_trip_id, stop_sequence)
        if arrival_time is None:
            return None
        return to_scheduled_datetime(service_date, arrival_time)

    def stop_id_at(self, gtfs_trip_id: str, stop_sequence: int) -> str | None:
        """The static stop_id scheduled at this (trip_id, stop_sequence) -
        None if that stop_sequence doesn't exist on this trip (e.g. asking
        further ahead than the trip actually runs)."""
        return self._lookup(self._stop_id_by_key, gtfs_trip_id, stop_sequence)

    @staticmethod
    def _lookup(series: pd.Series, gtfs_trip_id: str, stop_sequence: int):
        try:
            value = series.loc[(gtfs_trip_id, stop_sequence)]
        except KeyError:
            return None
        if isinstance(value, pd.Series):
            # Defensive only - trip_id+stop_sequence is expected to be
            # unique in the static feed, but if it ever isn't, take the
            # first match rather than erroring the whole request.
            value = value.iloc[0]
        return value
