"""Stage 5: road geometry for each segment (stop -> next stop), so the map draws bus legs along the roads.

Each stretch is cut from its pattern's shape in the GTFS shapes.txt (see below). Stretches without a shape are
traced from the buses' own GPS: for every stop pair that buses were seen driving (from stop events), up to
CANDIDATES traversals are taken: the run's pings between the two stop passings, with the stops at both ends.
A traversal is rejected if it is longer than MAX_DETOUR x the straight line (+ SLACK_M), which removes GPS
jumps and detours; the remaining traversal with the median length becomes the segment's shape, lightly
simplified (points closer than MIN_STEP_M are dropped).

GTFS shapes: stops are projected onto the pattern's shape, moving forward, and the shape between two stops is
the stretch; GPS tracing only fills stretches without one.

Output: <model>/segment_shapes.parquet (from_stop, to_stop, lat[], lon[], n_candidates, source), used by the
planner to draw legs. Segments without a shape keep the straight line.

Usage: .venv/bin/python pipeline/05_shapes.py [--days ...] [--workers 4]
"""

from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import os
import sys
import time
from pathlib import Path

import duckdb
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from transit import config as C  # noqa: E402
from transit import geo  # noqa: E402
from transit.matching import DayRuns  # noqa: E402

DAYS = ["2026-10-21", "2026-10-22", "2026-10-23", "2026-10-24", "2026-10-25", "2026-10-27"]
CANDIDATES = 5
MAX_DETOUR = 2.0
SLACK_M = 150.0
MIN_STEP_M = 15.0


def day_traversals(day: str) -> dict[tuple[str, str], list[np.ndarray]]:
    """(from_stop, to_stop) -> up to CANDIDATES traversals as (k, 2) arrays of x, y metres."""
    f = C.STOP_EVENTS / f"service_date={day}" / "part.parquet"
    if not f.exists():
        return {}
    ev = duckdb.sql(f"""
        WITH e AS (SELECT run_id, vehicle_id, stop_id, t_utc, observed, stop_pos,
                          lead(stop_id) OVER w nxt, lead(t_utc) OVER w nt, lead(observed) OVER w nobs,
                          lead(stop_pos) OVER w npos
                   FROM read_parquet('{f}', hive_partitioning = false)
                   WINDOW w AS (PARTITION BY run_id ORDER BY stop_pos))
        SELECT vehicle_id, stop_id, nxt, t_utc, nt FROM e
        WHERE observed AND nobs AND npos = stop_pos + 1 AND nt > t_utc""").fetchnumpy()
    r = DayRuns(day)
    # pings are sorted by vehicle, then time: index range per vehicle (stop events' run_id is not a DayRuns index)
    vb = np.flatnonzero(np.r_[True, r.vk[1:] != r.vk[:-1]])
    vrange = {r.vmap[r.vk[i]]: (i, j) for i, j in zip(vb, np.r_[vb[1:], len(r.vk)])}
    out: dict[tuple[str, str], list[np.ndarray]] = {}
    for veh, a, b, t0, t1 in zip(ev["vehicle_id"], ev["stop_id"], ev["nxt"], ev["t_utc"], ev["nt"]):
        key = (str(a), str(b))
        lst = out.setdefault(key, [])
        if len(lst) >= CANDIDATES or veh not in vrange:
            continue
        s, e = vrange[veh]
        lo, hi = np.searchsorted(r.ts[s:e], t0, "right") + s, np.searchsorted(r.ts[s:e], t1, "left") + s
        lst.append(np.column_stack([r.px[lo:hi], r.py[lo:hi]]))
    return out


def gtfs_segment_shapes() -> dict[tuple[str, str], np.ndarray]:
    """(from_stop, to_stop) -> polyline (k, 2) in metres, cut from shapes.txt along each pattern."""
    from scipy.spatial import cKDTree
    from transit.gtfs import GTFS
    f = C.GTFS_DIR / "shapes.txt"
    if not f.exists():
        return {}
    g = GTFS(C.GTFS_DIR)
    tr = duckdb.sql(f"SELECT trip_id, shape_id FROM read_csv('{C.GTFS_DIR / 'trips.txt'}', all_varchar = true)").fetchnumpy()
    trip_shape = dict(zip(tr["trip_id"], tr["shape_id"]))
    sh = duckdb.sql(f"""SELECT shape_id, CAST(shape_pt_lat AS DOUBLE) la, CAST(shape_pt_lon AS DOUBLE) lo
                        FROM read_csv('{f}', all_varchar = true)
                        ORDER BY shape_id, CAST(shape_pt_sequence AS INTEGER)""").fetchnumpy()
    sid = sh["shape_id"]
    brk = np.flatnonzero(np.r_[True, sid[1:] != sid[:-1]])
    x, y = geo.to_xy(sh["la"], sh["lo"])
    shapes = {sid[i]: np.column_stack([x[i:j], y[i:j]]) for i, j in zip(brk, np.r_[brk[1:], len(sid)])}
    out: dict[tuple[str, str], np.ndarray] = {}
    for p in g.patterns:
        shp = trip_shape.get(g.trip_ids[p.trips[0]]) if len(p.trips) else None
        line = shapes.get(shp)
        if line is None or len(line) < 2:
            continue
        # densify to <= 20 m so stops project onto the shape precisely
        seg = np.hypot(*np.diff(line, axis=0).T)
        reps = np.maximum(1, np.ceil(seg / 20.0)).astype(int)
        dense = [line[:1]]
        for k in range(len(seg)):
            t = np.arange(1, reps[k] + 1)[:, None] / reps[k]
            dense.append(line[k] + t * (line[k + 1] - line[k]))
        line = np.vstack(dense)
        tree = cKDTree(line)
        idx, prev = [], 0
        for st in p.stops:
            q = np.array([g.stop_x[st], g.stop_y[st]])
            d, cand = tree.query(q, k=min(16, len(line)))
            ok = [c for dd, c in zip(np.atleast_1d(d), np.atleast_1d(cand)) if c >= prev and dd <= 150]
            c = min(ok) if ok else None
            idx.append(c)
            if c is not None:
                prev = c
        for k in range(len(p.stops) - 1):
            a, b = idx[k], idx[k + 1]
            if a is None or b is None or b <= a:
                continue
            pa_, pb = np.array([g.stop_x[p.stops[k]], g.stop_y[p.stops[k]]]), np.array([g.stop_x[p.stops[k + 1]], g.stop_y[p.stops[k + 1]]])
            key = (g.stop_ids[p.stops[k]], g.stop_ids[p.stops[k + 1]])
            out.setdefault(key, np.vstack([pa_, line[a:b + 1], pb]))
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", default=",".join(DAYS))
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()
    t_start = time.time()
    days = [d for d in args.days.split(",") if d]
    with mp.get_context("fork").Pool(max(1, min(args.workers, len(days)))) as pool:
        parts = pool.map(day_traversals, days)
    trav: dict[tuple[str, str], list[np.ndarray]] = {}
    for part in parts:
        for k, v in part.items():
            trav.setdefault(k, []).extend(v)

    stops = duckdb.sql(f"SELECT stop_id, stop_lat, stop_lon FROM read_csv('{C.GTFS_DIR / 'stops.txt'}', all_varchar = true)").fetchnumpy()
    sxy = {sid: geo.to_xy(float(la), float(lo)) for sid, la, lo in zip(stops["stop_id"], stops["stop_lat"], stops["stop_lon"])}
    rec = {"from_stop": [], "to_stop": [], "lat": [], "lon": [], "n_candidates": [], "source": []}
    rejected = 0
    gshapes = gtfs_segment_shapes()
    for (a, b), line in gshapes.items():
        trav[(a, b)] = [("gtfs", line)]
    for (a, b), cands in trav.items():
        if a not in sxy or b not in sxy:
            continue
        pa_, pb = np.array(sxy[a], dtype=float), np.array(sxy[b], dtype=float)
        straight = float(np.hypot(*(pb - pa_)))
        good = []
        src = "gtfs" if cands and isinstance(cands[0], tuple) else "gps"
        for c in cands:
            line = c[1] if src == "gtfs" else (np.vstack([pa_, c, pb]) if len(c) else np.vstack([pa_, pb]))
            length = float(np.hypot(*np.diff(line, axis=0).T).sum())
            if length <= MAX_DETOUR * straight + SLACK_M:
                good.append((length, line))
            else:
                rejected += 1
        if not good or all(len(line) <= 2 for _, line in good):
            continue
        good.sort(key=lambda x: x[0])
        line = good[len(good) // 2][1]
        keep = [0]
        for i in range(1, len(line) - 1):
            if np.hypot(*(line[i] - line[keep[-1]])) >= MIN_STEP_M:
                keep.append(i)
        keep.append(len(line) - 1)
        line = line[keep]
        lat, lon = geo.from_xy(line[:, 0], line[:, 1])
        rec["from_stop"].append(a)
        rec["to_stop"].append(b)
        rec["lat"].append(np.round(lat, 6).tolist())
        rec["lon"].append(np.round(lon, 6).tolist())
        rec["n_candidates"].append(len(good))
        rec["source"].append(src)
    C.MODEL.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.table(rec), C.MODEL / "segment_shapes.parquet")
    summary = {"days": days, "segments_seen": len(trav), "segments_with_shape": len(rec["from_stop"]),
               "from_gtfs_shapes": rec["source"].count("gtfs"), "from_gps": rec["source"].count("gps"),
               "traversals_rejected": rejected, "seconds": round(time.time() - t_start, 1)}
    (C.QC / "shapes.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    os.nice(5)
    main()
