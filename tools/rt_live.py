"""Live worker. Polls GTFS-RT VehiclePositions, keeps a rolling window of positions, and every
REBUILD_S turns it into live stop events with the same stage 1 cleaning rules and stage 2 trip matching as the
archive. The API (same data root) then uses these events for its live blend on today's date.

Real feed (needs the API key in ~/.otd_key; URL and x-api-key header as in transit/rt.py):
  nice -n 5 .venv/bin/python tools/rt_live.py
Simulation (no key needed): replays the October archive for SIM_DATE shifted to today, through the same
protobuf encode/decode path, after back-filling the last BACKFILL minutes:
  .venv/bin/python tools/rt_live.py --simulate 2026-10-28 --backfill 120

Writes under $DTC_DATA/live/: raw/<date>/<hour>.parquet (decoded positions), clean/pings, clean/stop_events,
status.json (last poll, vehicles, runs matched, field fill counts).
"""

from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import json
import os
import signal
import sys
import time
from pathlib import Path

import duckdb
import numpy as np
import polars as pl

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from transit import config as C  # noqa: E402
from transit import rt  # noqa: E402
from transit.gtfs import GTFS  # noqa: E402

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
LIVE = C.LIVE_DIR


class Simulator:
    """Serves the archived pings of `day`, shifted by whole days to look current, as GTFS-RT snapshots."""

    def __init__(self, day: str, start_utc: float, backfill_s: float, horizon_s: float = 6 * 3600):
        today = dt.datetime.fromtimestamp(start_utc, IST).date()
        self.shift = (today - dt.date.fromisoformat(day)).days * 86400
        lo, hi = start_utc - backfill_s - self.shift, start_utc + horizon_s - self.shift
        src = ROOT / f"data/clean/pings/service_date={day}"
        a = duckdb.sql(f"""
            SELECT vehicle_id, trip_id, route_id, ts, lat, lon FROM (
              SELECT vehicle_id, trip_id, route_id, ts_utc ts, lat, lon FROM read_parquet('{src}/*.parquet')
              UNION ALL
              SELECT vehicle_id, trip_id, route_id, end_ts_utc ts, lat, lon FROM read_parquet('{src}/*.parquet') WHERE end_ts_utc > ts_utc)
            WHERE ts > {lo} AND ts <= {hi} ORDER BY ts""").pl()
        self.df = a.with_columns((pl.col("ts").cast(pl.Int64) + self.shift).alias("ts_utc"),
                                 pl.col("route_id").cast(pl.Utf8), pl.col("vehicle_id").cast(pl.Utf8),
                                 pl.col("trip_id").cast(pl.Utf8)).select(rt.FIELDS)
        self.t = self.df["ts_utc"].to_numpy()
        self.last = start_utc - backfill_s

    def snapshot(self, now: float) -> bytes:
        lo, hi = np.searchsorted(self.t, self.last, "right"), np.searchsorted(self.t, now, "right")
        self.last = now
        return rt.encode(self.df.slice(lo, hi - lo), int(now))


def load_stage2():
    spec = importlib.util.spec_from_file_location("stage2", ROOT / "pipeline/02_stop_events.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.G = GTFS(C.GTFS_DIR)
    return mod


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--simulate", metavar="DAY", help="replay this archived day instead of the real feed")
    ap.add_argument("--backfill", type=float, default=120, help="simulation: minutes of history to load at start")
    ap.add_argument("--poll", type=float, default=15, help="seconds between feed fetches")
    ap.add_argument("--rebuild", type=float, default=120, help="seconds between stop-event rebuilds")
    ap.add_argument("--window", type=float, default=3, help="hours of positions kept for matching")
    ap.add_argument("--once", action="store_true", help="one fetch and one rebuild, then exit")
    ap.add_argument("--min-frac", type=float, default=0.0, help="live: minimum share of the route's stops matched")
    ap.add_argument("--min-stops", type=int, default=4, help="live: minimum stops matched per run")
    ap.add_argument("--max-minutes", type=float, default=0, help="stop after this long (0: run until killed)")
    args = ap.parse_args()

    # live outputs go to their own folders under the live root
    C.PINGS, C.STOP_EVENTS, C.QC = LIVE / "clean/pings", LIVE / "clean/stop_events", LIVE / "clean/qc"
    for p in (C.PINGS, C.STOP_EVENTS, C.QC, LIVE / "raw"):
        p.mkdir(parents=True, exist_ok=True)
    # Live runs are partial (a bus seen for minutes, not a whole trip) and the feed gives the route directly, so
    # the archive's "matched >= 30% of the route" rule would hold back nearly every run for 20-30 min.
    C.MIN_MATCHED_FRAC = args.min_frac
    C.MIN_MATCHED_STOPS = args.min_stops
    stage2 = load_stage2()
    t_start = time.time()
    sim = Simulator(args.simulate, t_start, args.backfill * 60) if args.simulate else None
    url, headers = (None, None) if sim else rt.feed_request()
    buf = pl.DataFrame(schema=rt.SCHEMA)
    if not sim:      # restart: reload the decoded snapshots of the last window
        files = sorted((LIVE / "raw").glob("*/*.parquet"))
        recent = [f for f in files if f.stat().st_mtime > t_start - args.window * 3600]
        if recent:
            buf = pl.concat([pl.read_parquet(f).select(rt.FIELDS) for f in recent])
            print(f"reloaded {buf.height:,} positions from {len(recent)} saved snapshots", flush=True)
    raw_pending: list[pl.DataFrame] = []
    last_build, polls, errors, fill = 0.0, 0, 0, {}
    stop = False
    signal.signal(signal.SIGTERM, lambda *_: globals().update(_stop=True))

    while True:
        now = time.time()
        try:
            blob = sim.snapshot(now) if sim else rt.fetch(url, headers)
            df, fill = rt.decode(blob)
            polls += 1
            raw_pending.append(df.with_columns(pl.lit(int(now)).alias("fetched_utc")))
            buf = pl.concat([buf, df])
        except Exception as e:  # network errors: log without the key, keep going
            errors += 1
            print(f"{dt.datetime.now(IST):%H:%M:%S} fetch failed: {rt.redact(str(e))[:200]}", flush=True)
        if now - last_build >= args.rebuild or args.once:
            last_build = now
            t0 = time.time()
            buf = buf.filter(pl.col("ts_utc") > now - args.window * 3600 - 600)
            if raw_pending:
                raw = pl.concat(raw_pending)
                raw_pending = []
                hour = dt.datetime.fromtimestamp(now, IST)
                f = LIVE / "raw" / f"{hour:%Y-%m-%d}" / f"{hour:%H}_{int(now)}.parquet"
                f.parent.mkdir(parents=True, exist_ok=True)
                raw.write_parquet(f)
            day = dt.datetime.fromtimestamp(now, IST).date().isoformat()
            pings = rt.clean_window(buf, now, args.window * 3600)
            out = C.PINGS / f"service_date={day}"
            out.mkdir(parents=True, exist_ok=True)
            pings.write_parquet(out / "part.parquet.tmp")
            os.replace(out / "part.parquet.tmp", out / "part.parquet")
            ev_dir = C.STOP_EVENTS / f"service_date={day}"
            (C.QC / f"stop_events_{day}.json").unlink(missing_ok=True)
            tmp_ev = ev_dir / "part.parquet"
            keep = tmp_ev.with_suffix(".prev")
            if tmp_ev.exists():
                os.replace(tmp_ev, keep)          # the API keeps reading the previous version until the new one is ready
            qc = stage2.process_day(day)
            keep.unlink(missing_ok=True)
            status = {"updated_ist": dt.datetime.fromtimestamp(now, IST).isoformat(timespec="seconds"),
                      "source": f"simulation of {args.simulate}" if sim else "GTFS-RT feed",
                      "polls": polls, "fetch_errors": errors, "vehicles_in_window": int(pings["vehicle_id"].n_unique()),
                      "pings_in_window": pings.height,
                      "latest_position_age_s": round(now - float(buf["ts_utc"].max()), 1) if buf.height else None,
                      "runs_kept": qc.get("kept"), "stop_events": qc.get("events"),
                      "rebuild_seconds": round(time.time() - t0, 1), "last_feed_fields": fill}
            (LIVE / "status.json").write_text(json.dumps(status, indent=2))
            print(json.dumps({k: status[k] for k in ("updated_ist", "vehicles_in_window", "runs_kept", "stop_events",
                                                     "latest_position_age_s", "rebuild_seconds")}), flush=True)
        if args.once or globals().get("_stop") or stop or (args.max_minutes and now - t_start > args.max_minutes * 60):
            break
        time.sleep(max(0.0, args.poll - (time.time() - now)))


if __name__ == "__main__":
    os.nice(5)
    main()
