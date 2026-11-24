"""Stage 8: how well does the model predict actual bus ride times on held-out days?

Samples observed trip fragments (board stop -> alight stop on one real bus run) from the test days, using
only runs with >= FULL_TRIP_FRAC of their stops matched and both ends actually observed. For each fragment:

  observed   = actual time between passing the two stops (from GPS)
  model      = segment model for that day type, hour by hour from the boarding time
  timetable  = GTFS scheduled time between the same two stops on the matched trip
  flat speed = distance / city-wide median speed (all hours)

Metrics: MAE (min), MAPE, P50/P90 absolute error, share within +-5 min and within +-10%, by method, and
broken down by hour band and distance band for the model.

Also writes reports/gmaps_manual.csv: 15 of the fragments, with an empty column to fill with Google Maps
transit times looked up by hand for the same weekday and time.

Outputs: reports/validation.md, reports/validation_samples.csv, reports/gmaps_manual.csv
"""

from __future__ import annotations

import csv
import datetime as dt
import sys
from pathlib import Path

import duckdb
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from transit import config as C  # noqa: E402
from transit.planner import Network  # noqa: E402
from transit.live import LiveReplay  # noqa: E402

N_SAMPLES = C.VALIDATION_SAMPLES
SEED = 42
DIST_BANDS = [(0, 2000, "<2 km"), (2000, 5000, "2-5 km"), (5000, 15000, "5-15 km"), (15000, 1e9, "15+ km")]
HOUR_BANDS = [(0, 7, "night/early (0-7)"), (7, 11, "morning peak (7-11)"), (11, 16, "midday (11-16)"),
              (16, 21, "evening peak (16-21)"), (21, 24, "late (21-24)")]


def metrics(err_s: np.ndarray, obs_s: np.ndarray) -> dict:
    ae = np.abs(err_s) / 60
    return {"n": int(len(ae)), "MAE_min": round(float(ae.mean()), 2),
            "MAPE_pct": round(float(np.mean(np.abs(err_s) / obs_s) * 100), 1),
            "P50_min": round(float(np.median(ae)), 2), "P90_min": round(float(np.quantile(ae, 0.9)), 2),
            "within_5min_pct": round(float(np.mean(ae <= 5) * 100), 1),
            "within_10pct_pct": round(float(np.mean(np.abs(err_s) / obs_s <= 0.10) * 100), 1),
            "bias_min": round(float(np.mean(err_s) / 60), 2)}


def main() -> None:
    net = Network()
    g = net.g
    files = [C.STOP_EVENTS / f"service_date={d}" / "part.parquet" for d in C.TEST_DAYS]
    files = [f for f in files if f.exists()]
    if not files:
        sys.exit("no stop events for the test days")
    live = LiveReplay(net, files) if C.LIVE_REPLAY else None
    flist = ", ".join(f"'{f}'" for f in files)
    ev = duckdb.sql(f"""
        SELECT service_date, run_id, pattern_id, gtfs_trip_id, stop_pos, t_utc, observed
        FROM read_parquet([{flist}], hive_partitioning = false) WHERE frac_matched >= {C.FULL_TRIP_FRAC}
        ORDER BY service_date, run_id, stop_pos""").fetchnumpy()
    if len(ev["run_id"]) == 0:
        sys.exit("no complete runs on the test days")
    key = np.char.add(ev["service_date"].astype(str), ev["run_id"].astype(str))
    _, first = np.unique(key, return_index=True)
    first = np.sort(first)
    bounds = list(zip(first, np.append(first[1:], len(key))))
    trip_index = {t: i for i, t in enumerate(g.trip_ids)}

    rng = np.random.default_rng(SEED)
    rows = []
    tries = 0
    while len(rows) < N_SAMPLES and tries < N_SAMPLES * 20:
        tries += 1
        a, b = bounds[rng.integers(len(bounds))]
        obs_idx = np.flatnonzero(ev["observed"][a:b]) + a
        if len(obs_idx) < 4:
            continue
        i, j = np.sort(rng.choice(obs_idx, 2, replace=False))
        pid = int(ev["pattern_id"][i])
        pi, pj = int(ev["stop_pos"][i]), int(ev["stop_pos"][j])
        if pj - pi < 3:
            continue
        p = g.patterns[pid]
        dist = float(p.cumdist[pj] - p.cumdist[pi])
        day = str(ev["service_date"][i])
        d = C.daytype_of(day)
        t_i, t_j = float(ev["t_utc"][i]), float(ev["t_utc"][j])
        midnight = dt.datetime.fromisoformat(day).replace(tzinfo=dt.timezone.utc).timestamp() - C.IST_OFFSET_S
        t0 = t_i - midnight
        observed = t_j - t_i
        if observed <= 0:
            continue
        trip = trip_index[str(ev["gtfs_trip_id"][i])]
        rows.append({
            "service_date": day, "daytype": d, "hour": int(t0 // 3600) % 24, "route_id": p.route_id,
            "from_stop": g.stop_names[p.stops[pi]], "to_stop": g.stop_names[p.stops[pj]],
            "from_lat": float(g.stop_lat[p.stops[pi]]), "from_lon": float(g.stop_lon[p.stops[pi]]),
            "to_lat": float(g.stop_lat[p.stops[pj]]), "to_lon": float(g.stop_lon[p.stops[pj]]),
            "stops": pj - pi, "dist_m": round(dist), "depart_ist": (dt.datetime(2000, 1, 1) + dt.timedelta(seconds=t0 % 86400)).strftime("%H:%M"),
            "observed_s": observed, "model_s": net.ride_time(pid, pi, pj, t0, d),
            "plan_s": (m_s := net.ride_time(pid, pi, pj, t0, d)) + C.PLAN_BUFFER * (net.ride_time(pid, pi, pj, t0, d, "p85") - m_s),
            "timetable_s": float(g.trip_sched[trip][pj] - g.trip_sched[trip][pi]),
            "flat_s": dist / (float(np.median(net.city_kmh)) / 3.6),
            "live_s": live.ride_time(pid, pi, pj, t_i, day, d, int(ev["run_id"][i])) if live else float("nan")})

    if not rows:
        sys.exit("could not sample any fragments")
    obs = np.array([r["observed_s"] for r in rows])
    res = {m: metrics(np.array([r[f"{m}_s"] for r in rows]) - obs, obs) for m in (("model", "live") if C.LIVE_REPLAY else ("model",)) + ("timetable", "flat")}

    # "plan for" time (P85): share of rides that arrive within it, from history alone and scaled by the live blend
    plan = np.array([r["plan_s"] for r in rows])
    cover = {"target_pct": 85.0, "buffer_scale": C.PLAN_BUFFER, "historical_pct": round(float(np.mean(obs <= plan)) * 100, 1),
             "historical_mean_buffer_min": round(float(np.mean(plan - np.array([r["model_s"] for r in rows]))) / 60, 2)}
    if C.LIVE_REPLAY:
        lp = plan * np.array([r["live_s"] for r in rows]) / np.maximum(np.array([r["model_s"] for r in rows]), 1.0)
        cover["live_pct"] = round(float(np.mean(obs <= lp)) * 100, 1)

    def table(title: str, groups: list[tuple[str, np.ndarray]]) -> list[str]:
        out = [f"\n### {title}\n", "| Group | n | MAE (min) | MAPE | within ±5 min | within ±10% |", "|---|---|---|---|---|---|"]
        mod = np.array([r["model_s"] for r in rows])
        for name, mask in groups:
            if mask.sum() == 0:
                continue
            m = metrics(mod[mask] - obs[mask], obs[mask])
            out.append(f"| {name} | {m['n']} | {m['MAE_min']} | {m['MAPE_pct']}% | {m['within_5min_pct']}% | {m['within_10pct_pct']}% |")
        return out

    dist = np.array([r["dist_m"] for r in rows])
    hour = np.array([r["hour"] for r in rows])
    dty = np.array([r["daytype"] for r in rows])
    lines = ["# Validation report", "",
             f"Train days: {', '.join(C.TRAIN_DAYS)}. Test days (held out): {', '.join(C.TEST_DAYS)}.",
             f"{len(rows)} trip fragments sampled from complete test-day runs (seed {SEED}); each is a real bus ride",
             "between two observed stops, at least 3 stops apart.", "",
             "## Ride time error by method", "",
             "| Method | n | MAE (min) | MAPE | P50 (min) | P90 (min) | within ±5 min | within ±10% | bias (min) |",
             "|---|---|---|---|---|---|---|---|---|"]
    names = {"model": "**Our model** (segment × day type × hour)", "live": f"**Our model + live blend** (replayed: same-day data from the {C.LIVE_WINDOW_S // 60} min before boarding)", "timetable": "GTFS timetable", "flat": "Flat city-wide speed"}
    for m, r in res.items():
        lines.append(f"| {names[m]} | {r['n']} | {r['MAE_min']} | {r['MAPE_pct']}% | {r['P50_min']} | {r['P90_min']} | "
                     f"{r['within_5min_pct']}% | {r['within_10pct_pct']}% | {r['bias_min']} |")
    lines += table("Model error by distance", [(n, (dist >= a) & (dist < b)) for a, b, n in DIST_BANDS])
    lines += table("Model error by time of day", [(n, (hour >= a) & (hour < b)) for a, b, n in HOUR_BANDS])
    lines += table("Model error by day type", [("weekday", dty == 0), ("weekend", dty == 1)])
    lines += ["", "## \"Plan for\" time (P85)", "",
              f"Share of rides that arrived within the \"plan for\" time (target {cover['target_pct']:.0f}%): "
              f"**{cover['historical_pct']}%** from history"
              + (f", **{cover['live_pct']}%** with the live blend" if "live_pct" in cover else "")
              + f". The buffer over the estimate averages {cover['historical_mean_buffer_min']} min."]
    lines += ["", "Bias > 0 means the method predicts longer rides than observed.",
              "Google Maps comparison: `tools/gmaps_lookup.py` (Routes API) writes `gmaps_comparison.csv` and `gmaps_metrics.json`."]
    C.REPORTS.mkdir(parents=True, exist_ok=True)
    (C.REPORTS / "validation.md").write_text("\n".join(lines) + "\n")
    with open(C.REPORTS / "validation_samples.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    gm = C.REPORTS / "gmaps_manual.csv"
    filled = gm.exists() and any((row.get("gmaps_bus_min (fill in)") or "").strip() for row in csv.DictReader(open(gm)))
    pick = sorted(rows, key=lambda r: -r["dist_m"])[:: max(1, len(rows) // 15)][:15]
    with open(gm if not filled else C.REPORTS / "gmaps_manual.new.csv", "w", newline="") as fh:   # never overwrite lookups
        w = csv.writer(fh)
        w.writerow(["service_date", "weekday", "depart_ist", "from_stop", "from_lat", "from_lon", "to_stop", "to_lat",
                    "to_lon", "observed_min", "model_min", "timetable_min", "gmaps_bus_min (fill in)"])
        for r in pick:
            w.writerow([r["service_date"], dt.date.fromisoformat(r["service_date"]).strftime("%A"), r["depart_ist"],
                        r["from_stop"], r["from_lat"], r["from_lon"], r["to_stop"], r["to_lat"], r["to_lon"],
                        round(r["observed_s"] / 60, 1), round(r["model_s"] / 60, 1), round(r["timetable_s"] / 60, 1), ""])
    import json
    (C.REPORTS / "validation_metrics.json").write_text(json.dumps(
        {"methods": res, "plan_for": cover, "by_distance": {n: metrics(np.array([r["model_s"] for r in rows])[(dist >= a) & (dist < b)] - obs[(dist >= a) & (dist < b)], obs[(dist >= a) & (dist < b)]) for a, b, n in DIST_BANDS if ((dist >= a) & (dist < b)).any()},
         "by_hour": {n: metrics(np.array([r["model_s"] for r in rows])[(hour >= a) & (hour < b)] - obs[(hour >= a) & (hour < b)], obs[(hour >= a) & (hour < b)]) for a, b, n in HOUR_BANDS if ((hour >= a) & (hour < b)).any()},
         "test_days": C.TEST_DAYS, "train_days": C.TRAIN_DAYS}, indent=2))
    print("\n".join(lines))


if __name__ == "__main__":
    main()
