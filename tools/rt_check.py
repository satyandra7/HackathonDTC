"""Fetch a few snapshots of the real GTFS-RT VehiclePositions feed and record what it contains:
which fields are filled (what the feed actually carries), how many vehicles and routes, how fresh the
positions are, how often they change, and how many feed route_ids the official static GTFS knows.

  .venv/bin/python tools/rt_check.py [--snapshots 4] [--gap 20]
Writes reports/results/rt_feed_check.md and .json. The key is read from ~/.otd_key and never printed.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
from pathlib import Path

import polars as pl

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from transit import config as C  # noqa: E402
from transit import rt  # noqa: E402

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshots", type=int, default=4)
    ap.add_argument("--gap", type=float, default=20)
    args = ap.parse_args()
    url, headers = rt.feed_request()
    snaps = []
    for k in range(args.snapshots):
        t0 = time.time()
        blob = rt.fetch(url, headers)
        df, fill = rt.decode(blob)
        snaps.append({"fetched_utc": t0, "bytes": len(blob), "seconds": round(time.time() - t0, 2), "df": df, "fill": fill})
        if k < args.snapshots - 1:
            time.sleep(args.gap)
    last = snaps[-1]
    df = last["df"]
    routes = pl.read_csv(C.GTFS_DIR / "routes.txt", infer_schema_length=0)["route_id"].to_list()
    feed_routes = df["route_id"].drop_nulls().unique().to_list()
    age = last["fetched_utc"] - df["ts_utc"].cast(pl.Float64)
    # how many vehicles moved / reported a new timestamp between the first and last snapshot
    a, b = snaps[0]["df"], last["df"]
    j = a.join(b, on="vehicle_id", suffix="_b")
    res = {
        "fetched_ist": dt.datetime.fromtimestamp(last["fetched_utc"], IST).isoformat(timespec="seconds"),
        "snapshots": [{"bytes": s["bytes"], "seconds": s["seconds"], "entities": s["fill"]["entities"],
                       "header_age_s": round(s["fetched_utc"] - s["fill"]["header_timestamp"], 1)} for s in snaps],
        "fields_filled": {k: v for k, v in last["fill"].items() if k not in ("entities", "header_timestamp")},
        "vehicles": df["vehicle_id"].n_unique(), "routes_in_feed": len(feed_routes),
        "feed_routes_in_static_gtfs": sum(1 for r in feed_routes if r in set(routes)),
        "trip_id_prefix_equals_route_id": round(float(
            df.filter(pl.col("trip_id").is_not_null())
              .select((pl.col("trip_id").str.split("_").list.first() == pl.col("route_id")).mean()).item() or 0), 3),
        "position_age_s": {"median": round(float(age.median()), 1), "p90": round(float(age.quantile(0.9)), 1),
                           "share_older_than_5min": round(float((age > 300).mean()), 3)},
        "vehicles_with_new_timestamp_between_first_and_last": int((j["ts_utc_b"] > j["ts_utc"]).sum()),
        "vehicles_in_both": j.height,
        "seconds_between_first_and_last": round(last["fetched_utc"] - snaps[0]["fetched_utc"], 1),
        "sample_trip_ids": df["trip_id"].drop_nulls().head(3).to_list(),
    }
    C.REPORTS.mkdir(parents=True, exist_ok=True)
    (C.REPORTS / "rt_feed_check.json").write_text(json.dumps(res, indent=2))
    f = res["fields_filled"]
    n = last["fill"]["entities"]
    lines = ["# Live GTFS-RT feed check", "",
             f"Fetched {res['fetched_ist']}: {args.snapshots} snapshots {args.gap:.0f} s apart from the endpoint in the "
             "approval email (`x-api-key` header).", "",
             "| Snapshot | Size | Fetch time | Vehicles | Header age |", "|---|---|---|---|---|"]
    for i, s in enumerate(res["snapshots"], 1):
        lines.append(f"| {i} | {s['bytes'] / 1e6:.2f} MB | {s['seconds']} s | {s['entities']:,} | {s['header_age_s']} s |")
    lines += ["", "## Fields filled (last snapshot)", "", "| Field | Filled |", "|---|---|"]
    for k, v in f.items():
        lines.append(f"| `{k}` | {v:,} of {n:,} ({100 * v / max(1, n):.0f}%) |")
    lines += ["", "## Contents", "",
              f"- {res['vehicles']:,} vehicles on {res['routes_in_feed']:,} routes; "
              f"**{res['feed_routes_in_static_gtfs']:,}** of those routes are in the official static GTFS.",
              f"- `trip_id` prefix equals `route_id` for {res['trip_id_prefix_equals_route_id'] * 100:.0f}% of vehicles "
              f"(e.g. {', '.join(res['sample_trip_ids'])}).",
              f"- Position age at fetch: median {res['position_age_s']['median']} s, P90 {res['position_age_s']['p90']} s; "
              f"{res['position_age_s']['share_older_than_5min'] * 100:.0f}% older than 5 min.",
              f"- Between the first and last snapshot ({res['seconds_between_first_and_last']} s), "
              f"{res['vehicles_with_new_timestamp_between_first_and_last']:,} of {res['vehicles_in_both']:,} vehicles "
              "reported a newer position."]
    (C.REPORTS / "rt_feed_check.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
