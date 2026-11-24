"""Google Maps comparison (validation, brief §"Judged on"): ask the Google Maps Routes API for a bus-only transit
route for real bus rides from the test days, and compare its bus ride time with the observed ride and with ours.

For each sampled ride (reports/validation_samples.csv: two stops, weekday, departure time), the request is a
TRANSIT route restricted to BUS, leaving on the next same weekday at the same time (Google only plans for the
future). Google's *bus ride time* is the in-vehicle time of its transit steps (stop departure -> arrival), so it is
comparable to our ride times, which exclude walking and waiting. Its door-to-door time is kept too.

The API key is read from ~/.gmaps_key (or $GMAPS_KEY) and never written anywhere. Responses are cached in
data/gmaps/raw.jsonl, so reruns do not repeat requests.

Usage: .venv/bin/python tools/gmaps_lookup.py [--n 200] [--seed 7]
Outputs: reports/gmaps_comparison.csv, reports/gmaps_metrics.json, and the 15 rows of reports/gmaps_manual.csv
filled in.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from transit import config as C  # noqa: E402

URL = "https://routes.googleapis.com/directions/v2:computeRoutes"
FIELDS = ",".join([
    "routes.duration", "routes.legs.steps.travelMode", "routes.legs.steps.staticDuration",
    "routes.legs.steps.transitDetails.stopDetails", "routes.legs.steps.transitDetails.transitLine.nameShort",
    "routes.legs.steps.transitDetails.transitLine.vehicle.type", "routes.legs.steps.transitDetails.stopCount"])
CACHE = C.DATA / "gmaps" / "raw.jsonl"
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def key() -> str:
    k = os.environ.get("GMAPS_KEY") or (Path.home() / ".gmaps_key").read_text()
    return k.strip()


def next_departure(weekday: int, hhmm: str, today: dt.date) -> str:
    """Next date after `today` with this ISO weekday, at hh:mm IST, as RFC 3339."""
    d = today + dt.timedelta(days=1)
    while d.isoweekday() != weekday:
        d += dt.timedelta(days=1)
    h, m = map(int, hhmm.split(":"))
    return dt.datetime(d.year, d.month, d.day, h, m, tzinfo=IST).isoformat()


def ask(k: str, row: dict, depart: str) -> dict:
    body = {"origin": {"location": {"latLng": {"latitude": float(row["from_lat"]), "longitude": float(row["from_lon"])}}},
            "destination": {"location": {"latLng": {"latitude": float(row["to_lat"]), "longitude": float(row["to_lon"])}}},
            "travelMode": "TRANSIT", "departureTime": depart, "transitPreferences": {"allowedTravelModes": ["BUS"]}}
    req = urllib.request.Request(URL, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "X-Goog-Api-Key": k, "X-Goog-FieldMask": FIELDS})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            msg = e.read().decode(errors="replace")[:300]
            if e.code in (429, 500, 503) and attempt < 2:
                time.sleep(2 * (attempt + 1))
                continue
            return {"error": f"HTTP {e.code}: {msg}"}
        except urllib.error.URLError as e:
            if attempt < 2:
                time.sleep(2)
                continue
            return {"error": str(e)}
    return {"error": "no response"}


def parse(resp: dict) -> dict:
    """Bus ride (in-vehicle) minutes, door-to-door minutes, bus legs and lines of Google's first route."""
    routes = resp.get("routes") or []
    if not routes:
        return {"g_ok": False}
    r = routes[0]
    ride, lines, kinds = 0.0, [], set()
    for st in r["legs"][0].get("steps", []):
        if st.get("travelMode") != "TRANSIT":
            continue
        t = st["transitDetails"]
        sd = t.get("stopDetails", {})
        a, b = sd.get("departureTime"), sd.get("arrivalTime")
        if a and b:
            ride += (dt.datetime.fromisoformat(b.replace("Z", "+00:00")) - dt.datetime.fromisoformat(a.replace("Z", "+00:00"))).total_seconds()
        else:
            ride += float(st.get("staticDuration", "0s").rstrip("s"))
        lines.append(t.get("transitLine", {}).get("nameShort", "?"))
        kinds.add(t.get("transitLine", {}).get("vehicle", {}).get("type", "?"))
    if not lines:
        return {"g_ok": False}
    return {"g_ok": True, "g_bus_min": round(ride / 60, 1), "g_total_min": round(float(r["duration"].rstrip("s")) / 60, 1),
            "g_bus_legs": len(lines), "g_lines": " + ".join(lines), "g_vehicle": "/".join(sorted(kinds))}


def metrics(pred: np.ndarray, obs: np.ndarray) -> dict:
    e = pred - obs
    return {"n": int(len(e)), "MAE_min": round(float(np.mean(np.abs(e))), 2),
            "MAPE_pct": round(float(np.mean(np.abs(e) / obs)) * 100, 1),
            "P50_min": round(float(np.median(np.abs(e))), 2),
            "within_5min_pct": round(float(np.mean(np.abs(e) <= 5)) * 100, 1),
            "within_10pct_pct": round(float(np.mean(np.abs(e) <= 0.1 * obs)) * 100, 1),
            "bias_min": round(float(np.mean(e)), 2)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=200, help="rides to look up (the 15 hand-lookup rows are always included)")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--today", default=dt.date.today().isoformat())
    args = ap.parse_args()
    k = key()
    rows = list(csv.DictReader(open(C.REPORTS / "validation_samples.csv")))
    manual = list(csv.DictReader(open(C.REPORTS / "gmaps_manual.csv")))
    sig = lambda r: (r["service_date"], r["depart_ist"], r["from_stop"], r["to_stop"])  # noqa: E731
    msig = {sig(r) for r in manual}
    first = [i for i, r in enumerate(rows) if sig(r) in msig]
    rest = [i for i in np.random.default_rng(args.seed).permutation(len(rows)) if i not in set(first)]
    pick = first + rest[: max(0, args.n - len(first))]

    CACHE.parent.mkdir(parents=True, exist_ok=True)
    cache = {}
    if CACHE.exists():
        for line in CACHE.read_text().splitlines():
            c = json.loads(line)
            cache[c["key"]] = c
    today = dt.date.fromisoformat(args.today)
    out, calls = [], 0
    with open(CACHE, "a") as fh:
        for i in pick:
            r = rows[i]
            wd = dt.date.fromisoformat(r["service_date"]).isoweekday()
            ck = "|".join(sig(r))
            if ck not in cache:
                depart = next_departure(wd, r["depart_ist"], today)
                resp = ask(k, r, depart)
                calls += 1
                cache[ck] = {"key": ck, "depart": depart, "resp": resp, "asked_at": dt.datetime.now(IST).isoformat()}
                fh.write(json.dumps(cache[ck]) + "\n")
                fh.flush()
                time.sleep(0.15)
            c = cache[ck]
            p = parse(c["resp"])
            out.append({"service_date": r["service_date"], "depart_ist": r["depart_ist"], "route_id": r["route_id"],
                        "from_stop": r["from_stop"], "to_stop": r["to_stop"], "stops": r["stops"], "dist_m": r["dist_m"],
                        "observed_min": round(float(r["observed_s"]) / 60, 1), "model_min": round(float(r["model_s"]) / 60, 1),
                        "live_min": round(float(r["live_s"]) / 60, 1), "timetable_min": round(float(r["timetable_s"]) / 60, 1),
                        "gmaps_depart": c["depart"], "error": c["resp"].get("error", ""), **p})
    errors = [o for o in out if o["error"]]
    if errors:
        print(f"{len(errors)} errors, first: {errors[0]['error']}")

    with open(C.REPORTS / "gmaps_comparison.csv", "w", newline="") as fh:
        cols = list(dict.fromkeys(k2 for o in out for k2 in o))
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows(out)

    ok = [o for o in out if o.get("g_ok")]
    res = {"asked": len(out), "google_found_bus_route": len(ok), "api_calls_this_run": calls}
    for name, sub in (("all", ok), ("google_single_bus", [o for o in ok if o["g_bus_legs"] == 1])):
        if not sub:
            continue
        obs = np.array([o["observed_min"] for o in sub])
        res[name] = {m: metrics(np.array([o[f"{m}_min"] for o in sub]), obs)
                     for m in ("g_bus", "live", "model", "timetable")}
    (C.REPORTS / "gmaps_metrics.json").write_text(json.dumps(res, indent=2))

    # fill the hand-lookup sheet
    gm = {(o["service_date"], o["depart_ist"], o["from_stop"], o["to_stop"]): o for o in out}
    col = "gmaps_bus_min (fill in)"
    for r in manual:
        o = gm.get(sig(r))
        if o and o.get("g_ok"):
            r[col] = o["g_bus_min"]
    with open(C.REPORTS / "gmaps_manual.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(manual[0]))
        w.writeheader()
        w.writerows(manual)
    print(json.dumps(res, indent=2))


if __name__ == "__main__":
    main()
