"""Build a small synthetic GTFS feed + cleaned GPS pings to test the pipeline end to end without real data.

Two crossing routes in central Delhi coordinates:
  route 101: 12 stops west -> east (and back), 500 m apart
  route 202: 10 stops south -> north (and back), crossing route 101 near its 6th stop
Trips every 15 min, 05:00-23:30, plus one trip at 24:15:00 (past midnight). Timetable assumes 20 km/h.

Simulated buses (route 101 both ways, route 202 northbound only) drive at 12 km/h in the weekday peaks
(08-10, 17-20) and 28 km/h otherwise (30 km/h all day at weekends), dwell 20 s at each stop, report
every 11 s with 6 m GPS noise. Route 202 southbound has no GPS, so it exercises the city-speed fallback.

Usage: python tests/make_fixture.py <out_dir>     (then run the pipeline with DTC_DATA=<out_dir>)
"""

from __future__ import annotations

import csv
import datetime as dt
import sys
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

IST = 19800
LAT0, LON0 = 28.62, 77.20
M_LAT = 1 / 110540
M_LON = 1 / (111320 * np.cos(np.radians(LAT0)))
DAYS = [f"2026-10-{d}" for d in (21, 22, 23, 24, 25, 26, 27, 28, 31)]


def stops_line(prefix, n, start, step, offset=(0.0, 0.0)):
    out = []
    for i in range(n):
        y = start[1] + step[1] * i + offset[1]
        x = start[0] + step[0] * i + offset[0]
        out.append((f"{prefix}{i + 1}", f"{prefix.upper()} Stop {i + 1}", LAT0 + y * M_LAT, LON0 + x * M_LON, x, y))
    return out


def main(out: Path) -> None:
    gt = out / "raw/gtfs"
    gt.mkdir(parents=True, exist_ok=True)
    a_e = stops_line("a", 12, (-3000, 0), (500, 0))              # route 101 eastbound
    a_w = stops_line("aw", 12, (2500, 0), (-500, 0), (0, 25))    # westbound, opposite kerb
    b_n = stops_line("b", 10, (-50, -2500), (0, 500))            # route 202 northbound, crosses near a6
    b_s = stops_line("bs", 10, (-25, 2000), (0, -500))
    a_e[0] = ("a1", "Kashmere Gate ISBT", *a_e[0][2:])
    b_n[-1] = ("b10", "Nehru Place", *b_n[-1][2:])
    patterns = {("101", "0"): a_e, ("101", "1"): a_w, ("202", "0"): b_n, ("202", "1"): b_s}

    def w(name, header, rows):
        with open(gt / name, "w", newline="") as fh:
            c = csv.writer(fh)
            c.writerow(header)
            c.writerows(rows)

    w("agency.txt", ["agency_id", "agency_name", "agency_url", "agency_timezone"],
      [["DTC", "Delhi Transport Corporation", "https://example.org", "Asia/Kolkata"]])
    allstops = {s[0]: s for p in patterns.values() for s in p}
    w("stops.txt", ["stop_id", "stop_name", "stop_lat", "stop_lon", "location_type"],
      [[s[0], s[1], f"{s[2]:.6f}", f"{s[3]:.6f}", "0"] for s in allstops.values()])
    w("routes.txt", ["route_id", "agency_id", "route_short_name", "route_long_name", "route_type"],
      [["101", "DTC", "101", "Kashmere Gate - East", "3"], ["202", "DTC", "202", "South - Nehru Place", "3"]])
    w("calendar.txt", ["service_id", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
                       "start_date", "end_date"], [["ALL", 1, 1, 1, 1, 1, 1, 1, "20261001", "20261031"]])
    trips, st = [], []
    sched = {}
    starts = list(range(5 * 3600, 23 * 3600 + 1801, 15 * 60)) + [24 * 3600 + 15 * 60]
    for (route, d), stops in patterns.items():
        for s0 in starts:
            tid = f"T{route}_{d}_{s0}"
            trips.append([route, "ALL", tid, d])
            sched[tid] = (route, d, s0)
            for k, s in enumerate(stops):
                t = s0 + int(k * 500 / (20 / 3.6))
                hms = f"{t // 3600:02d}:{t % 3600 // 60:02d}:{t % 60:02d}"
                blank = 0 < k < len(stops) - 1 and k % 3 == 1          # some blank intermediate times
                st.append([tid, "" if blank else hms, "" if blank else hms, s[0], k + 1])
    w("trips.txt", ["route_id", "service_id", "trip_id", "direction_id"], trips)
    w("stop_times.txt", ["trip_id", "arrival_time", "departure_time", "stop_id", "stop_sequence"], st)

    rng = np.random.default_rng(7)
    vid = 0
    for day in DAYS:
        weekend = dt.date.fromisoformat(day).isoweekday() >= 6
        midnight = dt.datetime.fromisoformat(day).replace(tzinfo=dt.timezone.utc).timestamp() - IST
        cols = {k: [] for k in ("vehicle_id", "trip_id", "route_id", "ts_utc", "end_ts_utc", "lat", "lon")}
        for tid, (route, d, s0) in sched.items():
            if route == "202" and d == "1":
                continue
            stops = patterns[(route, d)]
            vid += 1
            t = midnight + s0 + rng.uniform(0, 180)
            xs = np.array([s[4] for s in stops])
            ys = np.array([s[5] for s in stops])
            track_t, track_x, track_y = [], [], []
            for k in range(len(stops)):
                track_t += [t, t + 20]
                track_x += [xs[k]] * 2
                track_y += [ys[k]] * 2
                t += 20
                if k + 1 < len(stops):
                    h = ((t + IST) % 86400) / 3600
                    v = 30 if weekend else (12 if (8 <= h < 10 or 17 <= h < 20) else 28)
                    t += 500 / (v / 3.6)
            tt = np.arange(track_t[0], track_t[-1], 11.0)
            x = np.interp(tt, track_t, track_x) + rng.normal(0, 6, len(tt))
            y = np.interp(tt, track_t, track_y) + rng.normal(0, 6, len(tt))
            hh, mm = divmod(s0 // 60, 60)
            rt_trip = f"{route}_{hh % 24:02d}_{mm:02d}_{rng.integers(1000, 9999)}"
            n = len(tt)
            cols["vehicle_id"] += [f"DL1PX{vid:05d}"] * n
            cols["trip_id"] += [rt_trip] * n
            cols["route_id"] += [float(route)] * n
            cols["ts_utc"] += tt.astype(np.int64).tolist()
            cols["end_ts_utc"] += tt.astype(np.int64).tolist()
            cols["lat"] += (LAT0 + y * M_LAT).tolist()
            cols["lon"] += (LON0 + x * M_LON).tolist()
        p = out / f"clean/pings/service_date={day}"
        p.mkdir(parents=True, exist_ok=True)
        pq.write_table(pa.table(cols), p / "part-0.parquet")
    print(f"fixture written to {out}: {len(allstops)} stops, {len(trips)} trips, {len(DAYS)} days of pings")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
