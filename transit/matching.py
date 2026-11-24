"""Matching GPS tracks to GTFS stop sequences (stop-event stage and live worker)."""

from __future__ import annotations

import datetime as dt

import duckdb
import numpy as np

from . import config as C
from . import geo


def snap(px, py, t_in, t_out, sx, sy, radius):
    """Passing time at each stop (NaN if the track never comes within `radius`), forward only.

    px, py: ping positions (m); t_in / t_out: first / last time at that position (stationary pings are
    compressed in Stage 1); sx, sy: stop positions (m), in pattern order. The time is read at the point of
    the track closest to the stop, interpolated between two consecutive pings."""
    ax, ay, bx, by = px[:-1], py[:-1], px[1:], py[1:]
    dx, dy = bx - ax, by - ay
    l2 = dx * dx + dy * dy
    u = ((sx[:, None] - ax) * dx + (sy[:, None] - ay) * dy) / np.where(l2 > 0, l2, 1.0)
    u = np.clip(np.where(l2 > 0, u, 0.0), 0.0, 1.0)
    d = np.hypot(sx[:, None] - (ax + u * dx), sy[:, None] - (ay + u * dy))
    tseg = t_out[:-1] + u * (t_in[1:] - t_out[:-1])
    out = np.full(len(sx), np.nan)
    last = 0
    nseg = d.shape[1]
    for j in range(len(sx)):
        row = d[j, last:]
        ok = np.flatnonzero(row <= radius)
        if ok.size == 0:
            continue
        a = ok[0]
        b = a
        while b + 1 < row.size and row[b + 1] <= radius:   # contiguous pass near the stop
            b += 1
        k = last + a + int(np.argmin(row[a:b + 1]))
        out[j] = tseg[j, k]
        last = min(k, nseg - 1)
    return out


def monotone(t):
    """Drop observed times that go backwards."""
    t = t.copy()
    prev = -np.inf
    for j in range(len(t)):
        if np.isnan(t[j]):
            continue
        if t[j] < prev:
            t[j] = np.nan
        else:
            prev = t[j]
    return t


def parse_start(trip_id: str) -> int | None:
    """Seconds since midnight from a ROUTE_HH_MM[_NNNN] trip id."""
    parts = trip_id.split("_")
    if len(parts) >= 3 and parts[1].isdigit() and parts[2].isdigit():
        return int(parts[1]) * 3600 + int(parts[2]) * 60
    return None


def circ_gap(a, b):
    """|a - b| in seconds on a 24 h circle (a trip_id says 00:15 for a GTFS trip at 24:15:00)."""
    return np.abs((np.asarray(a, dtype=np.int64) - b + 43200) % 86400 - 43200)


class DayRuns:
    """One service day of cleaned pings, split into runs (one vehicle, one trip_id, no gap > RUN_GAP_S)."""

    def __init__(self, day: str):
        src = C.PINGS / f"service_date={day}"
        if not src.is_dir():
            raise FileNotFoundError(f"no pings in {src}")
        con = duckdb.connect()
        con.execute("SET threads=4")
        con.execute(f"""
            CREATE TEMP TABLE p AS
            SELECT vehicle_id, trip_id, CAST(route_id AS BIGINT) AS route_id, ts_utc, end_ts_utc, lat, lon
            FROM read_parquet('{src}/*.parquet')
            QUALIFY row_number() OVER (PARTITION BY vehicle_id, ts_utc ORDER BY end_ts_utc DESC) = 1""")
        self.vmap = [r[0] for r in con.execute("SELECT DISTINCT vehicle_id FROM p ORDER BY 1").fetchall()]
        self.tmap = [r[0] for r in con.execute("SELECT DISTINCT coalesce(trip_id, '') FROM p ORDER BY 1").fetchall()]
        a = con.execute("""
            SELECT dense_rank() OVER (ORDER BY vehicle_id) - 1 AS vk,
                   dense_rank() OVER (ORDER BY coalesce(trip_id, '')) - 1 AS tk,
                   coalesce(route_id, -1) AS route_id, ts_utc, end_ts_utc, lat, lon
            FROM p ORDER BY vehicle_id, ts_utc""").fetchnumpy()
        con.close()
        self.day = day
        self.vk, self.tk = a["vk"].astype(np.int64), a["tk"].astype(np.int64)
        self.rid = a["route_id"].astype(np.int64)
        self.ts, self.te = a["ts_utc"].astype(np.float64), a["end_ts_utc"].astype(np.float64)
        self.px, self.py = geo.to_xy(a["lat"], a["lon"])
        n = len(self.vk)
        brk = np.ones(n, dtype=bool)
        brk[1:] = (self.vk[1:] != self.vk[:-1]) | (self.tk[1:] != self.tk[:-1]) | (self.ts[1:] - self.te[:-1] > C.RUN_GAP_S)
        self.starts = np.flatnonzero(brk)
        self.ends = np.append(self.starts[1:], n)
        self.midnight = dt.datetime.fromisoformat(day).replace(tzinfo=dt.timezone.utc).timestamp() - C.IST_OFFSET_S

    def __len__(self):
        return len(self.starts)


