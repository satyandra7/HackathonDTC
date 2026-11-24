"""Stages 2-3: match each vehicle run to a trip in the official static GTFS and turn its GPS track into stop
passing times.

1. Split each vehicle's cleaned pings into runs: a new run starts when trip_id changes or after a gap
   longer than RUN_GAP_S.
2. Candidate GTFS trips for a run: the GTFS trips whose route_id equals the feed's route_id (the official feed
   uses the same route IDs as the GPS data), active on that service date. The feed's trip_id is
   ROUTE_HH_MM[_NNNN] (assigned by the Transport Stack pipeline, not taken from GTFS). Candidates start within
   START_WINDOW_S of HH:MM (START_WINDOW_WIDE_S if none).
3. Each candidate pattern (stop sequence, usually one per direction) is scored by how many of its stops
   the track passes within SNAP_RADIUS_M, moving forward only. The best pattern wins, and within it the
   trip with the nearest start time (compared on a 24 h circle: 00:15 in a trip_id = 24:15:00 in GTFS).
4. A stop's passing time is read where the track comes closest to the stop, interpolated between two
   consecutive pings. Times must not go backwards. Unmatched stops between matched ones are interpolated
   by distance (observed = false).

Output: <data root>/clean/stop_events/service_date=YYYY-MM-DD/part.parquet, plus clean/qc/stop_events_<day>.json.

Usage
  .venv/bin/python pipeline/02_stop_events.py --day 2026-10-21
  .venv/bin/python pipeline/02_stop_events.py --train-test --workers 4      # train + test days from config
"""

from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import os
import sys
import time
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from transit import config as C  # noqa: E402
from transit.gtfs import GTFS  # noqa: E402
from transit.matching import DayRuns, circ_gap, monotone, parse_start, snap  # noqa: E402

G: GTFS | None = None              # loaded once in the parent, shared with forked workers


def process_day(day: str) -> dict:
    g = G
    out_dir = C.STOP_EVENTS / f"service_date={day}"
    out_file = out_dir / "part.parquet"
    qc_file = C.QC / f"stop_events_{day}.json"
    if out_file.exists() and qc_file.exists():
        return {"day": day, "skipped": True}
    t0 = time.time()
    try:
        r = DayRuns(day)
    except FileNotFoundError as e:
        return {"day": day, "error": str(e)}

    active = g.active_trip_mask(day)
    _, used = g.services_on(day)
    cand_of = {route: t[active[t]] for route, t in g.trips_by_route.items()}

    qc = {"day": day, "gtfs_date_used": used, "route_mode": "route_id",
          "pings": int(len(r.vk)), "runs": int(len(r)), "short": 0, "route_not_in_gtfs": 0,
          "no_candidate_trip": 0, "low_match": 0, "kept": 0, "events": 0}
    cols: dict[str, list] = {k: [] for k in ("run", "veh", "rt_trip", "rt_route", "trip", "pos", "t", "obs", "frac")}
    for ri, (s, e) in enumerate(zip(r.starts, r.ends)):
        if e - s < C.MIN_PINGS_PER_RUN:
            qc["short"] += 1
            continue
        route = str(r.rid[s])
        cand = cand_of.get(route)
        if cand is None or cand.size == 0:
            qc["route_not_in_gtfs"] += 1
            continue
        rt_trip = r.tmap[r.tk[s]]
        st = parse_start(rt_trip)
        if st is None:
            st = int(r.ts[s] - r.midnight)
        gap = circ_gap(g.trip_start[cand], st)
        c = cand[gap <= C.START_WINDOW_S]
        if c.size == 0:
            c = cand[gap <= C.START_WINDOW_WIDE_S]
        if c.size == 0:
            qc["no_candidate_trip"] += 1
            continue
        best = None
        for pid in np.unique(np.array([g.trip_pattern[t] for t in c])):
            pat = g.patterns[pid]
            times = monotone(snap(r.px[s:e], r.py[s:e], r.ts[s:e], r.te[s:e],
                                  g.stop_x[pat.stops], g.stop_y[pat.stops], C.SNAP_RADIUS_M))
            m = int(np.count_nonzero(~np.isnan(times)))
            if best is None or m > best[0]:
                best = (m, pid, times)
        m, pid, times = best
        pat = g.patterns[pid]
        frac = m / len(pat.stops)
        if m < C.MIN_MATCHED_STOPS or frac < C.MIN_MATCHED_FRAC:
            qc["low_match"] += 1
            continue
        obs = ~np.isnan(times)
        idx = np.flatnonzero(obs)
        lo, hi = idx[0], idx[-1]
        # trip: nearest scheduled start, using the observed time at the first matched stop as a check
        in_pat = np.array([t for t in c if g.trip_pattern[t] == pid], dtype=np.int64)
        trip = int(in_pat[np.argmin(circ_gap(g.trip_start[in_pat], st))])
        seg = np.arange(lo, hi + 1)
        tt = np.interp(pat.cumdist[seg], pat.cumdist[obs], times[obs])
        k = len(seg)
        cols["run"].append(np.full(k, ri, np.int32))
        cols["veh"].append(np.full(k, r.vk[s], np.int32))
        cols["rt_trip"].append(np.full(k, r.tk[s], np.int32))
        cols["rt_route"].append(np.full(k, r.rid[s], np.int64))
        cols["trip"].append(np.full(k, trip, np.int32))
        cols["pos"].append(seg.astype(np.int16))
        cols["t"].append(tt)
        cols["obs"].append(obs[seg])
        cols["frac"].append(np.full(k, frac, np.float32))
        qc["kept"] += 1
        qc["events"] += k

    out_dir.mkdir(parents=True, exist_ok=True)
    cat = {k: (np.concatenate(v) if v else np.array([], dtype=np.int32)) for k, v in cols.items()}
    trip = cat["trip"].astype(np.int64)
    pos = cat["pos"].astype(np.int64)
    n = trip.size
    pat_of = np.array(g.trip_pattern, dtype=np.int64)[trip] if n else np.array([], np.int64)
    stop_idx = np.array([g.patterns[p].stops[q] for p, q in zip(pat_of, pos)], dtype=np.int64)
    sched = np.array([g.trip_sched[t][q] for t, q in zip(trip, pos)], dtype=np.int64)
    obj = lambda xs: np.array(xs, dtype=object)  # noqa: E731
    tbl = pa.table({
        "service_date": pa.array([day] * n, pa.string()),
        "run_id": pa.array(cat["run"], pa.int32()),
        "vehicle_id": pa.array(obj(r.vmap)[cat["veh"]] if n else [], pa.string()),
        "rt_trip_id": pa.array(obj(r.tmap)[cat["rt_trip"]] if n else [], pa.string()),
        "rt_route_id": pa.array(cat["rt_route"].astype(str) if n else [], pa.string()),
        "gtfs_trip_id": pa.array(obj(g.trip_ids)[trip] if n else [], pa.string()),
        "pattern_id": pa.array(pat_of, pa.int32()),
        "route_id": pa.array(obj(g.trip_route)[trip] if n else [], pa.string()),
        "stop_pos": pa.array(pos, pa.int16()),
        "stop_id": pa.array(obj(g.stop_ids)[stop_idx] if n else [], pa.string()),
        "t_utc": pa.array(cat["t"], pa.float64()),
        "sched_s": pa.array(sched, pa.int32()),
        "observed": pa.array(cat["obs"], pa.bool_()),
        "frac_matched": pa.array(cat["frac"], pa.float32()),
    })
    pq.write_table(tbl, out_file, compression="zstd")
    with_route = qc["runs"] - qc["short"] - qc["route_not_in_gtfs"]
    qc["match_rate_of_runs_with_route"] = round(qc["kept"] / with_route, 3) if with_route else 0.0
    qc["match_rate_of_runs_10plus_pings"] = round(qc["kept"] / max(1, qc["runs"] - qc["short"]), 3)
    qc["observed_share"] = round(float(cat["obs"].mean()), 3) if n else 0.0
    qc["seconds"] = round(time.time() - t0, 1)
    C.QC.mkdir(parents=True, exist_ok=True)
    qc_file.write_text(json.dumps(qc, indent=2))
    return qc


def main() -> None:
    global G
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    grp = ap.add_mutually_exclusive_group(required=True)
    grp.add_argument("--day", action="append", help="service date YYYY-MM-DD (repeatable)")
    grp.add_argument("--train-test", action="store_true", help="all train and test days from transit/config.py")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--force", action="store_true", help="redo days that already have output")
    args = ap.parse_args()
    days = C.TRAIN_DAYS + C.TEST_DAYS if args.train_test else args.day

    t0 = time.time()
    G = GTFS(C.GTFS_DIR)
    print(f"GTFS loaded in {time.time() - t0:.1f}s: {G.summary()}; "
          "route mode: route_id", flush=True)
    if args.force:
        for d in days:
            for f in (C.STOP_EVENTS / f"service_date={d}" / "part.parquet", C.QC / f"stop_events_{d}.json"):
                f.unlink(missing_ok=True)
    w = max(1, min(args.workers, len(days)))
    if w == 1:
        results = [process_day(d) for d in days]
    else:
        with mp.get_context("fork").Pool(w) as pool:
            results = pool.map(process_day, days)
    for res in results:
        print(json.dumps(res), flush=True)


if __name__ == "__main__":
    os.nice(5)
    main()
