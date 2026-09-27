"""
Trains the long-range model (added 2026-09-27): a vehicle's delay several
stops ahead (1-25 stops, at most 30 scheduled minutes), predicted at the
moment it is confirmed at a stop. The setup that won the walk-forward
comparison in the thesis notes (22 test days from 2 Sep: 48.2 s mean
absolute error vs 58.8 s for "keep the current delay", better on 22/22
days): gradient-boosted trees on

  current delay, scheduled minutes / stops ahead, hour, weekday, stop
  number, route drift (mean delay change per scheduled minute on the
  route), vehicle type,
  path_hist   sum of each stretch's typical delay gain on the way (from
              service days before the row's own day),
  path_recent sum of the mean gain of any-route vehicles on those stretches
              in the 15 min before the prediction moment, path_cov = share
              of stretches that had such traffic,
  the vehicle ahead at the TARGET stop (same route, reached it before the
  prediction moment, <= 60 min ago): its delay there, how long ago, and
  the timetable gap to it.

main.py computes the same inputs live from the snapshots
(long_range_features, sharing pick_segment_recent / pick_vehicle_ahead with
the next-stop model); the lookup tables it needs travel inside the saved
file. Live, "typical gain" uses every day in the labels file (all of them
are before the day being predicted).

Also writes CHECK_PATH: 400 real cases from 23-24 Sep with the inputs as
computed here, for the train/serve consistency check.

Usage (from bkk-delay-service/, host venv; ~5-10 min, several GB of RAM -
run it in its own window):
    python scripts/train_long_range.py
"""
import hashlib
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from gtfs_schedule import read_stop_times  # noqa: E402

SERVICE_DIR = Path(__file__).resolve().parent.parent
DATA_PATH = SERVICE_DIR / "data" / "delay_labels.csv"
MODEL_PATH = SERVICE_DIR / "models" / "long_range_model.joblib"
CHECK_PATH = SERVICE_DIR / "data" / "long_range_check_sample.csv"

SAMPLE_PERCENT = 10          # of trip-days used as prediction references
STOPS_AHEAD = [1, 2, 3, 4, 6, 8, 10, 13, 16, 20, 25]
MAX_AHEAD = max(STOPS_AHEAD)
MAX_SCHEDULED_MINUTES = 30
RECENT_SECONDS = 15 * 60     # must match main.py's SEGMENT_RECENT_MINUTES
MIN_HIST_TRAVERSALS = 5
VEHICLE_AHEAD_MAX_MINUTES = 60
FEATURES = ["d_a", "sched_min", "k", "hour", "dow", "stop_sequence_a", "route_drift", "vtype",
            "path_hist", "path_recent", "path_cov",
            "has_ahead", "ahead_delay", "ahead_age_min", "ahead_gap_min"]
CHECK_DAYS, CHECK_STOPS_AHEAD, CHECK_SIZE = [20260923, 20260924], [3, 5, 10], 400
EPOCH = pd.Timestamp("1970-01-01", tz="UTC")
t0 = time.time()


def log(msg):
    print(f"[{time.time() - t0:5.0f}s] {msg}", flush=True)


def main() -> None:
    # ---- stop visits ----
    cols = ["trip_id", "route_id", "vehicle_route_type", "stop_id", "stop_sequence", "service_date",
            "scheduled_arrival", "actual_arrival", "delay_seconds"]
    df = pd.read_csv(DATA_PATH, usecols=cols, engine="pyarrow")
    df["stop_sequence"] = df["stop_sequence"].astype(int)
    actual = pd.to_datetime(df["actual_arrival"], utc=True)
    df["t"] = (actual - EPOCH).dt.total_seconds()
    # Exact integer microseconds for the recent-traffic window: vehicles polled
    # in the same collection batch are saved milliseconds apart, and a float
    # key (stretch * 1e7 + seconds) is only good to ~0.06 s at this size - it
    # once counted a vehicle 4.9 ms before the prediction moment as "same time"
    # (found by the train/serve consistency check, 2026-09-27).
    df["t_us"] = ((actual - EPOCH) // pd.Timedelta(microseconds=1)).astype("int64")
    df["ts"] = (pd.to_datetime(df["scheduled_arrival"], utc=True) - EPOCH).dt.total_seconds()
    df = df.sort_values(["trip_id", "service_date", "stop_sequence"], ignore_index=True)
    days = np.sort(df["service_date"].unique())
    df["day_idx"] = np.searchsorted(days, df["service_date"].to_numpy())
    log(f"loaded {len(df):,} stop visits, {len(days)} service days")

    key = df["trip_id"].astype(str) + "|" + df["service_date"].astype(str)
    consecutive = (key.eq(key.shift(1)) & df["stop_sequence"].eq(df["stop_sequence"].shift(1) + 1)).to_numpy()
    sampled = pd.util.hash_pandas_object(key, index=False).to_numpy() % 100 < SAMPLE_PERCENT
    del key
    sched = read_stop_times(["trip_id", "stop_sequence", "stop_id"], trip_ids=set(df.loc[sampled, "trip_id"].astype(str)))
    sched["stop_id"] = "BKK_" + sched["stop_id"]
    stops = pd.Index(pd.unique(pd.concat([df["stop_id"].astype(str), sched["stop_id"]], ignore_index=True)))
    n_stops = len(stops)
    df["stop_code"] = stops.get_indexer(df["stop_id"].astype(str)).astype(np.int64)
    df["route_code"] = pd.factorize(df["route_id"])[0].astype(np.int64)

    # ---- stretch traversals: recent-window index, expanding daily history, full table ----
    sc = df["stop_code"].to_numpy()
    seg_id = (np.roll(sc, 1) * n_stops + sc)[consecutive]
    gain_us = df["t_us"].to_numpy()[consecutive]
    gain_day = df["day_idx"].to_numpy()[consecutive]
    gain = (df["delay_seconds"] - df["delay_seconds"].shift(1)).to_numpy()[consecutive]
    # Dense stretch codes keep the combined integer key exact:
    # code * 1e13 + microseconds since t_base (a month is ~2.6e12 us).
    seg_unique, seg_dense = np.unique(seg_id, return_inverse=True)
    t_base = int(gain_us.min()) - 7200 * 10**6
    recent_key = seg_dense.astype(np.int64) * 10**13 + (gain_us - t_base)
    order = np.argsort(recent_key, kind="stable")
    recent_key, recent_cum = recent_key[order], np.concatenate([[0.0], np.cumsum(gain[order])])
    daily = pd.DataFrame({"seg": seg_id, "day": gain_day, "gain": gain}).groupby(["seg", "day"])["gain"].agg(["sum", "size"])
    hist_key = daily.index.get_level_values("seg").to_numpy() * 1000 + daily.index.get_level_values("day").to_numpy()
    hist_cum_sum = np.concatenate([[0.0], np.cumsum(daily["sum"].to_numpy())])
    hist_cum_n = np.concatenate([[0], np.cumsum(daily["size"].to_numpy())])
    full = pd.DataFrame({"seg": seg_id, "gain": gain}).groupby("seg")["gain"].agg(["mean", "size"])
    full = full[full["size"] >= MIN_HIST_TRAVERSALS]
    typical_gain = {(stops[s // n_stops], stops[s % n_stops]): float(v) for s, v in full["mean"].items()}
    log(f"{len(gain):,} stretch traversals; typical gain for {len(typical_gain):,} stretches")

    # ---- path features for every reference visit ----
    ref = df[sampled].copy()
    sched = sched.sort_values(["trip_id", "stop_sequence"], ignore_index=True)
    flat = stops.get_indexer(sched["stop_id"]).astype(np.int64)
    trip_start = pd.Series(np.arange(len(sched)), index=sched.index).groupby(sched["trip_id"]).first()
    trip_len = sched.groupby("trip_id")["stop_sequence"].max()
    ref["base"] = ref["trip_id"].astype(str).map(trip_start)
    ref["tlen"] = ref["trip_id"].astype(str).map(trip_len)
    ref = ref[ref["base"].notna()].reset_index(drop=True)
    ref["ri"] = np.arange(len(ref))
    base = ref["base"].to_numpy(np.int64) + ref["stop_sequence"].to_numpy() - 1
    seq, tlen = ref["stop_sequence"].to_numpy(), ref["tlen"].to_numpy()
    t_ref_us, day_ref = ref["t_us"].to_numpy(), ref["day_idx"].to_numpy()
    aligned = flat[base] == ref["stop_code"].to_numpy()
    log(f"timetable/observed stop agree at {100 * aligned.mean():.2f}% of reference visits")

    path = {name: np.zeros((len(ref), MAX_AHEAD + 1)) for name in ["recent", "cov", "hist", "hist_full"]}
    sums = {name: np.zeros(len(ref)) for name in ["recent", "cov", "hist", "hist_full"]}
    full_mean = full["mean"]
    for m in range(1, MAX_AHEAD + 1):
        valid = seq + m <= tlen
        idx = np.where(valid, base + m, base)
        sid = flat[idx - 1] * n_stops + flat[idx]
        pos = np.minimum(np.searchsorted(seg_unique, sid), len(seg_unique) - 1)
        known = seg_unique[pos] == sid  # stretches never traversed have no recent traffic
        q = pos.astype(np.int64) * 10**13 + (t_ref_us - t_base)
        hi = np.searchsorted(recent_key, q, "left")  # strictly before the prediction moment
        lo = np.searchsorted(recent_key, q - RECENT_SECONDS * 10**6, "left")
        n = np.where(known, hi - lo, 0)
        recent = np.where(n > 0, (recent_cum[hi] - recent_cum[lo]) / np.maximum(n, 1), 0.0)
        hh, hl = np.searchsorted(hist_key, sid * 1000 + day_ref, "left"), np.searchsorted(hist_key, sid * 1000, "left")
        hn = hist_cum_n[hh] - hist_cum_n[hl]
        hist = np.where(hn >= MIN_HIST_TRAVERSALS, (hist_cum_sum[hh] - hist_cum_sum[hl]) / np.maximum(hn, 1), 0.0)
        sums["recent"] += np.where(valid, recent, 0.0)
        sums["cov"] += valid & (n > 0)
        sums["hist"] += np.where(valid, hist, 0.0)
        sums["hist_full"] += np.where(valid, full_mean.reindex(sid).fillna(0.0).to_numpy(), 0.0)
        for name in sums:
            path[name][:, m] = sums[name] / (m if name == "cov" else 1)
    log("path features computed")

    # ---- pairs + the vehicle ahead at the target stop ----
    right = df.loc[sampled, ["trip_id", "service_date", "stop_sequence", "stop_code", "ts", "t", "delay_seconds"]]
    left_cols = ["trip_id", "service_date", "route_id", "route_code", "vehicle_route_type", "stop_sequence",
                 "ts", "t", "delay_seconds", "ri"]
    pairs = []
    for k in STOPS_AHEAD:
        left = ref[left_cols].rename(columns={c: c + "_a" for c in ["stop_sequence", "ts", "t", "delay_seconds"]})
        left["stop_sequence"] = left["stop_sequence_a"] + k
        pk = left.merge(right, on=["trip_id", "service_date", "stop_sequence"]).assign(k=k)
        ri = pk["ri"].to_numpy()
        for name in ["recent", "cov", "hist", "hist_full"]:
            pk[f"path_{name}"] = path[name][ri, k]
        pairs.append(pk)
    p = pd.concat(pairs, ignore_index=True)
    del pairs, path
    p["sched_min"] = (p["ts"] - p["ts_a"]) / 60
    p["actual_min"] = (p["t"] - p["t_a"]) / 60
    p = p[(p["sched_min"] > 0) & (p["sched_min"] <= MAX_SCHEDULED_MINUTES)
          & (p["actual_min"] > 0) & (p["actual_min"] <= p["sched_min"] + 30)].reset_index(drop=True)

    arrivals = pd.DataFrame({
        "bykey": df["route_code"].to_numpy() * n_stops + df["stop_code"].to_numpy(), "t_ahead": df["t"].to_numpy(),
        "ahead_trip": df["trip_id"].astype(str).to_numpy(), "ahead_delay": df["delay_seconds"].to_numpy(),
        "ts_ahead": df["ts"].to_numpy(),
    }).sort_values("t_ahead")
    q = pd.DataFrame({"bykey": p["route_code"].to_numpy() * n_stops + p["stop_code"].to_numpy(),
                      "t_q": p["t_a"].to_numpy(), "row": np.arange(len(p))}).sort_values("t_q")
    found = pd.merge_asof(q, arrivals, left_on="t_q", right_on="t_ahead", by="bykey",
                          direction="backward", allow_exact_matches=False).sort_values("row")
    age = (found["t_q"].to_numpy() - found["t_ahead"].to_numpy()) / 60
    same_trip = found["ahead_trip"].fillna("").astype(str).to_numpy() == p["trip_id"].astype(str).to_numpy()
    ok = found["t_ahead"].notna().to_numpy() & ~same_trip & (age <= VEHICLE_AHEAD_MAX_MINUTES)
    p["has_ahead"] = ok.astype(int)
    p["ahead_delay"] = np.where(ok, found["ahead_delay"].to_numpy(), 0.0)
    p["ahead_age_min"] = np.where(ok, age, 0.0)
    p["ahead_gap_min"] = np.where(ok, (p["ts"].to_numpy() - found["ts_ahead"].to_numpy()) / 60, 0.0)
    del arrivals, found, q

    local = (EPOCH + pd.to_timedelta(p["ts_a"], unit="s")).dt.tz_convert("Europe/Budapest")
    p["hour"], p["dow"] = local.dt.hour, local.dt.dayofweek
    p["d_a"], p["d_b"] = p["delay_seconds_a"], p["delay_seconds"]
    route_drift = ((p["d_b"] - p["d_a"]) / p["sched_min"]).groupby(p["route_id"]).mean()
    p["route_drift"] = p["route_id"].map(route_drift).fillna(0.0)
    vtypes = sorted(p["vehicle_route_type"].dropna().unique())
    p["vtype"] = p["vehicle_route_type"].astype("category").cat.set_categories(vtypes)
    log(f"{len(p):,} training pairs; vehicle ahead at target on {100 * p['has_ahead'].mean():.0f}%")

    # ---- fit on everything, save ----
    model = HistGradientBoostingRegressor(loss="absolute_error", max_iter=300, categorical_features=["vtype"],
                                          random_state=26)
    model.fit(p[FEATURES], p["d_b"])
    in_sample = (model.predict(p[FEATURES]) - p["d_b"]).abs().mean()
    log(f"fitted; in-sample MAE {in_sample:.1f} s (keep-delay {(p['d_a'] - p['d_b']).abs().mean():.1f} s)")

    MODEL_PATH.parent.mkdir(exist_ok=True)
    joblib.dump({
        "model": model, "features": FEATURES, "vehicle_types": vtypes,
        "route_drift": {str(r): float(v) for r, v in route_drift.items()},
        "typical_gain": typical_gain,
        "stops_ahead_trained": STOPS_AHEAD, "max_scheduled_minutes": MAX_SCHEDULED_MINUTES,
        "min_hist_traversals": MIN_HIST_TRAVERSALS,
        "trained_at": datetime.now(timezone.utc).isoformat(), "data": DATA_PATH.name,
        "data_sha256": hashlib.sha256(DATA_PATH.read_bytes()).hexdigest(), "training_pairs": len(p),
        "sample_percent": SAMPLE_PERCENT,
    }, MODEL_PATH)
    log(f"saved {MODEL_PATH} (sha256 {hashlib.sha256(MODEL_PATH.read_bytes()).hexdigest()[:12]})")

    # ---- consistency-check sample: inputs as live will compute them (typical gain from all days) ----
    check = p[p["service_date"].isin(CHECK_DAYS) & p["k"].isin(CHECK_STOPS_AHEAD)].sample(CHECK_SIZE, random_state=27).copy()
    check["path_hist"] = check["path_hist_full"]
    check["pred"] = model.predict(check[FEATURES])
    check[["trip_id", "route_id", "vehicle_route_type", "service_date", "stop_sequence_a", "stop_sequence", "k",
           "t_a", "ts_a", "d_a", "sched_min", "path_hist", "path_recent", "path_cov",
           "has_ahead", "ahead_delay", "ahead_age_min", "ahead_gap_min", "pred"]].to_csv(CHECK_PATH, index=False)
    log(f"consistency sample -> {CHECK_PATH}")


if __name__ == "__main__":
    main()
