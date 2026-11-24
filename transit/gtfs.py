"""Static GTFS loader following the GTFS Schedule reference.

Handles: text IDs; times past 24:00:00 (stored as seconds since service-day midnight); blank
intermediate stop times (interpolated by distance); calendar + calendar_dates (either may be
absent); frequencies.txt (expanded into individual trips); pickup_type / drop_off_type = 1;
location_type (only 0/empty stops are boardable). shapes.txt is not needed: route geometry is the
stop sequence.

Trips with identical stop sequences form a *pattern* (a RAPTOR route).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import polars as pl

from . import geo


def _read(path: Path, required: bool = True) -> pl.DataFrame | None:
    if not path.exists():
        if required:
            raise FileNotFoundError(f"GTFS file missing: {path}")
        return None
    df = pl.read_csv(path, infer_schema_length=0, encoding="utf8-lossy", truncate_ragged_lines=True)
    df = df.rename({c: c.strip().lstrip("﻿").strip('"') for c in df.columns})
    return df.with_columns(pl.col(c).str.strip_chars() for c in df.columns)


def parse_time(s: str | None) -> int:
    """'H:MM:SS' or 'HH:MM:SS' (may exceed 24) -> seconds; blank -> -1."""
    if s is None or s == "":
        return -1
    h, m, sec = s.split(":")
    return int(h) * 3600 + int(m) * 60 + int(sec)


def _time_col(c: str) -> pl.Expr:
    p = pl.col(c).str.split(":")
    return (pl.when(pl.col(c).is_null() | (pl.col(c) == ""))
            .then(-1)
            .otherwise(p.list.get(0).cast(pl.Int32) * 3600 + p.list.get(1).cast(pl.Int32) * 60
                       + p.list.get(2).cast(pl.Int32))
            .cast(pl.Int32))


@dataclass
class Pattern:
    pid: int
    route_id: str
    stops: np.ndarray            # stop indices, in order
    cumdist: np.ndarray          # metres from first stop along the stop polyline
    trips: list[int] = field(default_factory=list)   # trip indices, sorted by start time


class GTFS:
    def __init__(self, folder: Path):
        self.folder = Path(folder)
        f = self.folder

        # ---- agency / feed info
        agency = _read(f / "agency.txt")
        self.timezone = agency["agency_timezone"][0] if "agency_timezone" in agency.columns else "Asia/Kolkata"
        fi = _read(f / "feed_info.txt", required=False)
        self.feed_info = fi.row(0, named=True) if fi is not None and fi.height else {}

        # ---- stops
        st = _read(f / "stops.txt")
        if "location_type" not in st.columns:
            st = st.with_columns(pl.lit("").alias("location_type"))
        st = st.filter(pl.col("stop_lat").is_not_null() & (pl.col("stop_lat") != ""))
        self.stop_ids: list[str] = st["stop_id"].to_list()
        self.stop_index = {s: i for i, s in enumerate(self.stop_ids)}
        self.stop_names: list[str] = st["stop_name"].fill_null("").to_list()
        self.stop_lat = st["stop_lat"].cast(pl.Float64).to_numpy()
        self.stop_lon = st["stop_lon"].cast(pl.Float64).to_numpy()
        self.stop_x, self.stop_y = geo.to_xy(self.stop_lat, self.stop_lon)
        lt = st["location_type"].fill_null("").to_list()
        self.stop_boardable = np.array([v in ("", "0") for v in lt])
        self.stop_code = st["stop_code"].fill_null("").to_list() if "stop_code" in st.columns else [""] * len(lt)

        # ---- routes
        rt = _read(f / "routes.txt")
        for c in ("route_short_name", "route_long_name", "route_color", "route_type"):
            if c not in rt.columns:
                rt = rt.with_columns(pl.lit("").alias(c))
        self.routes = {r["route_id"]: r for r in rt.iter_rows(named=True)}

        # ---- trips
        tr = _read(f / "trips.txt")
        for c in ("direction_id", "trip_headsign", "shape_id"):
            if c not in tr.columns:
                tr = tr.with_columns(pl.lit("").alias(c))

        # ---- stop_times
        stt = _read(f / "stop_times.txt")
        for c in ("pickup_type", "drop_off_type"):
            if c not in stt.columns:
                stt = stt.with_columns(pl.lit("").alias(c))
        stt = (stt.with_columns(_time_col("arrival_time").alias("arr"), _time_col("departure_time").alias("dep"),
                                pl.col("stop_sequence").cast(pl.Int64).alias("seq"))
               .filter(pl.col("stop_id").is_in(list(self.stop_index)))
               .sort(["trip_id", "seq"]))
        g = (stt.group_by("trip_id", maintain_order=True)
             .agg(pl.col("stop_id"), pl.col("arr"), pl.col("dep"), pl.col("pickup_type"), pl.col("drop_off_type")))
        tinfo = {r["trip_id"]: r for r in tr.iter_rows(named=True)}

        # ---- frequencies: expand template trips into one trip per departure
        freq = _read(f / "frequencies.txt", required=False)
        freq_rows: dict[str, list[tuple[int, int, int]]] = {}
        if freq is not None:
            for r in freq.iter_rows(named=True):
                freq_rows.setdefault(r["trip_id"], []).append(
                    (parse_time(r["start_time"]), parse_time(r["end_time"]), int(r["headway_secs"])))

        self.trip_ids: list[str] = []
        self.trip_route: list[str] = []
        self.trip_service: list[str] = []
        self.trip_direction: list[str] = []
        self.trip_headsign: list[str] = []
        self.trip_pattern: list[int] = []
        self.trip_sched: list[np.ndarray] = []      # scheduled time at each pattern stop (s since service midnight)
        self.trip_no_pickup: list[np.ndarray | None] = []
        self.trip_no_dropoff: list[np.ndarray | None] = []
        self.patterns: list[Pattern] = []
        pat_index: dict[tuple, int] = {}

        for row in g.iter_rows(named=True):
            info = tinfo.get(row["trip_id"])
            if info is None or len(row["stop_id"]) < 2:
                continue
            key = (info["route_id"], tuple(row["stop_id"]))
            pid = pat_index.get(key)
            if pid is None:
                sidx = np.array([self.stop_index[s] for s in row["stop_id"]], dtype=np.int32)
                d = np.hypot(np.diff(self.stop_x[sidx]), np.diff(self.stop_y[sidx]))
                pid = len(self.patterns)
                pat_index[key] = pid
                self.patterns.append(Pattern(pid, info["route_id"], sidx, np.concatenate([[0.0], np.cumsum(d)])))
            pat = self.patterns[pid]
            times = self._fill_times(np.array(row["dep"], dtype=np.float64), np.array(row["arr"], dtype=np.float64),
                                     pat.cumdist)
            if times is None:
                continue
            nopick = np.array([v == "1" for v in row["pickup_type"]])
            nodrop = np.array([v == "1" for v in row["drop_off_type"]])
            starts = [0]
            if row["trip_id"] in freq_rows:      # frequency-based: one copy per headway
                starts = [s - times[0] for a, b, h in freq_rows[row["trip_id"]] for s in range(a, b, h)]
            for k, off in enumerate(starts):
                self.trip_ids.append(row["trip_id"] if len(starts) == 1 else f"{row['trip_id']}@{k}")
                self.trip_route.append(info["route_id"])
                self.trip_service.append(info["service_id"])
                self.trip_direction.append(info["direction_id"] or "")
                self.trip_headsign.append(info["trip_headsign"] or "")
                self.trip_pattern.append(pid)
                self.trip_sched.append(times + off)
                self.trip_no_pickup.append(nopick if nopick.any() else None)
                self.trip_no_dropoff.append(nodrop if nodrop.any() else None)
                pat.trips.append(len(self.trip_ids) - 1)

        self.trip_start = np.array([t[0] for t in self.trip_sched], dtype=np.int32)
        for p in self.patterns:
            p.trips.sort(key=lambda t: self.trip_start[t])
        self.trips_by_route: dict[str, np.ndarray] = {}
        for t, r in enumerate(self.trip_route):
            self.trips_by_route.setdefault(r, []).append(t)
        self.trips_by_route = {r: np.array(v, dtype=np.int32) for r, v in self.trips_by_route.items()}
        self.trip_service_arr = np.array(self.trip_service, dtype=object)

        # ---- calendar
        self.calendar = _read(f / "calendar.txt", required=False)
        self.calendar_dates = _read(f / "calendar_dates.txt", required=False)
        if self.calendar is None and self.calendar_dates is None:
            raise FileNotFoundError("GTFS has neither calendar.txt nor calendar_dates.txt")
        self._svc_cache: dict[str, tuple[set[str], str]] = {}

    # ------------------------------------------------------------------------------------------
    @staticmethod
    def _fill_times(dep: np.ndarray, arr: np.ndarray, cumdist: np.ndarray) -> np.ndarray | None:
        """Departure time at each stop; blanks (-1) are interpolated by distance along the pattern."""
        t = np.where(dep >= 0, dep, arr)
        known = t >= 0
        if known.sum() < 2:
            return None
        if not known.all():
            t = np.interp(cumdist, cumdist[known], t[known])
        return np.maximum.accumulate(t).astype(np.int32)

    def _services_exact(self, date: dt.date) -> set[str]:
        ymd = date.strftime("%Y%m%d")
        wd = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"][date.weekday()]
        active: set[str] = set()
        if self.calendar is not None:
            c = self.calendar
            active = set(c.filter((pl.col(wd) == "1") & (pl.col("start_date") <= ymd) & (pl.col("end_date") >= ymd))
                         ["service_id"].to_list())
        if self.calendar_dates is not None:
            cd = self.calendar_dates.filter(pl.col("date") == ymd)
            active |= set(cd.filter(pl.col("exception_type") == "1")["service_id"].to_list())
            active -= set(cd.filter(pl.col("exception_type") == "2")["service_id"].to_list())
        return active

    def services_on(self, date_str: str) -> tuple[set[str], str]:
        """Active service_ids for a date. If the feed doesn't cover it, use the nearest covered date
        with the same weekday. Returns (service_ids, date actually used)."""
        if date_str in self._svc_cache:
            return self._svc_cache[date_str]
        d = dt.date.fromisoformat(date_str)
        s = self._services_exact(d)
        used = date_str
        if not s:
            # nearest date with the same weekday inside the feed's calendar range, searched outwards
            lo, hi = self.calendar_range()
            if lo and hi:
                anchor = min(max(d, lo), hi)
                anchor += dt.timedelta(days=(d.weekday() - anchor.weekday()) % 7)
                if anchor > hi:
                    anchor -= dt.timedelta(weeks=1)
                for k in range(0, 60):
                    for cand in (anchor - dt.timedelta(weeks=k), anchor + dt.timedelta(weeks=k)):
                        if lo <= cand <= hi:
                            s = self._services_exact(cand)
                            if s:
                                used = cand.isoformat()
                                break
                    if s:
                        break
        self._svc_cache[date_str] = (s, used)
        return s, used

    def calendar_range(self) -> tuple[dt.date | None, dt.date | None]:
        ds = []
        if self.calendar is not None and self.calendar.height:
            ds += [min(self.calendar["start_date"].to_list()), max(self.calendar["end_date"].to_list())]
        if self.calendar_dates is not None and self.calendar_dates.height:
            ds += [min(self.calendar_dates["date"].to_list()), max(self.calendar_dates["date"].to_list())]
        if not ds:
            return None, None
        p = lambda s: dt.date(int(s[:4]), int(s[4:6]), int(s[6:8]))  # noqa: E731
        return p(min(ds)), p(max(ds))

    def signature(self) -> str:
        """Identifies this feed, so derived files are not reused with another feed."""
        import hashlib
        h = hashlib.sha1()
        for name in ("routes.txt", "trips.txt", "stops.txt"):
            f = self.folder / name
            h.update(f"{name}:{f.stat().st_size}:{f.stat().st_mtime_ns}".encode())
        return h.hexdigest()[:16]

    def active_trip_mask(self, date_str: str) -> np.ndarray:
        s, _ = self.services_on(date_str)
        return np.isin(self.trip_service_arr, list(s))

    def summary(self) -> dict:
        return {"stops": len(self.stop_ids), "routes": len(self.routes), "trips": len(self.trip_ids),
                "patterns": len(self.patterns), "timezone": self.timezone,
                "has_calendar": self.calendar is not None, "has_calendar_dates": self.calendar_dates is not None,
                "max_time_s": int(max((t[-1] for t in self.trip_sched), default=0))}
