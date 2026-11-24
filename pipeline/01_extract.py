"""Stage 1: extract and clean the historical GTFS-RT vehicle positions.

Reads one or more daily SQLite files (bus_movements_YYYY_MM_DD.db) read-only,
applies the cleaning rules of the technical note in order, and writes:

  data/clean/pings/service_date=YYYY-MM-DD/part-<source day>.parquet
  data/clean/qc/<source day>.json      (rows in/out per rule, lag and dwell stats)

Notes on how the rules are applied
- Dedup keeps, for each (vehicle_id, ts_utc), the row that was saved first.
- A source file holds a few fixes from the neighbouring service day (the two
  clocks differ). Those rows land in the neighbouring service_date partition, so
  when several source days are processed the same fix can appear in two part
  files. Readers should dedup on (vehicle_id, ts_utc) across parts.
- The teleport filter walks each vehicle's points in time order and compares each
  point with the previous *kept* point. After 3 consecutive rejections the
  current point becomes the new anchor, so one bad point cannot wipe out the rest
  of a vehicle's day. The pass runs twice.

Usage
  .venv/bin/python pipeline/01_extract.py --day 2026-10-15
  .venv/bin/python pipeline/01_extract.py --all --threads 16
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from pathlib import Path

import duckdb
import numpy as np
from numba import njit

ROOT = Path(__file__).resolve().parent.parent
SRC_CANDIDATES = [
    ROOT / "data/raw/gtfs-rt-history/2026-10",          # layout in README
    ROOT / "data/raw/delhi-dtc-bus-gtfs-rt-2026-10",    # layout produced by extracting the .7z
]
OUT_PINGS = ROOT / "data/clean/pings"
OUT_QC = ROOT / "data/clean/qc"
TMP = ROOT / "data/tmp"

# ---- parameters ---------------------------------------
LAT_MIN, LAT_MAX = 28.35, 28.95
LON_MIN, LON_MAX = 76.80, 77.55
# Exact same coordinates reported by >= this many vehicles in a day. Coordinates are
# float32 (~2 m steps), so busy terminals naturally repeat exact positions across
# 10-15 buses; only a much larger count indicates a fallback/sentinel position.
SENTINEL_MIN_VEHICLES = 50
MAX_LAG_S = 30 * 60             # fix may be at most 30 min older than when it was saved
IST_OFFSET_S = 5 * 3600 + 30 * 60
TELEPORT_MAX_KMH = 90.0
TELEPORT_JUMP_M = 1000.0
TELEPORT_JUMP_DT_S = 10
TELEPORT_RESET_AFTER = 3
TELEPORT_PASSES = 2


def find_src() -> Path:
    for p in SRC_CANDIDATES:
        if p.is_dir() and any(p.glob("bus_movements_*.db")):
            return p
    sys.exit(f"No bus_movements_*.db found in any of: {', '.join(map(str, SRC_CANDIDATES))}")


@njit(cache=True)
def _haversine_m(lat1, lon1, lat2, lon2):
    r = 6371008.8
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


@njit(cache=True)
def _teleport_keep(veh, ts, lat, lon, max_kmh, jump_m, jump_dt, reset_after):
    """Rows must be sorted by (veh, ts). Returns a boolean keep mask."""
    n = veh.shape[0]
    keep = np.ones(n, dtype=np.bool_)
    anchor = -1
    rejected = 0
    for i in range(n):
        if i == 0 or veh[i] != veh[i - 1]:
            anchor = i
            rejected = 0
            continue
        dt = ts[i] - ts[anchor]
        d = _haversine_m(lat[anchor], lon[anchor], lat[i], lon[i])
        bad = (dt <= 0) or (d / dt * 3.6 > max_kmh) or (d > jump_m and dt <= jump_dt)
        if bad and rejected < reset_after:
            keep[i] = False
            rejected += 1
        else:
            anchor = i
            rejected = 0
    return keep


def count(con, table: str) -> int:
    return con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


def process_day(con: duckdb.DuckDBPyConnection, db_path: Path) -> dict:
    day = db_path.stem.removeprefix("bus_movements_").replace("_", "-")
    t0 = time.time()
    qc: dict = {"source_file": db_path.name, "source_day": day, "rules": []}
    steps = qc["rules"]

    def step(rule: str, table: str, before: int) -> int:
        after = count(con, table)
        steps.append({"rule": rule, "rows_in": before, "rows_out": after,
                      "dropped": before - after,
                      "dropped_pct": round(100 * (before - after) / before, 3) if before else 0.0})
        print(f"  {rule:<24} {before:>12,} -> {after:>12,}  (-{before - after:,})", flush=True)
        return after

    # 0. read (read-only). DuckDB's sqlite scanner opens the file without writing to it.
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE raw AS
        SELECT vehicle_id,
               TRY_CAST(timestamp AS BIGINT) AS ts_utc,
               TRY_CAST(lat AS DOUBLE)       AS lat,
               TRY_CAST(lng AS DOUBLE)       AS lon,
               speed                         AS speed_reported,
               TRY_CAST(route_id AS INTEGER) AS route_id,
               trip_id,
               CAST(date AS TIMESTAMP) + TRY_CAST(time AS INTERVAL) AS recv_ist
        FROM sqlite_scan('{db_path}', 'vehicle_feed')
    """)
    n = count(con, "raw")
    qc["rows_read"] = n
    qc["unparseable"] = con.execute("""
        SELECT count(*) FILTER (WHERE ts_utc IS NULL),
               count(*) FILTER (WHERE lat IS NULL OR lon IS NULL),
               count(*) FILTER (WHERE recv_ist IS NULL),
               count(*) FILTER (WHERE vehicle_id IS NULL)
        FROM raw""").fetchone()
    print(f"  {'read':<24} {n:>12,}", flush=True)

    # 1. exact dedup on (vehicle_id, ts_utc), keep the first saved row
    qc["dup_groups_with_different_position"] = con.execute("""
        SELECT count(*) FROM (
          SELECT vehicle_id, ts_utc FROM raw GROUP BY ALL
          HAVING count(*) > 1 AND (count(DISTINCT lat) > 1 OR count(DISTINCT lon) > 1))
    """).fetchone()[0]
    con.execute("""
        CREATE OR REPLACE TEMP TABLE t1 AS
        SELECT * EXCLUDE (rn) FROM (
          SELECT *, row_number() OVER (PARTITION BY vehicle_id, ts_utc ORDER BY recv_ist) AS rn
          FROM raw WHERE vehicle_id IS NOT NULL AND ts_utc IS NOT NULL)
        WHERE rn = 1
    """)
    con.execute("DROP TABLE raw")
    n = step("1 dedup", "t1", n)

    # 2. geofence
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE t2 AS SELECT * FROM t1
        WHERE lat BETWEEN {LAT_MIN} AND {LAT_MAX} AND lon BETWEEN {LON_MIN} AND {LON_MAX}
    """)
    con.execute("DROP TABLE t1")
    n = step("2 geofence", "t2", n)

    # 3. sentinel coordinates: exact positions reported by many different vehicles
    sentinels = con.execute(f"""
        SELECT lat, lon, count(DISTINCT vehicle_id) AS vehicles, count(*) AS rows
        FROM t2 GROUP BY lat, lon
        HAVING count(DISTINCT vehicle_id) >= {SENTINEL_MIN_VEHICLES}
        ORDER BY vehicles DESC
    """).fetchall()
    qc["sentinel_positions"] = [dict(lat=a, lon=b, vehicles=c, rows=d) for a, b, c, d in sentinels[:20]]
    qc["sentinel_position_count"] = len(sentinels)
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE t3 AS SELECT * FROM t2
        WHERE (lat, lon) NOT IN (
          SELECT (lat, lon) FROM t2 GROUP BY lat, lon
          HAVING count(DISTINCT vehicle_id) >= {SENTINEL_MIN_VEHICLES})
    """)
    con.execute("DROP TABLE t2")
    n = step("3 sentinel coords", "t3", n)

    # 4. time sanity: lag = saved time - fix time (both IST). Keep 0 <= lag <= 30 min.
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE t3l AS
        SELECT *, epoch(recv_ist)::BIGINT - (ts_utc + {IST_OFFSET_S}) AS lag_s FROM t3
    """)
    con.execute("DROP TABLE t3")
    lag = con.execute("""
        SELECT quantile_cont(lag_s, [0.001, 0.01, 0.05, 0.5, 0.95, 0.99, 0.999]),
               min(lag_s), max(lag_s),
               count(*) FILTER (WHERE lag_s < 0),
               count(*) FILTER (WHERE lag_s > 1800)
        FROM t3l""").fetchone()
    qc["lag_s"] = {"quantiles_p0.1_p1_p5_p50_p95_p99_p99.9": lag[0], "min": lag[1], "max": lag[2],
                   "negative": lag[3], "over_30min": lag[4]}
    con.execute(f"CREATE OR REPLACE TEMP TABLE t4 AS SELECT * FROM t3l WHERE lag_s BETWEEN 0 AND {MAX_LAG_S}")
    con.execute("DROP TABLE t3l")
    n = step("4 time sanity", "t4", n)

    # 5. day re-bucketing from the GPS clock
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE t5 AS
        SELECT *, make_timestamp((ts_utc + {IST_OFFSET_S}) * 1000000) AS ts_ist FROM t4
    """)
    con.execute("DROP TABLE t4")
    qc["service_dates"] = {str(d): c for d, c in con.execute(
        "SELECT CAST(ts_ist AS DATE) d, count(*) FROM t5 GROUP BY d ORDER BY d").fetchall()}

    # 6. teleport filter (sequential per vehicle, numba)
    arr = con.execute("""
        SELECT dense_rank() OVER (ORDER BY vehicle_id) AS vk, ts_utc, lat, lon
        FROM t5 ORDER BY vehicle_id, ts_utc
    """).fetchnumpy()
    veh = arr["vk"].astype(np.int64)
    ts = arr["ts_utc"].astype(np.int64)
    la = arr["lat"].astype(np.float64)
    lo = arr["lon"].astype(np.float64)
    keep = np.ones(len(veh), dtype=np.bool_)
    for p in range(TELEPORT_PASSES):
        idx = np.flatnonzero(keep)
        k = _teleport_keep(veh[idx], ts[idx], la[idx], lo[idx], TELEPORT_MAX_KMH,
                           TELEPORT_JUMP_M, TELEPORT_JUMP_DT_S, TELEPORT_RESET_AFTER)
        keep[idx[~k]] = False
        qc[f"teleport_pass{p + 1}_dropped"] = int((~k).sum())
    con.register("tp_keep", {"vk": veh, "ts_utc": ts, "keep": keep})
    con.execute("""
        CREATE OR REPLACE TEMP TABLE t6 AS
        SELECT t.* FROM (SELECT *, dense_rank() OVER (ORDER BY vehicle_id) AS vk FROM t5) t
        JOIN tp_keep k USING (vk, ts_utc) WHERE k.keep
    """)
    con.unregister("tp_keep")
    del arr, veh, ts, la, lo, keep
    con.execute("DROP TABLE t5")
    n = step("6 teleport", "t6", n)

    # 7. stationary compression: consecutive identical positions -> one dwell record
    con.execute("""
        CREATE OR REPLACE TEMP TABLE t7 AS
        WITH flagged AS (
          SELECT *, CASE WHEN lat = lag(lat) OVER w AND lon = lag(lon) OVER w THEN 0 ELSE 1 END AS new_run
          FROM t6 WINDOW w AS (PARTITION BY vehicle_id ORDER BY ts_utc)
        ), runs AS (
          SELECT *, sum(new_run) OVER (PARTITION BY vehicle_id ORDER BY ts_utc) AS run_id FROM flagged
        )
        SELECT vehicle_id,
               min(ts_utc)                        AS ts_utc,
               max(ts_utc)                        AS end_ts_utc,
               count(*)                           AS n_pings,
               any_value(lat)                     AS lat,
               any_value(lon)                     AS lon,
               arg_min(speed_reported, ts_utc)    AS speed_reported,
               arg_min(route_id, ts_utc)          AS route_id,
               arg_min(trip_id, ts_utc)           AS trip_id,
               arg_min(lag_s, ts_utc)             AS lag_s
        FROM runs GROUP BY vehicle_id, run_id
    """)
    con.execute("DROP TABLE t6")
    n = step("7 stationary compress", "t7", n)
    qc["dwell_records"] = con.execute("SELECT count(*) FROM t7 WHERE n_pings > 1").fetchone()[0]

    # 8. derived columns
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE t8 AS
        WITH p AS (
          SELECT *,
                 lag(end_ts_utc) OVER w AS prev_ts, lag(lat) OVER w AS plat, lag(lon) OVER w AS plon
          FROM t7 WINDOW w AS (PARTITION BY vehicle_id ORDER BY ts_utc)
        )
        SELECT vehicle_id, ts_utc, end_ts_utc, n_pings,
               make_timestamp((ts_utc + {IST_OFFSET_S}) * 1000000) AS ts_ist,
               CAST(make_timestamp((ts_utc + {IST_OFFSET_S}) * 1000000) AS DATE) AS service_date,
               hour(make_timestamp((ts_utc + {IST_OFFSET_S}) * 1000000))::TINYINT AS hour,
               isodow(make_timestamp((ts_utc + {IST_OFFSET_S}) * 1000000))::TINYINT AS dow,
               lat, lon, route_id, trip_id, speed_reported, lag_s,
               (ts_utc - prev_ts)::INTEGER AS dt_prev,
               CASE WHEN plat IS NULL THEN NULL ELSE
                 2 * 6371008.8 * asin(sqrt(
                   pow(sin(radians(lat - plat) / 2), 2) +
                   cos(radians(plat)) * cos(radians(lat)) * pow(sin(radians(lon - plon) / 2), 2)))
               END AS dist_prev_m,
               CASE WHEN plat IS NULL THEN NULL ELSE
                 (degrees(atan2(
                   sin(radians(lon - plon)) * cos(radians(lat)),
                   cos(radians(plat)) * sin(radians(lat)) -
                   sin(radians(plat)) * cos(radians(lat)) * cos(radians(lon - plon)))) + 360) % 360
               END AS bearing
        FROM p
    """)
    con.execute("DROP TABLE t7")
    con.execute("""
        ALTER TABLE t8 ADD COLUMN speed_derived_kmh DOUBLE;
        UPDATE t8 SET speed_derived_kmh = CASE WHEN dt_prev > 0 THEN dist_prev_m / dt_prev * 3.6 END;
    """)
    qc["rows_out"] = count(con, "t8")
    qc["vehicles"], qc["routes"], qc["trip_ids"] = con.execute(
        "SELECT count(DISTINCT vehicle_id), count(DISTINCT route_id), count(DISTINCT trip_id) FROM t8").fetchone()
    qc["report_interval_s"] = dict(zip(
        ["p10", "p50", "p90", "p99"],
        con.execute("SELECT quantile_cont(dt_prev, [0.1, 0.5, 0.9, 0.99]) FROM t8").fetchone()[0]))
    qc["speed_derived_kmh_p50_p99"] = con.execute(
        "SELECT quantile_cont(speed_derived_kmh, [0.5, 0.99]) FROM t8").fetchone()[0]

    # write, partitioned by service_date, sorted by vehicle and time
    OUT_PINGS.mkdir(parents=True, exist_ok=True)
    tag = db_path.stem.removeprefix("bus_movements_")
    con.execute(f"""
        COPY (SELECT * FROM t8 ORDER BY vehicle_id, ts_utc)
        TO '{OUT_PINGS}' (FORMAT parquet, COMPRESSION zstd, PARTITION_BY (service_date),
                          OVERWRITE_OR_IGNORE, FILENAME_PATTERN 'part-{tag}-{{i}}')
    """)
    con.execute("DROP TABLE t8")
    qc["seconds"] = round(time.time() - t0, 1)
    OUT_QC.mkdir(parents=True, exist_ok=True)
    (OUT_QC / f"{tag}.json").write_text(json.dumps(qc, indent=2, default=str))
    return qc


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--day", action="append", help="source day YYYY-MM-DD (repeatable)")
    g.add_argument("--all", action="store_true", help="process every bus_movements_*.db")
    ap.add_argument("--src", type=Path, help="folder with the .db files (auto-detected by default)")
    ap.add_argument("--threads", type=int, default=8)
    ap.add_argument("--memory", default="32GB", help="DuckDB memory limit")
    ap.add_argument("--force", action="store_true", help="reprocess days whose QC file already exists")
    args = ap.parse_args()

    src = args.src or find_src()
    files = sorted(src.glob("bus_movements_*.db")) if args.all else [
        src / f"bus_movements_{d.replace('-', '_')}.db" for d in args.day]
    for f in files:
        if not f.exists():
            sys.exit(f"missing {f}")

    TMP.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute(f"SET threads={args.threads}; SET memory_limit='{args.memory}'; "
                f"SET TimeZone='UTC'; SET temp_directory='{TMP}'; SET preserve_insertion_order=false; "
                "SET enable_progress_bar=false; INSTALL sqlite; LOAD sqlite;")
    for f in files:
        tag = f.stem.removeprefix("bus_movements_")
        if (OUT_QC / f"{tag}.json").exists() and not args.force:
            print(f"{f.name}: already done (QC file exists), skipping", flush=True)
            continue
        print(f"{f.name}", flush=True)
        qc = process_day(con, f)
        print(f"  done in {qc['seconds']}s, {qc['rows_out']:,} records written\n", flush=True)


if __name__ == "__main__":
    os.nice(5)
    main()
