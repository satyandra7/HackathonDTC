"""Stage 4: segment travel-time model.

A segment is a pair of consecutive stops (from_stop, to_stop), shared across routes. An observation is
the time between the bus passing the two stops, from the train days only, with both stops actually
observed (not interpolated). Dwell is not separated: segment time = departure-to-departure.

For each (segment, day type, hour): median and P85 after trimming values beyond 3 x IQR. Sparse cells
are shrunk towards a parent estimate:

    T = (n * T_cell + k * T_parent) / (n + k),   confidence = n / (n + k)

Parent chain: same segment +-1 hour -> same segment and day type, all hours -> same segment, all days.
Segments never observed are timed at run time from distance and the city-wide median speed for that
day type and hour (city_speed.parquet).

Outputs (data/model/): segment_profiles.parquet, city_speed.parquet, model_summary.json
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import duckdb
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from transit import config as C  # noqa: E402
from transit.gtfs import GTFS  # noqa: E402


def main() -> None:
    t0 = time.time()
    g = GTFS(C.GTFS_DIR)
    files = [C.STOP_EVENTS / f"service_date={d}" / "part.parquet" for d in C.TRAIN_DAYS]
    missing = [str(f) for f in files if not f.exists()]
    if missing:
        sys.exit(f"stop events missing for train days: {missing}")
    D = C.N_DAYTYPES
    dtype_case = "CASE " + " ".join(f"WHEN e.service_date = '{d}' THEN {C.daytype_of(d)}" for d in C.TRAIN_DAYS) + " END"

    # segment lengths along each pattern (metres), keyed by stop pair
    seg_len: dict[tuple[str, str], float] = {}
    for p in g.patterns:
        for k in range(len(p.stops) - 1):
            seg_len.setdefault((g.stop_ids[p.stops[k]], g.stop_ids[p.stops[k + 1]]),
                               float(p.cumdist[k + 1] - p.cumdist[k]))
    con = duckdb.connect()
    con.execute("SET threads=8")
    con.register("seglen", pa.table({"from_stop": [a for a, _ in seg_len], "to_stop": [b for _, b in seg_len],
                                     "len_m": list(seg_len.values())}))
    flist = ", ".join(f"'{f}'" for f in files)
    con.execute(f"""
        CREATE TEMP TABLE obs AS
        WITH e AS (
          SELECT *, lead(stop_id) OVER w AS next_stop, lead(t_utc) OVER w AS next_t,
                 lead(observed) OVER w AS next_obs, lead(stop_pos) OVER w AS next_pos
          FROM read_parquet([{flist}], hive_partitioning = false)
          WINDOW w AS (PARTITION BY service_date, run_id ORDER BY stop_pos))
        SELECT e.stop_id AS from_stop, e.next_stop AS to_stop,
               {dtype_case} AS daytype,
               CAST(floor(((e.t_utc + {C.IST_OFFSET_S}) % 86400) / 3600) AS INTEGER) AS hour,
               e.next_t - e.t_utc AS dt_s, s.len_m, CAST(e.service_date AS VARCHAR) AS sd
        FROM e JOIN seglen s ON s.from_stop = e.stop_id AND s.to_stop = e.next_stop
        WHERE e.observed AND e.next_obs AND e.next_pos = e.stop_pos + 1 AND e.next_t > e.t_utc
          AND s.len_m / (e.next_t - e.t_utc) * 3.6 BETWEEN {C.SEG_MIN_KMH} AND {C.SEG_MAX_KMH}""")
    if C.RECENT_DAYS > 0:      # count the most recent training days twice (recency weighting)
        recent = ", ".join(f"'{d}'" for d in sorted(C.TRAIN_DAYS)[-C.RECENT_DAYS:])
        con.execute(f"INSERT INTO obs SELECT * FROM obs WHERE sd IN ({recent})")
    n_obs = con.execute("SELECT count(*) FROM obs").fetchone()[0]
    if n_obs == 0:
        sys.exit("no usable segment observations")

    # trimmed stats per cell
    cell = con.execute(f"""
        WITH q AS (SELECT from_stop, to_stop, daytype, hour,
                          quantile_cont(dt_s, 0.25) q1, quantile_cont(dt_s, 0.75) q3
                   FROM obs GROUP BY ALL),
        t AS (SELECT o.* FROM obs o JOIN q USING (from_stop, to_stop, daytype, hour)
              WHERE o.dt_s BETWEEN q.q1 - 3 * (q.q3 - q.q1) AND q.q3 + 3 * (q.q3 - q.q1))
        SELECT from_stop, to_stop, daytype, hour, count(*) n, (1 - {C.EST_MEAN_W}) * quantile_cont(dt_s, {C.EST_QUANTILE}) + {C.EST_MEAN_W} * avg(dt_s) med, quantile_cont(dt_s, 0.85) p85
        FROM t GROUP BY ALL""").fetchnumpy()
    city = con.execute("""
        SELECT daytype, hour, median(len_m / dt_s * 3.6) kmh, count(*) n FROM obs GROUP BY ALL""").fetchnumpy()

    # dense arrays [segment, daytype, hour]
    keys = sorted(set(zip(cell["from_stop"], cell["to_stop"])))
    sidx = {k: i for i, k in enumerate(keys)}
    S = len(keys)
    n = np.zeros((S, D, 24))
    med = np.full((S, D, 24), np.nan)
    p85 = np.full((S, D, 24), np.nan)
    for f, t, d, h, c, m, p in zip(cell["from_stop"], cell["to_stop"], cell["daytype"], cell["hour"],
                                   cell["n"], cell["med"], cell["p85"]):
        i = sidx[(f, t)]
        n[i, d, h], med[i, d, h], p85[i, d, h] = c, m, p

    def pooled(w, v):
        """n-weighted mean of medians along the given axes (ignoring empty cells)."""
        num = np.nansum(np.where(w > 0, w * np.nan_to_num(v), 0), axis=-1)
        den = w.sum(axis=-1)
        return np.where(den > 0, num / np.where(den > 0, den, 1), np.nan), den

    # parent 1: +-1 hour, same day type
    nb_w = np.stack([np.roll(n, s, axis=2) for s in (-1, 0, 1)], axis=-1)
    nb_v = np.stack([np.roll(med, s, axis=2) for s in (-1, 0, 1)], axis=-1)
    nb_p = np.stack([np.roll(p85, s, axis=2) for s in (-1, 0, 1)], axis=-1)
    par1, par1_n = pooled(nb_w, nb_v)
    par1_p85, _ = pooled(nb_w, nb_p)
    # parent 2: same day type, all hours;  parent 3: all days and hours
    par2, par2_n = pooled(n, med)
    par2_p85, _ = pooled(n, p85)
    par3, _ = pooled(n.reshape(S, -1), med.reshape(S, -1))
    par3_p85, _ = pooled(n.reshape(S, -1), p85.reshape(S, -1))
    par2 = np.broadcast_to(par2[:, :, None], (S, D, 24))
    par2_p85 = np.broadcast_to(par2_p85[:, :, None], (S, D, 24))
    par3 = np.broadcast_to(par3[:, None, None], (S, D, 24))
    par3_p85 = np.broadcast_to(par3_p85[:, None, None], (S, D, 24))
    par2_n = np.broadcast_to(par2_n[:, :, None], (S, D, 24))

    use1 = (par1_n >= 3)
    use2 = ~use1 & (par2_n >= 3)
    parent = np.where(use1, par1, np.where(use2, par2, par3))
    parent_p85 = np.where(use1, par1_p85, np.where(use2, par2_p85, par3_p85))
    level = np.where(n > 0, 0, np.where(use1, 1, np.where(use2, 2, 3))).astype(np.int8)
    k = C.SHRINK_K
    est = np.where(n > 0, (n * np.nan_to_num(med) + k * parent) / (n + k), parent)
    ratio = np.where(n > 0, np.nan_to_num(p85) / np.where(med > 0, med, 1), parent_p85 / np.where(parent > 0, parent, 1))
    est_p85 = est * np.maximum(ratio, 1.0)
    conf = n / (n + k)

    # optional recent-trend factor: how much slower the last TREND_DAYS training days ran than the model says
    trend = 1.0
    if C.TREND_DAYS > 0:
        recent = sorted(C.TRAIN_DAYS)[-C.TREND_DAYS:]
        rc = "CASE " + " ".join(f"WHEN e.service_date = '{d}' THEN 1" for d in recent) + " ELSE 0 END"
        ro = con.execute(f"""
            WITH e AS (
              SELECT *, lead(stop_id) OVER w AS next_stop, lead(t_utc) OVER w AS next_t,
                     lead(observed) OVER w AS next_obs, lead(stop_pos) OVER w AS next_pos
              FROM read_parquet([{flist}], hive_partitioning = false)
              WINDOW w AS (PARTITION BY service_date, run_id ORDER BY stop_pos))
            SELECT e.stop_id, e.next_stop, {dtype_case} AS d,
                   CAST(floor(((e.t_utc + {C.IST_OFFSET_S}) % 86400) / 3600) AS INTEGER) AS h, e.next_t - e.t_utc AS dt
            FROM e WHERE {rc} = 1 AND e.observed AND e.next_obs AND e.next_pos = e.stop_pos + 1 AND e.next_t > e.t_utc
            USING SAMPLE 300000""").fetchall()
        rat = [dt_ / est[sidx[(a, b)], d_, h_] for a, b, d_, h_, dt_ in ro if (a, b) in sidx and est[sidx[(a, b)], d_, h_] > 0]
        if rat:
            trend = float(np.median(rat))
            est = est * trend
            est_p85 = est_p85 * trend

    C.MODEL.mkdir(parents=True, exist_ok=True)
    fs, ts_ = zip(*keys)
    ii, dd, hh = np.meshgrid(np.arange(S), np.arange(D), np.arange(24), indexing="ij")
    pq.write_table(pa.table({
        "from_stop": np.array(fs, dtype=object)[ii.ravel()], "to_stop": np.array(ts_, dtype=object)[ii.ravel()],
        "daytype": dd.ravel().astype(np.int8), "hour": hh.ravel().astype(np.int8),
        "est_s": est.ravel().astype(np.float32), "p85_s": est_p85.ravel().astype(np.float32),
        "n": n.ravel().astype(np.int32), "confidence": conf.ravel().astype(np.float32),
        "level": level.ravel()}), C.MODEL / "segment_profiles.parquet", compression="zstd")

    cs = np.full((D, 24), np.nan)
    for d, h, v in zip(city["daytype"], city["hour"], city["kmh"]):
        cs[d, h] = v
    allday = np.nanmedian(cs, axis=1, keepdims=True)
    cs = np.where(np.isnan(cs), np.where(np.isnan(allday), C.DEFAULT_KMH, allday), cs)
    pq.write_table(pa.table({"daytype": np.repeat(np.arange(D), 24).astype(np.int8),
                             "hour": np.tile(np.arange(24), D).astype(np.int8),
                             "kmh": cs.ravel().astype(np.float32)}), C.MODEL / "city_speed.parquet")

    # sanity check: is peak slower than off-peak on well-observed segments?
    busy = (n[:, 0, 9] >= 5) & (n[:, 0, 13] >= 5)
    summary = {
        "train_days": C.TRAIN_DAYS, "observations": int(n_obs), "segments_observed": S,
        "segments_in_gtfs": len(seg_len), "coverage": round(S / max(1, len(seg_len)), 3),
        "cells_with_data": int((n > 0).sum()),
        "city_kmh_weekday": [round(float(v), 1) for v in cs[0]],
        "city_kmh_weekend": [round(float(v), 1) for v in cs[1]],
        "busy_segments_09_vs_13": int(busy.sum()),
        "median_ratio_09_over_13": round(float(np.median(med[busy, 0, 9] / med[busy, 0, 13])), 3) if busy.any() else None,
        "trend_factor": round(trend, 4), "seconds": round(time.time() - t0, 1),
    }
    (C.MODEL / "model_summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
