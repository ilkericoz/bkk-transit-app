"""
Archive old vehicle_position_snapshots days to Parquet (2026-10-11)

The table grows ~1.7 GB/day in Postgres. A compression benchmark on one day
(8 Oct, 7.56M rows, ~1.9 GB in Postgres) found Parquet + zstd level 22 with
rows sorted by (vehicle_id, recorded_at) the smallest lossless format: 113 MB,
~17x smaller (xz -9e on CSV 125 MB, zstd -19 130 MB, gzip -9 282 MB).

One file per Budapest calendar day (by recorded_at), every column kept as is;
recorded_at is stored as a UTC timestamp. Days are exported with COPY in
6-hour slices, pausing while the live collector lags (same check as
collector_lag_scan.py).

A day only counts as archived when a fingerprint computed by Postgres matches
the same fingerprint computed from the written file: row count, sum/min/max
of id, sum of id^2, non-null count of every column and sums of the numeric
columns. Matching days go to manifest.jsonl with that fingerprint, which is
what a later delete must check against.

Export and --verify are read-only on the database. Usage (from bkk-delay-service/, host venv):
    python scripts/archive_snapshots.py --before 2026-09-27   # export / resume
    python scripts/archive_snapshots.py --verify               # re-check all files vs DB
    python scripts/archive_snapshots.py --delete [--max-days N] # delete archived days from DB
"""

import argparse
import json
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import psycopg2
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.csv as pacsv
import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).resolve().parent))
from live_model_comparison import DB_CONFIG

ARCHIVE_DIR = Path(r"E:\bkk-archive\vehicle_position_snapshots")
MANIFEST_PATH = ARCHIVE_DIR / "manifest.jsonl"
BUDAPEST = ZoneInfo("Europe/Budapest")
LIVE_LAG_PAUSE_SECONDS = 30

# Column order of the COPY output and of the Parquet file. recorded_at leaves
# Postgres as epoch microseconds (exact for timestamp(6)) and becomes a UTC
# timestamp here, which avoids parsing "+02" offsets.
SCHEMA = pa.schema([
    ("id", pa.int64()), ("bearing", pa.float64()), ("label", pa.string()),
    ("last_update_time", pa.int64()), ("lat", pa.float64()), ("lon", pa.float64()),
    ("recorded_at", pa.int64()), ("route_id", pa.string()), ("vehicle_id", pa.string()),
    ("vehicle_route_type", pa.string()), ("service_date", pa.string()), ("status", pa.string()),
    ("stop_distance_percent", pa.int32()), ("stop_id", pa.string()), ("stop_sequence", pa.int32()),
    ("trip_id", pa.string()), ("deviated", pa.bool_()), ("stale", pa.bool_()),
])
SELECT_COLUMNS = ", ".join(
    "(extract(epoch FROM recorded_at) * 1000000)::bigint" if f.name == "recorded_at" else f.name
    for f in SCHEMA)
INT_SUM_COLUMNS = ["last_update_time", "stop_distance_percent", "stop_sequence"]
FLOAT_SUM_COLUMNS = ["bearing", "lat", "lon"]
FINGERPRINT_KEYS = (["rows", "id_sum", "id_sq_sum", "id_min", "id_max"]
                    + [f"nonnull_{f.name}" for f in SCHEMA]
                    + [f"sum_{c}" for c in INT_SUM_COLUMNS + FLOAT_SUM_COLUMNS]
                    + ["count_deviated", "count_stale"])

LIVE_LAG_QUERY = """
    SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY extract(epoch FROM recorded_at) - last_update_time)
    FROM vehicle_position_snapshots WHERE recorded_at >= now() - interval '1 minute'
"""
FINGERPRINT_QUERY = f"""
    SELECT count(*), sum(id), sum(id::numeric * id), min(id), max(id),
           {", ".join(f"count({f.name})" for f in SCHEMA)},
           {", ".join(f"sum({c})" for c in INT_SUM_COLUMNS)},
           {", ".join(f"sum({c})" for c in FLOAT_SUM_COLUMNS)},
           count(*) FILTER (WHERE deviated), count(*) FILTER (WHERE stale)
    FROM vehicle_position_snapshots
    WHERE recorded_at >= %(start)s AND recorded_at < %(end)s
"""


def day_bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, datetime.min.time(), BUDAPEST)
    end = datetime.combine(day + timedelta(days=1), datetime.min.time(), BUDAPEST)
    return start, end


def wait_for_collector(cur) -> None:
    while True:
        cur.execute(LIVE_LAG_QUERY)
        lag = cur.fetchone()[0]
        if lag is None or lag <= LIVE_LAG_PAUSE_SECONDS:
            return
        print(f"  collector lag {lag:.0f} s - pausing", flush=True)
        time.sleep(30)


def db_fingerprint(cur, day: date) -> dict:
    start, end = day_bounds(day)
    cur.execute(FINGERPRINT_QUERY, {"start": start, "end": end})
    row = list(cur.fetchone())
    fp = {"rows": row.pop(0), "id_sum": int(row.pop(0) or 0), "id_sq_sum": int(row.pop(0) or 0),
          "id_min": row.pop(0), "id_max": row.pop(0)}
    for f in SCHEMA:
        fp[f"nonnull_{f.name}"] = row.pop(0)
    for c in INT_SUM_COLUMNS:
        fp[f"sum_{c}"] = int(row.pop(0) or 0)
    for c in FLOAT_SUM_COLUMNS:
        fp[f"sum_{c}"] = float(row.pop(0) or 0.0)
    fp["count_deviated"] = row.pop(0)
    fp["count_stale"] = row.pop(0)
    return fp


def file_fingerprint(path: Path) -> dict:
    t = pq.read_table(path)
    ids = t["id"].cast(pa.int64())
    id_list = ids.to_pylist()
    fp = {"rows": t.num_rows, "id_sum": sum(id_list), "id_sq_sum": sum(i * i for i in id_list),
          "id_min": pc.min(ids).as_py(), "id_max": pc.max(ids).as_py()}
    for f in SCHEMA:
        fp[f"nonnull_{f.name}"] = t.num_rows - t[f.name].null_count
    for c in INT_SUM_COLUMNS:
        fp[f"sum_{c}"] = int(pc.sum(t[c].cast(pa.int64())).as_py() or 0)
    for c in FLOAT_SUM_COLUMNS:
        fp[f"sum_{c}"] = float(pc.sum(t[c]).as_py() or 0.0)
    fp["count_deviated"] = pc.sum(t["deviated"].cast(pa.int64())).as_py() or 0
    fp["count_stale"] = pc.sum(t["stale"].cast(pa.int64())).as_py() or 0
    return fp


def compare(db_fp: dict, file_fp: dict) -> list[str]:
    """Exact match on everything except float sums, which differ only by summation order."""
    problems = []
    for key, expected in db_fp.items():
        got = file_fp[key]
        if isinstance(expected, float):
            if abs(got - expected) > 1e-9 * max(1.0, abs(expected)):
                problems.append(f"{key}: db {expected!r} file {got!r}")
        elif got != expected:
            problems.append(f"{key}: db {expected!r} file {got!r}")
    return problems


def read_manifest() -> dict[str, dict]:
    if not MANIFEST_PATH.exists():
        return {}
    entries = (json.loads(line) for line in MANIFEST_PATH.read_text().splitlines() if line.strip())
    return {e["day"]: e for e in entries}


def export_day(conn, day: date) -> dict:
    tmp_csv = ARCHIVE_DIR / f"_{day}.csv.tmp"
    tmp_parquet = ARCHIVE_DIR / f"_{day}.parquet.tmp"
    final = ARCHIVE_DIR / f"{day}.parquet"
    start, end = day_bounds(day)
    t0 = time.time()
    with conn.cursor() as cur, open(tmp_csv, "wb") as out:
        slice_start = start
        while slice_start < end:
            slice_end = min(slice_start + timedelta(hours=6), end)
            wait_for_collector(cur)
            query = cur.mogrify(
                f"COPY (SELECT {SELECT_COLUMNS} FROM vehicle_position_snapshots "
                "WHERE recorded_at >= %s AND recorded_at < %s) TO STDOUT WITH CSV",
                (slice_start, slice_end)).decode()
            cur.copy_expert(query, out)
            slice_start = slice_end
    if tmp_csv.stat().st_size == 0:  # e.g. 18 Sep: collector was down all day
        tmp_csv.unlink()
        print(f"{day}: no rows, skipped", flush=True)
        return {}

    table = pacsv.read_csv(
        tmp_csv,
        read_options=pacsv.ReadOptions(column_names=SCHEMA.names),
        convert_options=pacsv.ConvertOptions(
            column_types=SCHEMA, true_values=["t"], false_values=["f"],
            null_values=[""], strings_can_be_null=True, quoted_strings_can_be_null=False))
    table = table.set_column(
        table.schema.get_field_index("recorded_at"), "recorded_at",
        table["recorded_at"].cast(pa.timestamp("us", tz="UTC")))
    table = table.sort_by([("vehicle_id", "ascending"), ("recorded_at", "ascending")])
    pq.write_table(table, tmp_parquet, compression="zstd", compression_level=22,
                   row_group_size=1_000_000)
    del table
    tmp_csv.unlink()

    with conn.cursor() as cur:
        wait_for_collector(cur)
        db_fp = db_fingerprint(cur, day)
    problems = compare(db_fp, file_fingerprint(tmp_parquet))
    if problems:
        raise RuntimeError(f"{day}: file does not match DB:\n  " + "\n  ".join(problems))
    tmp_parquet.replace(final)
    entry = {"day": str(day), "file": final.name, "bytes": final.stat().st_size,
             "range_start": start.isoformat(), "range_end": end.isoformat(),
             "archived_at": datetime.now(BUDAPEST).isoformat(timespec="seconds"), **db_fp}
    with open(MANIFEST_PATH, "a") as m:
        m.write(json.dumps(entry) + "\n")
    print(f"{day}: {db_fp['rows']:,} rows -> {entry['bytes'] / 1e6:.0f} MB, "
          f"verified, {time.time() - t0:.0f} s", flush=True)
    return entry


def export(before: date) -> None:
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    for stale_tmp in ARCHIVE_DIR.glob("_*.tmp"):
        stale_tmp.unlink()
    done = read_manifest()
    with psycopg2.connect(**DB_CONFIG) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SELECT min(recorded_at) FROM vehicle_position_snapshots")
            first = cur.fetchone()[0].astimezone(BUDAPEST).date()
        day = first
        while day < before:
            if str(day) in done:
                print(f"{day}: already archived", flush=True)
            else:
                export_day(conn, day)
            day += timedelta(days=1)
    print("export finished", flush=True)


def verify() -> None:
    """Re-read every archived file and compare to the manifest and to the DB as it is now."""
    bad = 0
    with psycopg2.connect(**DB_CONFIG) as conn, conn.cursor() as cur:
        for day_str, entry in sorted(read_manifest().items()):
            file_fp = file_fingerprint(ARCHIVE_DIR / entry["file"])
            wait_for_collector(cur)
            db_fp = db_fingerprint(cur, date.fromisoformat(day_str))
            manifest_fp = {k: entry[k] for k in db_fp}
            problems = compare(manifest_fp, file_fp) + compare(manifest_fp, db_fp)
            print(f"{day_str}: {'OK' if not problems else 'MISMATCH ' + '; '.join(problems)}", flush=True)
            bad += bool(problems)
    print(f"verify finished, {bad} mismatching day(s)", flush=True)


def delete(max_days: int | None = None) -> None:
    """Delete archived days from Postgres, one transaction per day.

    A day is deleted only when its file and the DB both still match the
    manifest fingerprint, and committed only when DELETE removed exactly the
    manifest's row count; anything else rolls back and stops. Deleted days are
    logged to deleted.jsonl and skipped on a rerun.
    """
    deleted_path = ARCHIVE_DIR / "deleted.jsonl"
    already = set()
    if deleted_path.exists():
        already = {json.loads(line)["day"] for line in deleted_path.read_text().splitlines() if line.strip()}
    conn = psycopg2.connect(**DB_CONFIG)
    try:
        todo = [(d, e) for d, e in sorted(read_manifest().items()) if d not in already]
        for day_str, entry in todo[:max_days]:
            t0 = time.time()
            file_problems = compare({k: entry[k] for k in FINGERPRINT_KEYS},
                                    file_fingerprint(ARCHIVE_DIR / entry["file"]))
            if file_problems:
                raise RuntimeError(f"{day_str}: archive file no longer matches manifest: {file_problems}")
            with conn.cursor() as cur:
                wait_for_collector(cur)
                conn.commit()
                db_fp = db_fingerprint(cur, date.fromisoformat(day_str))
                db_problems = compare({k: entry[k] for k in FINGERPRINT_KEYS}, db_fp)
                if db_problems:
                    conn.rollback()
                    raise RuntimeError(f"{day_str}: DB no longer matches manifest: {db_problems}")
                cur.execute("DELETE FROM vehicle_position_snapshots "
                            "WHERE recorded_at >= %s AND recorded_at < %s",
                            (entry["range_start"], entry["range_end"]))
                if cur.rowcount != entry["rows"]:
                    conn.rollback()
                    raise RuntimeError(f"{day_str}: DELETE matched {cur.rowcount} rows, "
                                       f"manifest has {entry['rows']} - rolled back")
            conn.commit()
            with open(deleted_path, "a") as d:
                d.write(json.dumps({"day": day_str, "rows": entry["rows"],
                                    "deleted_at": datetime.now(BUDAPEST).isoformat(timespec="seconds")}) + "\n")
            print(f"{day_str}: deleted {entry['rows']:,} rows (matched manifest), {time.time() - t0:.0f} s", flush=True)
    finally:
        conn.close()
    print("delete finished", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--before", type=date.fromisoformat,
                        help="archive Budapest days strictly before this date")
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--delete", action="store_true",
                        help="delete archived days from Postgres (each checked against the manifest)")
    parser.add_argument("--max-days", type=int, help="with --delete: stop after this many days")
    args = parser.parse_args()
    if args.verify:
        verify()
    elif args.delete:
        delete(args.max_days)
    elif args.before:
        export(args.before)
    else:
        parser.error("give --before YYYY-MM-DD or --verify")
