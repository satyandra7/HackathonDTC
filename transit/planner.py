"""Journey planner: time-dependent RAPTOR on GTFS patterns with model-based in-vehicle times
(see reports/technical_note.md).

- When buses leave comes from GTFS: each trip's scheduled departure from its first stop.
- How long they take comes from the segment model: starting from that departure, each segment's time
  is the model estimate for the day type and the hour the bus reaches it. Segments the model has
  never seen use distance / city-wide median speed for that day type and hour.
- Walking: 1.3 x straight-line distance at 4.5 km/h; transfers between stops <= 400 m apart; access and
  egress within 800 m (widened to 1.5 km if fewer than 3 stops).
- Up to 3 options: the Pareto set over (arrival, number of buses), then alternatives found by banning
  the first bus of options already found.
"""

from __future__ import annotations

import datetime as dt
import difflib
import re
from dataclasses import dataclass

import numpy as np
import polars as pl
from numba import njit
from scipy.spatial import cKDTree

from . import config as C
from . import geo
from .gtfs import GTFS

INF = np.inf


@njit(cache=True)
def _scan_pattern(T, rows_ok, stops, tau_prev, tau_k, best, start, buf, best_target, nopick, nodrop):
    """One RAPTOR route scan: ride the current trip, improve arrivals, hop on an earlier trip where possible.
    Updates tau_k and best in place; returns the improved stops with (trip row, boarding position, position)."""
    ntr, nst = T.shape
    has_pick = nopick.shape[0] == ntr and nopick.shape[1] == nst
    has_drop = nodrop.shape[0] == ntr and nodrop.shape[1] == nst
    out_s = np.empty(nst, np.int64)
    out_r = np.empty(nst, np.int64)
    out_b = np.empty(nst, np.int64)
    out_i = np.empty(nst, np.int64)
    m = 0
    cur = -1
    board = -1
    for i in range(start, nst):
        s = stops[i]
        if cur >= 0:
            ta = T[cur, i]
            if ta < best[s] and ta < best_target and not (has_drop and nodrop[cur, i]):
                tau_k[s] = ta
                best[s] = ta
                out_s[m] = s
                out_r[m] = cur
                out_b[m] = board
                out_i[m] = i
                m += 1
        prev = tau_prev[s]
        if prev < np.inf and (cur < 0 or prev <= T[cur, i]):
            need = prev + buf
            cand = -1
            bt = np.inf
            for r in range(ntr):
                if rows_ok[r] and T[r, i] >= need and T[r, i] < bt:
                    bt = T[r, i]
                    cand = r
            if cand >= 0 and (cur < 0 or bt < T[cur, i]) and not (has_pick and nopick[cand, i]):
                cur = cand
                board = i
    return out_s[:m], out_r[:m], out_b[:m], out_i[:m]


WALK_MPS = C.WALK_KMH / 3.6


def _plain(x):
    """NumPy scalars -> Python numbers, recursively (for JSON)."""
    if isinstance(x, dict):
        return {k: _plain(v) for k, v in x.items()}
    if isinstance(x, list):
        return [_plain(v) for v in x]
    if isinstance(x, np.generic):
        return x.item()
    return x


def walk_s(dist_m):
    return np.asarray(dist_m) * C.WALK_DETOUR / WALK_MPS


@dataclass
class Place:
    lat: float
    lon: float
    label: str


class Network:
    def __init__(self, gtfs: GTFS | None = None, model_dir=None):
        self.g = g = gtfs or GTFS(C.GTFS_DIR)
        model_dir = model_dir or C.MODEL
        ns = len(g.stop_ids)

        # ---- model tables
        prof_file, city_file = model_dir / "segment_profiles.parquet", model_dir / "city_speed.parquet"
        self.city_kmh = np.full((C.N_DAYTYPES, 24), C.DEFAULT_KMH)
        seg_row: dict[tuple[int, int], int] = {}
        if prof_file.exists():
            pf = pl.read_parquet(prof_file).sort(["from_stop", "to_stop", "daytype", "hour"])
            keys = pf.select("from_stop", "to_stop").unique(maintain_order=True)
            S = keys.height
            self.seg_est = pf["est_s"].to_numpy().reshape(S, -1, 24).astype(np.float64)
            self.seg_p85 = pf["p85_s"].to_numpy().reshape(S, -1, 24).astype(np.float64)
            self.seg_conf = pf["confidence"].to_numpy().reshape(S, -1, 24).astype(np.float64)
            for i, (a, b) in enumerate(keys.iter_rows()):
                if a in g.stop_index and b in g.stop_index:
                    seg_row[(g.stop_index[a], g.stop_index[b])] = i
        else:
            self.seg_est = self.seg_p85 = self.seg_conf = np.zeros((0, C.N_DAYTYPES, 24))
        if city_file.exists():
            cf = pl.read_parquet(city_file)
            for d, h, v in cf.iter_rows():
                self.city_kmh[d, h] = v
        self.seg_row = seg_row
        # road geometry per segment, traced from GPS (pipeline/05_shapes.py); straight lines where missing
        self.shapes: dict[tuple[int, int], list[list[float]]] = {}
        shp = model_dir / "segment_shapes.parquet"
        if shp.exists():
            sf = pl.read_parquet(shp)
            for a, b, la, lo in zip(sf["from_stop"], sf["to_stop"], sf["lat"].to_list(), sf["lon"].to_list()):
                if a in g.stop_index and b in g.stop_index:
                    self.shapes[(g.stop_index[a], g.stop_index[b])] = [[y, x] for y, x in zip(la, lo)]
        self.model_loaded = prof_file.exists()

        # ---- per pattern: segment rows and lengths
        self.pat_seg = []
        self.pat_len = []
        for p in g.patterns:
            self.pat_seg.append(np.array([seg_row.get((int(p.stops[k]), int(p.stops[k + 1])), -1)
                                          for k in range(len(p.stops) - 1)], dtype=np.int64))
            self.pat_len.append(np.diff(p.cumdist))
        # unobserved segments: city speed, scaled by how this route's observed segments compare to city speed
        self.pat_fac = np.ones(len(g.patterns))
        if C.FALLBACK_ROUTE and len(self.seg_est):
            city_s_per_m = np.mean(3.6 / self.city_kmh)
            for pid in range(len(g.patterns)):
                rows, ln = self.pat_seg[pid], self.pat_len[pid]
                ok = (rows >= 0) & (ln > 30)
                n = int(ok.sum())
                if n:
                    r = float(np.median(self.seg_est[rows[ok]].mean(axis=(1, 2)) / (ln[ok] * city_s_per_m)))
                    self.pat_fac[pid] = 1 + (r - 1) * n / (n + C.FALLBACK_K)

        # ---- predicted times per pattern and day type: matrix [trips, stops]
        self.pat_times = [[None] * C.N_DAYTYPES for _ in g.patterns]
        for pid, p in enumerate(g.patterns):
            start = g.trip_start[p.trips].astype(np.float64)
            for d in range(C.N_DAYTYPES):
                self.pat_times[pid][d] = self.ride_matrix(pid, start, d)
        self.pat_trips = [np.array(p.trips, dtype=np.int64) for p in g.patterns]

        # ---- stop -> (pattern, position) list, walking transfers, spatial index
        self.stop_pats: list[list[tuple[int, int]]] = [[] for _ in range(ns)]
        for pid, p in enumerate(g.patterns):
            for k, s in enumerate(p.stops):
                self.stop_pats[s].append((pid, k))
        self.served = np.array([bool(v) for v in self.stop_pats]) & g.stop_boardable
        xy = np.column_stack([g.stop_x, g.stop_y])
        self.tree = cKDTree(xy)
        self.transfers: list[list[tuple[int, float]]] = [[] for _ in range(ns)]
        for a, b in self.tree.query_pairs(C.TRANSFER_RADIUS_M, output_type="ndarray"):
            if self.served[a] and self.served[b]:
                w = float(walk_s(np.hypot(*(xy[a] - xy[b]))))
                self.transfers[a].append((int(b), w))
                self.transfers[b].append((int(a), w))
        self._norm_names = [self._norm(s) for s in g.stop_names]
        self._live: dict[str, object] = {}

    def live_for(self, ds: str):
        """Live data for a date: the live worker's stop events (real GTFS-RT feed, reloaded when they change), else a
        replay of archived GPS stop events (e.g. an October 2026 day), else None."""
        if not C.LIVE_IN_API:
            return None
        lf = C.LIVE_DIR / "clean/stop_events" / f"service_date={ds}" / "part.parquet"
        if lf.exists():
            m = lf.stat().st_mtime
            cached = self._live.get(("live", ds))
            if cached is None or cached[0] != m:
                from .live import LiveReplay
                try:
                    self._live[("live", ds)] = (m, LiveReplay(self, [lf]))
                except Exception:          # file being replaced: keep the previous version
                    if cached is None:
                        return None
            return self._live[("live", ds)][1]
        if ds not in self._live:
            f = C.STOP_EVENTS / f"service_date={ds}" / "part.parquet"
            if f.exists():
                from .live import LiveReplay
                self._live[ds] = LiveReplay(self, [f])
            else:
                self._live[ds] = None
        return self._live[ds]

    def leg_path(self, stops) -> list[list[float]]:
        """Polyline along the road for consecutive stops (GPS-traced segment shapes, else straight)."""
        g = self.g
        path = [[float(g.stop_lat[stops[0]]), float(g.stop_lon[stops[0]])]]
        for a, b in zip(stops[:-1], stops[1:]):
            seg = self.shapes.get((int(a), int(b)))
            path.extend(seg[1:] if seg else [[float(g.stop_lat[b]), float(g.stop_lon[b])]])
        return path

    # ------------------------------------------------------------------ model-based ride times
    def seg_times(self, pid: int, k: np.ndarray | int, d: int, hour, which: str = "est") -> np.ndarray:
        """Segment time for hour of day `hour` (may be fractional: with C.HOUR_INTERP the hourly profile is
        interpolated linearly between hour centres, so times do not jump at the top of each hour)."""
        rows = self.pat_seg[pid][k]
        hf = np.asarray(hour, dtype=np.float64)
        tab = self.seg_est if which == "est" else self.seg_p85
        if C.HOUR_INTERP:
            h = hf - 0.5
            h0 = np.floor(h)
            w = h - h0
            ha, hb = h0.astype(np.int64) % 24, (h0.astype(np.int64) + 1) % 24
            r = np.maximum(rows, 0)
            model = (tab[r, d, ha] * (1 - w) + tab[r, d, hb] * w) if len(tab) else np.zeros(np.shape(rows))
            kmh = self.city_kmh[d, ha] * (1 - w) + self.city_kmh[d, hb] * w
        else:
            hour = np.floor(hf).astype(np.int64) % 24
            model = tab[np.maximum(rows, 0), d, hour] if len(tab) else np.zeros(np.shape(rows))
            kmh = self.city_kmh[d, hour]
        city = self.pat_len[pid][k] / (kmh / 3.6) * self.pat_fac[pid]
        if which == "p85":
            city = city * 1.3
        return np.where(rows >= 0, model, city)

    def ride_matrix(self, pid: int, start: np.ndarray, d: int, which: str = "est") -> np.ndarray:
        nst = len(self.g.patterns[pid].stops)
        T = np.empty((len(start), nst))
        T[:, 0] = start
        for k in range(nst - 1):
            T[:, k + 1] = T[:, k] + self.seg_times(pid, k, d, T[:, k] / 3600, which)
        return T

    def ride_time(self, pid: int, i: int, j: int, t0: float, d: int, which: str = "est") -> float:
        """Model ride time from pattern position i to j, leaving i at t0 (s since service midnight)."""
        t = t0
        for k in range(i, j):
            t += float(self.seg_times(pid, k, d, t / 3600, which))
        return t - t0

    def ride_confidence(self, pid: int, i: int, j: int, d: int, t0: float) -> float:
        rows = self.pat_seg[pid][i:j]
        if not len(rows) or not len(self.seg_conf):
            return 0.0
        h = int(t0 // 3600) % 24
        return float(np.mean(np.where(rows >= 0, self.seg_conf[np.maximum(rows, 0), d, h], 0.0)))

    # ------------------------------------------------------------------ places and stop search
    @staticmethod
    def _norm(s: str) -> str:
        return re.sub(r"[^a-z0-9 ]+", " ", s.lower()).strip()

    def search_stops(self, q: str, limit: int = 10) -> list[dict]:
        nq = self._norm(q)
        if not nq:
            return []
        scored: dict[str, tuple[float, list[int]]] = {}
        for i, name in enumerate(self._norm_names):
            if not self.served[i]:
                continue
            if name == nq:
                sc = 3.0
            elif name.startswith(nq):
                sc = 2.0
            elif nq in name:
                sc = 1.5
            elif all(w in name for w in nq.split()):
                sc = 1.2
            else:
                continue
            key = self.g.stop_names[i]
            prev = scored.get(key)
            scored[key] = (max(sc, prev[0] if prev else 0), (prev[1] if prev else []) + [i])
        if not scored:   # fuzzy fallback
            names = list({self.g.stop_names[i] for i in np.flatnonzero(self.served)})
            for key in difflib.get_close_matches(q, names, n=limit, cutoff=0.6):
                idx = [i for i in np.flatnonzero(self.served) if self.g.stop_names[i] == key]
                scored[key] = (1.0, idx)
        out = []
        for key, (sc, idx) in sorted(scored.items(), key=lambda kv: (-kv[1][0], len(kv[0])))[:limit]:
            out.append({"name": key, "score": sc, "stop_ids": [self.g.stop_ids[i] for i in idx],
                        "lat": float(np.mean(self.g.stop_lat[idx])), "lon": float(np.mean(self.g.stop_lon[idx]))})
        return out

    def resolve(self, text: str) -> Place:
        m = re.fullmatch(r"\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*", text)
        if m:
            return Place(float(m.group(1)), float(m.group(2)), text.strip())
        hits = self.search_stops(text, 1)
        if not hits:
            raise ValueError(f"no stop matches '{text}'")
        return Place(hits[0]["lat"], hits[0]["lon"], hits[0]["name"])

    def nearby(self, place: Place) -> tuple[np.ndarray, np.ndarray]:
        x, y = geo.to_xy(place.lat, place.lon)
        for r in (C.ACCESS_RADIUS_M, C.ACCESS_RADIUS_WIDE_M):
            idx = np.array([i for i in self.tree.query_ball_point([float(x), float(y)], r) if self.served[i]], dtype=np.int64)
            if len(idx) >= C.MIN_ACCESS_STOPS:
                break
        if len(idx) == 0:
            return idx, np.array([])
        d = np.hypot(self.g.stop_x[idx] - x, self.g.stop_y[idx] - y)
        return idx, walk_s(d)

    # ------------------------------------------------------------------ RAPTOR
    def _pickdrop(self, pid: int):
        """[trips, stops] no-pickup / no-drop-off matrices for a pattern (1 x 1 zeros if none are set)."""
        cache = self.__dict__.setdefault("_pd_cache", {})
        if pid not in cache:
            trips = self.pat_trips[pid]
            n = len(self.g.patterns[pid].stops)
            pk = [self.g.trip_no_pickup[t] for t in trips]
            dr = [self.g.trip_no_dropoff[t] for t in trips]
            z = np.zeros((1, 1), dtype=np.bool_)
            mp = np.array([x if x is not None else np.zeros(n, bool) for x in pk]) if any(x is not None for x in pk) else z
            md = np.array([x if x is not None else np.zeros(n, bool) for x in dr]) if any(x is not None for x in dr) else z
            cache[pid] = (mp, md)
        return cache[pid]

    def raptor(self, acc, acc_w, egr, egr_w, dep: float, d: int, active: np.ndarray, banned: set[int]):
        ns = len(self.g.stop_ids)
        R = C.MAX_ROUNDS
        tau = np.full((R + 1, ns), INF)
        best = np.full(ns, INF)
        labels: list[dict[int, tuple]] = [dict() for _ in range(R + 1)]
        for s, w in zip(acc, acc_w):
            if dep + w < tau[0, s]:
                tau[0, s] = best[s] = dep + w
                labels[0][int(s)] = ("access", float(w))
        egr_map = dict(zip(egr.tolist(), egr_w.tolist()))
        marked = set(int(s) for s in acc)
        results = []
        best_target = INF
        for k in range(1, R + 1):
            tau[k] = tau[k - 1]
            queue: dict[int, int] = {}
            for s in marked:
                for pid, pos in self.stop_pats[s]:
                    if pid in banned:
                        continue
                    if pos < queue.get(pid, 1 << 30):
                        queue[pid] = pos
            improved = set()
            for pid, start in queue.items():
                rows_ok = active[self.pat_trips[pid]]
                if not rows_ok.any():
                    continue
                nopick, nodrop = self._pickdrop(pid)
                imp_s, imp_row, imp_board, imp_i = _scan_pattern(
                    self.pat_times[pid][d], rows_ok, self.g.patterns[pid].stops, tau[k - 1], tau[k], best,
                    start, float(C.TRANSFER_BUFFER_S if k > 1 else 0), best_target, nopick, nodrop)
                for s_, r_, b_, i_ in zip(imp_s.tolist(), imp_row.tolist(), imp_board.tolist(), imp_i.tolist()):
                    labels[k][s_] = ("ride", pid, r_, b_, i_)
                    improved.add(s_)
            # walking transfers from stops just reached by bus
            walked = set()
            for s in improved:
                for nb, w in self.transfers[s]:
                    t = tau[k, s] + w
                    if t < best[nb] and t < best_target:
                        tau[k, nb] = best[nb] = t
                        labels[k][nb] = ("walk", s, w)
                        walked.add(nb)
            marked = improved | walked
            arr, end = INF, -1
            for s, w in egr_map.items():
                if tau[k, s] + w < arr:
                    arr, end = tau[k, s] + w, s
            if arr < best_target:
                best_target = arr
                results.append((k, end, arr))
            if not marked:
                break
        return results, labels, tau

    def _label(self, labels, k, s):
        while k >= 0:
            if s in labels[k]:
                return k, labels[k][s]
            k -= 1
        return -1, None

    def _rebuild(self, labels, k, end, egr_w_of, dep):
        legs = []
        s = end
        while True:
            k, lab = self._label(labels, k, s)
            if lab is None:
                return None
            if lab[0] == "access":
                legs.append(("access", s, lab[1]))
                break
            if lab[0] == "walk":          # transfer walk within round k; the stop walked from was reached by bus
                legs.append(("walk", lab[1], s, lab[2]))
                s = lab[1]
                if len(legs) > 4 * C.MAX_ROUNDS:
                    return None
                continue
            _, pid, row, bi, ai = lab
            legs.append(("ride", pid, row, bi, ai))
            s = int(self.g.patterns[pid].stops[bi])
            k -= 1
        legs.reverse()
        return legs

    # ------------------------------------------------------------------ public entry point
    def journeys(self, origin: str, dest: str, depart: dt.datetime, max_options: int = C.MAX_OPTIONS) -> dict:
        o, de = self.resolve(origin), self.resolve(dest)
        local = depart.astimezone(dt.timezone(dt.timedelta(seconds=C.IST_OFFSET_S))) if depart.tzinfo else depart
        acc, acc_w = self.nearby(o)
        egr, egr_w = self.nearby(de)
        out = {"from": o.__dict__, "to": de.__dict__, "depart": local.isoformat(), "options": [], "notes": []}
        if not len(acc) or not len(egr):
            out["notes"].append("no bus stop within 1.5 km of the origin or destination")
            return out
        straight = float(geo.haversine_m(o.lat, o.lon, de.lat, de.lon))
        found = []
        # service day: the calendar date; just after midnight also try the previous day's late trips
        days = [(local.date(), local.hour * 3600 + local.minute * 60 + local.second)]
        if local.hour < 4:
            days.append((local.date() - dt.timedelta(days=1), days[0][1] + 86400))
        for date, dep in days:
            ds = date.isoformat()
            d = C.daytype_of(ds)
            active = self.g.active_trip_mask(ds)
            _, used = self.g.services_on(ds)
            if used != ds:
                out["notes"].append(f"GTFS has no service for {ds}; used the timetable of {used} (same weekday)")
            banned: set[int] = set()
            for _ in range(max_options + 1):
                res, labels, tau = self.raptor(acc, acc_w, egr, egr_w, dep, d, active, banned)
                if not res:
                    break
                new_first = None
                for k, end, arr in res:
                    legs = self._rebuild(labels, k, end, None, dep)
                    if legs is None:
                        continue
                    opt = self._describe(legs, end, dict(zip(egr.tolist(), egr_w.tolist()))[end], dep, d, date, o, de,
                                         self.live_for(ds))
                    sig = tuple(l["route"] for l in opt["legs"] if l["mode"] == "bus")
                    same = [f for f in found if f["_sig"] == sig]
                    if sig and (not same or opt["total_minutes"] < same[0]["total_minutes"]):
                        if same:
                            found.remove(same[0])
                        opt["_sig"] = sig
                        found.append(opt)
                        if new_first is None:
                            new_first = next(l["_pid"] for l in opt["legs"] if l["mode"] == "bus")
                if new_first is None or len(found) >= max_options:
                    break
                banned.add(new_first)
        found.sort(key=lambda f: f["total_minutes"])
        for f in found[:max_options]:
            f.pop("_sig")
            f["legs"] = [l for l in f["legs"] if not (l["mode"] == "walk" and l["meters"] < 1)]
            for l in f["legs"]:
                l.pop("_pid", None)
            out["options"].append(f)
        out["options"] = _plain(out["options"])
        if straight < 1200:
            out["notes"].append(f"origin and destination are {straight:.0f} m apart; walking ({float(walk_s(straight)) / 60:.0f} min) may be faster")
        return out

    def _clock(self, date: dt.date, secs: float) -> str:
        t = dt.datetime.combine(date, dt.time()) + dt.timedelta(seconds=float(secs))
        return t.strftime("%H:%M") + ("" if t.date() == date else " (+1)")

    def _describe(self, legs, end, egress_w, dep, d, date, o: Place, de: Place, live=None) -> dict:
        g = self.g
        out_legs = []
        t = dep
        ride_now = ride_off = ride_p85 = wait = walk = ride_live = 0.0
        midnight = dt.datetime.combine(date, dt.time()).replace(tzinfo=dt.timezone.utc).timestamp() - C.IST_OFFSET_S
        confs = []
        for leg in legs:
            if leg[0] == "access":
                s, w = leg[1], leg[2]
                out_legs.append({"mode": "walk", "from": o.label, "to": g.stop_names[s], "minutes": round(w / 60, 1),
                                 "meters": round(w * WALK_MPS / C.WALK_DETOUR),
                                 "path": [[o.lat, o.lon], [float(g.stop_lat[s]), float(g.stop_lon[s])]]})
                t += w
                walk += w
            elif leg[0] == "walk":
                a, b, w = leg[1], leg[2], leg[3]
                out_legs.append({"mode": "walk", "from": g.stop_names[a], "to": g.stop_names[b], "minutes": round(w / 60, 1),
                                 "meters": round(w * WALK_MPS / C.WALK_DETOUR),
                                 "path": [[float(g.stop_lat[a]), float(g.stop_lon[a])], [float(g.stop_lat[b]), float(g.stop_lon[b])]]})
                t += w
                walk += w
            else:
                _, pid, row, bi, ai = leg
                p = g.patterns[pid]
                T = self.pat_times[pid][d]
                trip = int(self.pat_trips[pid][row])
                board_t, alight_t = T[row, bi], T[row, ai]
                w_s = max(0.0, board_t - t)
                r = alight_t - board_t
                # quiet-hour reference: the faster of the configured hour and 02:00, never slower than now
                off = min(self.ride_time(pid, bi, ai, h * 3600 + 1800, C.OFFPEAK_DAYTYPE)
                          for h in (C.OFFPEAK_HOUR, 2))
                off = min(off, alight_t - board_t)
                p85 = r + C.PLAN_BUFFER * (self.ride_time(pid, bi, ai, board_t, d, "p85") - r)   # see C.PLAN_BUFFER
                rinfo = g.routes.get(p.route_id, {})
                name = rinfo.get("route_short_name") or rinfo.get("route_long_name") or p.route_id
                stops = p.stops[bi:ai + 1]
                out_legs.append({
                    "mode": "bus", "route": name, "route_id": p.route_id, "trip_id": g.trip_ids[trip],
                    "headsign": g.trip_headsign[trip], "board": g.stop_names[stops[0]], "alight": g.stop_names[stops[-1]],
                    "board_stop_id": g.stop_ids[stops[0]], "alight_stop_id": g.stop_ids[stops[-1]],
                    "departs": self._clock(date, board_t), "arrives": self._clock(date, alight_t),
                    "scheduled_departure": self._clock(date, g.trip_sched[trip][bi]),
                    "wait_min": round(w_s / 60, 1), "ride_min": round(r / 60, 1), "ride_p85_min": round(p85 / 60, 1),
                    "timetable_ride_min": round((g.trip_sched[trip][ai] - g.trip_sched[trip][bi]) / 60, 1),
                    "stops": int(ai - bi), "color": "#" + (rinfo.get("route_color") or "1f6feb"),
                    "live_ride_min": (round(lr / 60, 1) if (lr := (live.ride_time(pid, bi, ai, midnight + board_t, date.isoformat(), d, -1)
                                                              if live else None)) is not None else None),
                    "path": self.leg_path(stops), "stop_points": [[float(g.stop_lat[s]), float(g.stop_lon[s])] for s in stops],
                    "_pid": pid})
                confs.append((r, self.ride_confidence(pid, bi, ai, d, board_t)))
                t = alight_t
                wait += w_s
                ride_now += r
                ride_off += off
                ride_p85 += p85
                ride_live += (out_legs[-1]["live_ride_min"] * 60) if live else r
        out_legs.append({"mode": "walk", "from": g.stop_names[end], "to": de.label, "minutes": round(egress_w / 60, 1),
                         "meters": round(egress_w * WALK_MPS / C.WALK_DETOUR),
                         "path": [[float(g.stop_lat[end]), float(g.stop_lon[end])], [de.lat, de.lon]]})
        walk += egress_w
        t += egress_w
        total = t - dep
        delay = ride_now - ride_off
        conf = sum(r * c for r, c in confs) / max(1e-9, sum(r for r, _ in confs))
        return {"total_minutes": round(total / 60, 1), "arrival": self._clock(date, t),
                "plan_for_minutes": round((total - ride_now + ride_p85) / 60, 1),
                "transfers": sum(1 for l in out_legs if l["mode"] == "bus") - 1,
                "walk_minutes": round(walk / 60, 1), "wait_minutes": round(wait / 60, 1),
                "ride_minutes": round(ride_now / 60, 1),
                "offpeak_minutes": round((total - delay) / 60, 1), "traffic_delay_minutes": round(delay / 60, 1),
                "confidence": round(conf, 2),
                "live_total_minutes": round((total - ride_now + ride_live) / 60, 1) if live else None,
                "source": (("historical + live GTFS-RT data of " if ("live", date.isoformat()) in self._live
                            else "historical + replayed live data of ") + date.isoformat()) if live else "historical model",
                "legs": out_legs}
