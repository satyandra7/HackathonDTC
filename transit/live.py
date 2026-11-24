"""Live blend (see reports/technical_note.md), evaluated by replaying a held-out day.

For a ride boarding at time t0 on day D, only data a live feed could have supplied at t0 is used: segment
traversals by *other* buses on D that finished in [t0 - WINDOW_S, t0).

  1. Corridor factor: on the segments of the ride's own route, the median ratio observed / historical over the
     window (r, from n observations), shrunk towards 1:  f = 1 + (r - 1) * n / (n + K_CORRIDOR).
     This captures "today this corridor is x% slower than usual".
  2. Per segment: if the segment itself was traversed in the window (n_s times, median time T_live, median age a),
     blend  T = w * T_live + (1 - w) * f * T_hist,  w = n_s / (n_s + M) * exp(-a / TAU_S).
     Otherwise  T = f * T_hist.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import duckdb
import numpy as np

from . import config as C

WINDOW_S = C.LIVE_WINDOW_S
M = C.LIVE_M
TAU_S = C.LIVE_TAU_S
K_CORRIDOR = C.LIVE_K_CORRIDOR
CITY_WINDOW_S = C.LIVE_CITY_WINDOW_S
K_CITY = C.LIVE_K_CITY
OWN_WINDOW_S = C.LIVE_OWN_WINDOW_S
K_OWN = C.LIVE_K_OWN
CORR_WINDOW_S = C.LIVE_CORR_WINDOW_S
OWN_CLIP = C.LIVE_OWN_CLIP


class LiveReplay:
    def __init__(self, net, files: list[Path]):
        self.net = net
        g = net.g
        flist = ", ".join(f"'{f}'" for f in files)
        a = duckdb.sql(f"""
            WITH e AS (
              SELECT service_date, run_id, stop_id, t_utc, observed,
                     lead(stop_id) OVER w AS nxt, lead(t_utc) OVER w AS nt, lead(observed) OVER w AS nobs,
                     lead(stop_pos) OVER w AS npos, stop_pos
              FROM read_parquet([{flist}], hive_partitioning = false)
              WINDOW w AS (PARTITION BY service_date, run_id ORDER BY stop_pos))
            SELECT service_date, run_id, stop_id, nxt, t_utc, nt
            FROM e WHERE observed AND nobs AND npos = stop_pos + 1 AND nt > t_utc""").fetchnumpy()
        si = g.stop_index
        ns = len(g.stop_ids)
        fa = np.array([si.get(s, -1) for s in a["stop_id"]], dtype=np.int64)
        fb = np.array([si.get(s, -1) for s in a["nxt"]], dtype=np.int64)
        ok = (fa >= 0) & (fb >= 0)
        day = np.array([str(x)[:10] for x in a["service_date"]])
        self.days = sorted(set(day[ok]))
        dcode = np.array([self.days.index(x) if x in self.days else -1 for x in day])
        key = (dcode * ns + fa) * ns + fb
        o = np.lexsort((a["nt"], key))
        o = o[ok[o]]
        self.key, self.t0, self.t1 = key[o], a["t_utc"][o], a["nt"][o]
        self.run = a["run_id"][o]
        self.ns = ns

        # city-wide factor: every traversal on a modelled segment, as observed / historical, by end time per day
        self.city: dict[str, tuple[np.ndarray, np.ndarray]] = {}
        if CITY_WINDOW_S > 0 and len(net.seg_est):
            sr = net.seg_row
            rows = np.array([sr.get((int(x), int(y)), -1) for x, y in zip(fa, fb)], dtype=np.int64)
            for di, dstr in enumerate(self.days):
                m = ok & (dcode == di) & (rows >= 0)
                mid = dt.datetime.fromisoformat(dstr).replace(tzinfo=dt.timezone.utc).timestamp() - C.IST_OFFSET_S
                h = ((a["t_utc"][m] - mid) // 3600).astype(np.int64) % 24
                hist = net.seg_est[rows[m], C.daytype_of(dstr), h]
                lr = np.log(np.maximum(a["nt"][m] - a["t_utc"][m], 1.0) / np.maximum(hist, 1.0))
                oo = np.argsort(a["nt"][m])
                self.city[dstr] = (a["nt"][m][oo], lr[oo])

    def city_factor(self, day: str, t0_utc: float) -> float:
        if day not in self.city:
            return 1.0
        t1, lr = self.city[day]
        lo, hi = np.searchsorted(t1, t0_utc - CITY_WINDOW_S), np.searchsorted(t1, t0_utc)
        n = hi - lo
        if n == 0:
            return 1.0
        r = float(np.exp(np.median(lr[lo:hi])))
        return 1.0 + (r - 1.0) * n / (n + K_CITY)

    def _obs(self, day: str, s_a: int, s_b: int):
        k = (self.days.index(day) * self.ns + s_a) * self.ns + s_b
        lo, hi = np.searchsorted(self.key, k, "left"), np.searchsorted(self.key, k, "right")
        return self.t0[lo:hi], self.t1[lo:hi], self.run[lo:hi]

    def ride_time(self, pid: int, i: int, j: int, t0_utc: float, day: str, d: int, exclude_run: int) -> float:
        net = self.net
        p = net.g.patterns[pid]
        midnight = dt.datetime.fromisoformat(day).replace(tzinfo=dt.timezone.utc).timestamp() - C.IST_OFFSET_S
        lo_t = t0_utc - WINDOW_S
        lo_c = t0_utc - (CORR_WINDOW_S or WINDOW_S)
        per_seg = []
        ratios = []
        # corridor factor from the whole route; per-segment live times from the ride's own segments
        for k in range(len(p.stops) - 1):
            ta, tb, rn = self._obs(day, int(p.stops[k]), int(p.stops[k + 1]))
            other = (tb < t0_utc) & (rn != exclude_run)
            if i <= k < j:
                m = other & (tb >= lo_t)
                per_seg.append((ta[m], tb[m]))
            m = other & (tb >= lo_c)
            for x0, x1 in zip(ta[m], tb[m]):
                hist = float(net.seg_times(pid, k, d, (x0 - midnight) / 3600))
                if hist > 0:
                    ratios.append((x1 - x0) / hist)
        n = len(ratios)
        g0 = self.city_factor(day, t0_utc)
        f = g0 + (float(np.median(ratios)) - g0) * n / (n + K_CORRIDOR) if n else g0
        # own-bus factor: how this very bus ran on its earlier segments (a live feed tracks the vehicle)
        if OWN_WINDOW_S > 0 and exclude_run is not None and exclude_run >= 0:
            own = []
            for k in range(0, i):
                ta, tb, rn = self._obs(day, int(p.stops[k]), int(p.stops[k + 1]))
                m = (rn == exclude_run) & (tb <= t0_utc) & (tb >= t0_utc - OWN_WINDOW_S)
                for x0, x1 in zip(ta[m], tb[m]):
                    hist = float(net.seg_times(pid, k, d, (x0 - midnight) / 3600))
                    if hist > 0:
                        own.append((x1 - x0) / hist)
            if own:
                no = len(own)
                ro = float(np.median(own))
                if OWN_CLIP > 0:
                    ro = min(max(ro, 1.0 / OWN_CLIP), OWN_CLIP)
                f = f + (ro - f) * no / (no + K_OWN)
        t = t0_utc
        for idx, k in enumerate(range(i, j)):
            hist = float(net.seg_times(pid, k, d, (t - midnight) / 3600))
            ta, tb = per_seg[idx]
            if len(ta):
                live = float(np.median(tb - ta))
                age = float(t0_utc - np.median(tb))
                w = len(ta) / (len(ta) + M) * np.exp(-age / TAU_S)
                t += w * live + (1 - w) * f * hist
            else:
                t += f * hist
        return t - t0_utc
