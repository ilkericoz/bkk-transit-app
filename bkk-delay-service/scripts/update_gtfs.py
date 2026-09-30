"""
Daily BKK timetable (static GTFS) update, added 2026-09-25 - run by a
Windows scheduled task at 04:30 (see ENGINEERING_NOTES.md), or by hand.

Why: BKK publishes timetable updates every few days, and the live feed
follows the newest one. Our copy went stale silently once: within two weeks
the share of live BKK trips it recognised fell from 97% to 91%, and those
vehicles got no prediction, no training label and no map colour.

What one run does:
  1. Download BKK's current timetable. Same version as ours -> log, stop.
  2. Check it: all required files, still valid, a plausible number of stop
     times, and it must recognise at least as many live BKK trips as ours
     (within MATCH_TOLERANCE points). Otherwise keep ours, log why, stop.
  3. Append the stop times of trips that disappear from the timetable to
     retired_stop_times.csv.gz (see gtfs_schedule.RETIRED_STOP_TIMES_PATH),
     so past days stay resolvable for label rebuilds.
  4. Install the new files into gtfs-data/raw and restart delay-service and
     backend (collection pauses for under a minute).
  5. If the delay service doesn't come back healthy, put the previous
     timetable and retired file back and restart again.
Every run appends to gtfs-data/update_log.txt. The downloaded zips are kept
in gtfs-data/archive/ (last KEEP_ZIPS), which is also what a rollback uses.

Usage (from bkk-delay-service/, host venv): python scripts/update_gtfs.py
  --dry-run  run the download and all checks, change nothing
  --force    run the checks even if the version is unchanged (with --dry-run: a test)
Exit code 0 = up to date or updated, 1 = rejected or rolled back.
"""

import io
import json
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile
from datetime import date, datetime
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parents[2]
GTFS = REPO / "bkk-backend" / "gtfs-data"
RAW = GTFS / "raw"
ARCHIVE = GTFS / "archive"
RETIRED = GTFS / "retired_stop_times.csv.gz"
LOG = GTFS / "update_log.txt"

GTFS_URL = "https://go.bkk.hu/api/static/v1/public-gtfs/budapest_gtfs.zip"
VEHICLES_URL = "http://localhost:8080/api/vehicles?lat=47.4979&lon=19.0402&radius=25000"
HEALTH_URL = "http://localhost:8000/health"

REQUIRED_FILES = ["agency.txt", "routes.txt", "stops.txt", "trips.txt", "stop_times.txt",
                  "calendar_dates.txt", "feed_info.txt"]
STOP_TIME_COLUMNS = ["trip_id", "arrival_time", "departure_time", "stop_id", "stop_sequence"]
MIN_STOP_TIME_ROWS = 3_000_000   # the 25 Sep 2026 file had ~6.5M; far fewer means a broken download
MATCH_TOLERANCE = 1.0            # percentage points the new file may recognise fewer live trips by
KEEP_ZIPS = 5
HEALTH_TIMEOUT_SECONDS = 300


def log(message: str) -> None:
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S}  {message}"
    print(line, flush=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def feed_info(text: str) -> dict:
    return pd.read_csv(io.StringIO(text), dtype=str).iloc[0].to_dict()


def live_bkk_trip_ids() -> list[str] | None:
    """Trip ids of BKK vehicles on the live map now, or None if the backend is down."""
    try:
        with urllib.request.urlopen(VEHICLES_URL, timeout=30) as response:
            vehicles = json.load(response)
    except OSError:
        return None
    vehicles = vehicles if isinstance(vehicles, list) else vehicles.get("vehicles", [])
    return [v["tripId"].removeprefix("BKK_") for v in vehicles if (v.get("tripId") or "").startswith("BKK_")]


def match_rate(trip_ids: list[str], known: set[str]) -> float:
    return 100 * sum(t in known for t in trip_ids) / len(trip_ids)


def docker() -> str:
    return shutil.which("docker") or r"C:\Program Files\Docker\Docker\resources\bin\docker.exe"


def restart_services() -> bool:
    """Restart both services and wait until the delay service has its schedule loaded."""
    subprocess.run([docker(), "compose", "restart", "delay-service", "backend"], cwd=REPO, check=True,
                   capture_output=True)
    deadline = time.time() + HEALTH_TIMEOUT_SECONDS
    while time.time() < deadline:
        time.sleep(10)
        try:
            with urllib.request.urlopen(HEALTH_URL, timeout=10) as response:
                health = json.load(response)
            if health.get("schedule_loaded") and health.get("model_loaded"):
                return True
        except OSError:
            pass
    return False


def install(zip_path: Path) -> None:
    with zipfile.ZipFile(zip_path) as z:
        for name in z.namelist():
            (RAW / name).write_bytes(z.read(name))


def main(dry_run: bool = False, force: bool = False) -> int:
    ARCHIVE.mkdir(exist_ok=True)
    current = feed_info((RAW / "feed_info.txt").read_text(encoding="utf-8-sig"))

    download = ARCHIVE / "download.tmp"
    urllib.request.urlretrieve(GTFS_URL, download)
    with zipfile.ZipFile(download) as z:
        members = set(z.namelist())
        new = feed_info(z.read("feed_info.txt").decode("utf-8-sig")) if "feed_info.txt" in members else {}
        up_to_date = new.get("feed_version") == current["feed_version"] and not force

        # ---- 2. checks before touching anything
        problems = [f"missing {f}" for f in REQUIRED_FILES if f not in members]
        if not problems and not up_to_date:
            if new["feed_end_date"] < date.today().strftime("%Y%m%d"):
                problems.append(f"already expired ({new['feed_end_date']})")
            new_trips = set(pd.read_csv(io.BytesIO(z.read("trips.txt")), usecols=["trip_id"], dtype=str)["trip_id"])
            with z.open("stop_times.txt") as f:
                rows = sum(1 for _ in f) - 1
            if rows < MIN_STOP_TIME_ROWS:
                problems.append(f"only {rows:,} stop times")
    if up_to_date:  # (outside the with-block: Windows can't delete a file that is still open)
        download.unlink()
        log(f"up to date (version {current['feed_version']})")
        return 0
    if problems:
        download.unlink()
        log(f"REJECTED version {new.get('feed_version')}: " + "; ".join(problems))
        return 1

    current_trips = set(pd.read_csv(RAW / "trips.txt", usecols=["trip_id"], dtype=str)["trip_id"])
    live = live_bkk_trip_ids()
    if live:
        rate_current, rate_new = match_rate(live, current_trips), match_rate(live, new_trips)
        if rate_new < rate_current - MATCH_TOLERANCE:
            download.unlink()
            log(f"REJECTED version {new['feed_version']}: recognises {rate_new:.1f}% of {len(live)} live BKK trips "
                f"vs {rate_current:.1f}% for ours")
            return 1
        match_note = f"live BKK trips recognised {rate_current:.1f}% -> {rate_new:.1f}% (of {len(live)})"
    else:
        match_note = "live match not checked (backend unreachable)"
    if dry_run:
        download.unlink()
        log(f"DRY RUN: version {new['feed_version']} passed all checks; {match_note}; "
            f"would retire {len(current_trips - new_trips):,} trips")
        return 0

    # ---- 3. retire the trips that disappear
    gone = current_trips - new_trips
    retiring = pd.concat(
        chunk[chunk["trip_id"].isin(gone)]
        for chunk in pd.read_csv(RAW / "stop_times.txt", usecols=STOP_TIME_COLUMNS, dtype=str, chunksize=500_000)
    )
    existing = pd.read_csv(RETIRED, dtype=str) if RETIRED.exists() else pd.DataFrame(columns=STOP_TIME_COLUMNS)
    # A trip retired again now comes from the newer timetable; one that is
    # back in the new timetable doesn't need to be kept here at all.
    existing = existing[~existing["trip_id"].isin(gone | new_trips)]
    retired = pd.concat([existing, retiring], ignore_index=True)

    # ---- 4. install: keep what a rollback needs, then swap
    previous_zip = ARCHIVE / f"budapest_gtfs_{current['feed_version']}.zip"
    if not previous_zip.exists():  # first run: zip up the timetable we are replacing
        with zipfile.ZipFile(previous_zip, "w", zipfile.ZIP_DEFLATED) as z:
            for f in RAW.iterdir():
                z.write(f, f.name)
    retired_backup = RETIRED.with_suffix(".gz.bak")
    if RETIRED.exists():
        shutil.copy2(RETIRED, retired_backup)
    retired.to_csv(RETIRED.with_suffix(".gz.tmp"), index=False, compression="gzip")
    RETIRED.with_suffix(".gz.tmp").replace(RETIRED)
    new_zip = ARCHIVE / f"budapest_gtfs_{new['feed_version']}.zip"
    download.replace(new_zip)
    install(new_zip)

    # ---- 5. restart, roll back if unhealthy
    if not restart_services():
        install(previous_zip)
        if retired_backup.exists():
            retired_backup.replace(RETIRED)
        healthy_again = restart_services()
        log(f"ROLLED BACK {new['feed_version']} -> {current['feed_version']}: delay service not healthy after "
            f"restart; after rollback healthy={healthy_again}")
        return 1

    for old in sorted(ARCHIVE.glob("budapest_gtfs_*.zip"), key=lambda p: p.stat().st_mtime)[:-KEEP_ZIPS]:
        if old not in (new_zip, previous_zip):
            old.unlink()
    log(f"UPDATED {current['feed_version']} -> {new['feed_version']} (valid to {new['feed_end_date']}): "
        f"{match_note}; retired +{len(gone):,} trips (file now {retired['trip_id'].nunique():,} trips)")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(dry_run="--dry-run" in sys.argv, force="--force" in sys.argv))
    except Exception as exc:  # noqa: BLE001 - a scheduled run must leave a trace in the log
        log(f"FAILED: {type(exc).__name__}: {exc}")
        sys.exit(1)
