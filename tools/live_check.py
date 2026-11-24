"""Check real-time predictions against what the buses then did, on today's live GTFS-RT data.

Rides are sampled from the live worker's stop events (data/work/live/clean/stop_events): a bus observed at two
stops at least 3 stops apart, boarding at least WARMUP minutes after the worker started (so the live window is
full). For each ride the prediction uses only what was known at boarding time: the historical model, and the live
blend from other buses' traversals in the window before boarding (plus the bus's own earlier segments), exactly as
the API would have answered then. The observed time comes from the same live feed, later.

  .venv/bin/python tools/live_check.py [--n 1500] [--warmup 25]
Writes reports/results/live_check.md and .json.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

import duckdb
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from transit import config as C  # noqa: E402
from transit.live import LiveReplay  # noqa: E402
from transit.planner import Network  # noqa: E402

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def metrics(pred: np.ndarray, obs: np.ndarray) -> dict:
    e = (pred - obs) / 60
    o = obs / 60
    return {"n": int(len(e)), "MAE_min": round(float(np.mean(np.abs(e))), 2),
            "MAPE_pct": round(float(np.mean(np.abs(e) / o)) * 100, 1), "P50_min": round(float(np.median(np.abs(e))), 2),
            "within_5min_pct": round(float(np.mean(np.abs(e) <= 5)) * 100, 1),
            "within_10pct_pct": round(float(np.mean(np.abs(e) <= 0.1 * o)) * 100, 1), "bias_min": round(float(np.mean(e)), 2)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=1500)
    ap.add_argument("--warmup", type=float, default=25, help="minutes after the first live position before boarding counts")
    ap.add_argument("--day", default=dt.datetime.now(IST).date().isoformat())
    args = ap.parse_args()
    f = C.LIVE_DIR / "clean/stop_events" / f"service_date={args.day}" / "part.parquet"
    raw = sorted((C.LIVE_DIR / "raw" / args.day).glob("*.parquet"))
    first_pos = float(duckdb.sql(f"SELECT min(fetched_utc) FROM read_parquet([{', '.join(repr(str(r)) for r in raw)}])").fetchall()[0][0])
    net = Network()
    g = net.g
    live = LiveReplay(net, [f])
    ev = duckdb.sql(f"""SELECT run_id, pattern_id, gtfs_trip_id, stop_pos, t_utc, observed
                        FROM read_parquet('{f}', hive_partitioning = false) ORDER BY run_id, stop_pos""").fetchnumpy()
    _, first = np.unique(ev["run_id"], return_index=True)
    first = np.sort(first)
    runs = list(zip(first, np.append(first[1:], len(ev["run_id"]))))
    trip_index = {t: i for i, t in enumerate(g.trip_ids)}
    d = C.daytype_of(args.day)
    mid = dt.datetime.fromisoformat(args.day).replace(tzinfo=dt.timezone.utc).timestamp() - C.IST_OFFSET_S
    t_min = first_pos + args.warmup * 60
    rng = np.random.default_rng(42)
    rows, tries = [], 0
    while len(rows) < args.n and tries < args.n * 40:
        tries += 1
        a, b = runs[rng.integers(len(runs))]
        oi = np.flatnonzero(ev["observed"][a:b]) + a
        oi = oi[ev["t_utc"][oi] >= t_min]
        if len(oi) < 2:
            continue
        i, j = np.sort(rng.choice(oi, 2, replace=False))
        pid, pi, pj = int(ev["pattern_id"][i]), int(ev["stop_pos"][i]), int(ev["stop_pos"][j])
        if pj - pi < 3:
            continue
        ti, tj = float(ev["t_utc"][i]), float(ev["t_utc"][j])
        if tj <= ti:
            continue
        trip = trip_index[str(ev["gtfs_trip_id"][i])]
        rows.append({"board_ist": dt.datetime.fromtimestamp(ti, IST).strftime("%H:%M"), "stops": pj - pi,
                     "observed_s": tj - ti, "model_s": net.ride_time(pid, pi, pj, ti - mid, d),
                     "live_s": live.ride_time(pid, pi, pj, ti, args.day, d, int(ev["run_id"][i])),
                     "timetable_s": float(g.trip_sched[trip][pj] - g.trip_sched[trip][pi])})
    if not rows:
        sys.exit("no rides yet: let the live worker run longer")
    obs = np.array([r["observed_s"] for r in rows])
    res = {"day": args.day, "rides": len(rows),
           "first_live_position_ist": dt.datetime.fromtimestamp(first_pos, IST).strftime("%H:%M"),
           "boarding_from_ist": dt.datetime.fromtimestamp(t_min, IST).strftime("%H:%M"),
           "boarding_to_ist": max(r["board_ist"] for r in rows),
           "median_stops": float(np.median([r["stops"] for r in rows])),
           "median_observed_min": round(float(np.median(obs)) / 60, 1),
           "methods": {m: metrics(np.array([r[f"{m}_s"] for r in rows]), obs) for m in ("live", "model", "timetable")}}
    # does the live advantage hold through the evening? split by boarding half-hour
    by = {}
    for r in rows:
        h, m = map(int, r["board_ist"].split(":"))
        by.setdefault(f"{h:02d}:{'00' if m < 30 else '30'}", []).append(r)
    res["by_half_hour"] = {k: {"n": len(v), "model_MAE": metrics(np.array([x["model_s"] for x in v]), np.array([x["observed_s"] for x in v]))["MAE_min"],
                               "live_MAE": metrics(np.array([x["live_s"] for x in v]), np.array([x["observed_s"] for x in v]))["MAE_min"]}
                           for k, v in sorted(by.items())}
    C.REPORTS.mkdir(parents=True, exist_ok=True)
    (C.REPORTS / "live_check.json").write_text(json.dumps(res, indent=2))
    names = {"live": "**Our model + live feed**", "model": "Our model, historical only", "timetable": "GTFS timetable (2026)"}
    lines = ["# Real-time check on the live GTFS-RT feed", "",
             f"{res['rides']:,} real bus rides on {args.day}, boarding {res['boarding_from_ist']}–{res['boarding_to_ist']} IST "
             f"(live feed from {res['first_live_position_ist']}); median {res['median_stops']:.0f} stops, "
             f"{res['median_observed_min']} min. Predictions use only data available at boarding.", "",
             "| Method | MAE (min) | MAPE | median error | within ±5 min | within ±10% | bias (min) |",
             "|---|---|---|---|---|---|---|"]
    for m, v in res["methods"].items():
        lines.append(f"| {names[m]} | {v['MAE_min']} | {v['MAPE_pct']}% | {v['P50_min']} | {v['within_5min_pct']}% | "
                     f"{v['within_10pct_pct']}% | {v['bias_min']} |")
    lines += ["", "| Boarding | rides | historical MAE | live MAE |", "|---|---|---|---|"]
    for k, v in res["by_half_hour"].items():
        lines.append(f"| {k} | {v['n']} | {v['model_MAE']} | {v['live_MAE']} |")
    (C.REPORTS / "live_check.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
