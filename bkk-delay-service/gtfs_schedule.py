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

# Older timetables that are still consulted, newest first (added 2026-09-25),
# as sibling folders of GTFS_DIR. BKK publishes timetable updates every few
# days; the live feed follows the newest one, so GTFS_DIR must be kept current
# (on 2026-09-25 an 11-Sep copy matched only 91.3% of live BKK trips vs 97.1%
# for a fresh one). But a newer file also drops trips that ran in the past:
# 9.7% of the 09-24 training rows were on trips missing from the 25-Sep file.
# So the newest file wins, and older ones only fill in trips it doesn't have
# (the 161k trips present in both had identical times, so no conflicts).
FALLBACK_FEEDS = ["raw_2026-09-11"]


def stop_times_paths() -> list[Path]:
    """The current stop_times.txt, then the fallback feeds that exist."""
    fallbacks = [GTFS_DIR.parent / name / "stop_times.txt" for name in FALLBACK_FEEDS]
    return [STOP_TIMES_PATH] + [path for path in fallbacks if path.exists()]


def read_stop_times(usecols: list[str], trip_ids: set[str] | None = None) -> pd.DataFrame:
    """
    stop_times rows from the current feed, plus - only for trips no newer
    feed has - from the fallback feeds. Read in chunks, optionally keeping
    only `trip_ids`, so memory stays close to one feed's worth. All columns
    as strings except stop_sequence (int).
    """
    frames, covered = [], set()
    for path in stop_times_paths():
        parts = []
        for chunk in pd.read_csv(path, usecols=usecols, dtype=str, chunksize=500_000):
            if trip_ids is not None:
                chunk = chunk[chunk["trip_id"].isin(trip_ids)]
            if covered:
                chunk = chunk[~chunk["trip_id"].isin(covered)]
            if not chunk.empty:
                parts.append(chunk)
        if parts:
            feed = pd.concat(parts, ignore_index=True)
            frames.append(feed)
            covered |= set(feed["trip_id"])
    if not frames:
        return pd.DataFrame(columns=usecols)
    df = pd.concat(frames, ignore_index=True)
    df["stop_sequence"] = df["stop_sequence"].astype(int)
    return df


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
        usecols = ["trip_id", "stop_id", "stop_sequence", "arrival_time", "departure_time"]
        df = read_stop_times(usecols)  # current feed + older ones for trips it lacks
        # trip_id + stop_sequence together uniquely identify one scheduled
        # stop visit within one trip (see build_delay_dataset.py's
        # docstring) - indexing by that pair gives fast lookups without the
        # per-entry Python-object overhead a plain dict keyed by 5M+ tuples
        # would carry.
        indexed = df.set_index(["trip_id", "stop_sequence"]).sort_index()
        self._by_key = indexed["arrival_time"]
        # departure_time (added 2026-09-14, for the Google benchmark
        # sampler's anchored-origin need - see scheduled_departure) - kept
        # as its own column rather than reusing arrival_time, since the two
        # genuinely differ at timepoints with a scheduled layover (buses
        # holding to stay on schedule), even though they're usually equal.
        self._departure_by_key = indexed["departure_time"]
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

    def scheduled_departure(self, gtfs_trip_id: str, stop_sequence: int, service_date: str) -> datetime | None:
        """Same as scheduled_arrival but for departure_time - used to
        anchor the Google benchmark sampler's query to a specific real
        service's actual departure from an origin stop, rather than "now"
        from an arbitrary live GPS point (see google_benchmark_sampler.py
        for why that distinction turned out to matter)."""
        departure_time = self._lookup(self._departure_by_key, gtfs_trip_id, stop_sequence)
        if departure_time is None:
            return None
        return to_scheduled_datetime(service_date, departure_time)

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
