"""GTFS-RT VehiclePositions: fetch, decode and clean into the same ping format as stage 1.

Access (dataset 7 on delhi.transportstack.in, "Instructions to Access Delhi Buses - GTFS-RT feed"): GET FEED_URL with
the API key in the `x-api-key` header; the key arrives by email after subscribing. It is read from ~/.otd_key
(or $OTD_KEY). A different URL can be set in $OTD_RT_URL or ~/.otd_rt_url; if it contains {key}, the key goes
into the URL instead (the organisers' reply mentioned that form). The key is never written to logs, reports or
the repo (`redact` strips it from error messages).

Speeds in the feed are not used (organisers' answer 5): every speed is computed from position and time.
"""

from __future__ import annotations

import os
import urllib.request
from pathlib import Path

import numpy as np
import polars as pl

LAT_MIN, LAT_MAX = 28.35, 28.95          # same geofence as stage 1
LON_MIN, LON_MAX = 76.80, 77.55
MAX_KMH = 90.0                           # stage 1 teleport rule
FIELDS = ["vehicle_id", "trip_id", "route_id", "ts_utc", "lat", "lon"]


def _secret(name: str) -> str:
    return (Path.home() / name).read_text().strip()


FEED_URL = ("https://dts-backend.transportstack.in/api/dataset/otd/get-file"   # endpoint from the approval email
            "?agency=delhi-buses&category=realtime_gtfs&filename=VehiclePositions.pb")


def feed_request() -> tuple[str, dict]:
    """(url, headers) for the live feed."""
    key = os.environ.get("OTD_KEY") or _secret(".otd_key")
    try:
        tpl = os.environ.get("OTD_RT_URL") or _secret(".otd_rt_url")
    except OSError:
        tpl = FEED_URL
    if "{key}" in tpl:
        return tpl.replace("{key}", key), {}
    return tpl, {"x-api-key": key}


def redact(text: str) -> str:
    try:
        key = os.environ.get("OTD_KEY") or _secret(".otd_key")
    except OSError:
        return text
    return text.replace(key, "***") if key else text


BACKEND = ("https://delhi.transportstack.in/", "https://dts-backend.transportstack.in/")


def fetch(url: str, headers: dict | None = None, timeout: float = 20.0) -> bytes:
    """GET the feed. The documented URL (on the website host) can return the site's HTML page instead of the
    feed; the same path on the backend host (which answers 401 without a key) is then tried."""
    h = {"User-Agent": "dtc-journey-planner", **(headers or {})}
    for u in (url, url.replace(*BACKEND)) if url.startswith(BACKEND[0]) else (url,):
        with urllib.request.urlopen(urllib.request.Request(u, headers=h), timeout=timeout) as r:
            blob = r.read()
        if not blob.lstrip()[:15].lower().startswith((b"<!doctype", b"<html")):
            return blob
    raise ValueError("feed returned an HTML page, not protobuf")


SCHEMA = {"vehicle_id": pl.Utf8, "trip_id": pl.Utf8, "route_id": pl.Utf8, "ts_utc": pl.Int64, "lat": pl.Float64,
          "lon": pl.Float64}


def decode(blob: bytes) -> tuple[pl.DataFrame, dict]:
    """VehiclePositions -> DataFrame (vehicle_id, trip_id, route_id, ts_utc, lat, lon) and field fill counts."""
    from google.transit import gtfs_realtime_pb2
    msg = gtfs_realtime_pb2.FeedMessage()
    msg.ParseFromString(blob)
    rows, fill = [], {}
    keys = ["vehicle.id", "vehicle.label", "trip.trip_id", "trip.route_id", "trip.direction_id", "trip.start_time",
            "trip.start_date", "position", "position.speed", "position.bearing", "timestamp", "stop_id",
            "current_stop_sequence", "current_status", "congestion_level", "occupancy_status"]
    fill = dict.fromkeys(keys, 0)
    for ent in msg.entity:
        if not ent.HasField("vehicle"):
            continue
        v = ent.vehicle
        has = {"vehicle.id": v.vehicle.id != "", "vehicle.label": v.vehicle.label != "",
               "trip.trip_id": v.trip.trip_id != "", "trip.route_id": v.trip.route_id != "",
               "trip.direction_id": v.trip.HasField("direction_id"), "trip.start_time": v.trip.start_time != "",
               "trip.start_date": v.trip.start_date != "", "position": v.HasField("position"),
               "position.speed": v.position.HasField("speed"), "position.bearing": v.position.HasField("bearing"),
               "timestamp": v.HasField("timestamp"), "stop_id": v.stop_id != "",
               "current_stop_sequence": v.HasField("current_stop_sequence"), "current_status": v.HasField("current_status"),
               "congestion_level": v.HasField("congestion_level"), "occupancy_status": v.HasField("occupancy_status")}
        for k, b in has.items():
            fill[k] += int(b)
        if not has["position"]:
            continue
        rid = v.trip.route_id or (v.trip.trip_id.split("_")[0] if v.trip.trip_id else "")   # answer 4: prefix = route_id
        rows.append((v.vehicle.id or v.vehicle.label or ent.id, v.trip.trip_id or None, rid or None,
                     int(v.timestamp) if has["timestamp"] else int(msg.header.timestamp), float(v.position.latitude),
                     float(v.position.longitude)))
    fill["entities"] = len(msg.entity)
    fill["header_timestamp"] = int(msg.header.timestamp)
    return pl.DataFrame(rows, schema=SCHEMA, orient="row"), fill


def encode(df: pl.DataFrame, header_ts: int) -> bytes:
    """DataFrame -> VehiclePositions FeedMessage (used by the simulator)."""
    from google.transit import gtfs_realtime_pb2
    msg = gtfs_realtime_pb2.FeedMessage()
    msg.header.gtfs_realtime_version = "2.0"
    msg.header.timestamp = int(header_ts)
    for i, (veh, trip, route, ts, lat, lon) in enumerate(df.select(FIELDS).iter_rows()):
        e = msg.entity.add()
        e.id = str(i)
        v = e.vehicle
        v.vehicle.id = str(veh)
        if trip:
            v.trip.trip_id = str(trip)
        if route is not None:
            v.trip.route_id = str(route)
        v.position.latitude, v.position.longitude = float(lat), float(lon)
        v.timestamp = int(ts)
    return msg.SerializeToString()


def clean_window(df: pl.DataFrame, now_utc: float, window_s: float) -> pl.DataFrame:
    """Stage 1 rules on a rolling window: geofence, dedup, time sanity, teleport filter, stationary compression.
    Returns the stage 1 ping schema that DayRuns reads (vehicle_id, trip_id, route_id, ts_utc, end_ts_utc, lat, lon)."""
    d = (df.filter((pl.col("ts_utc") > now_utc - window_s) & (pl.col("ts_utc") <= now_utc + 60)
                   & pl.col("lat").is_between(LAT_MIN, LAT_MAX) & pl.col("lon").is_between(LON_MIN, LON_MAX)
                   & pl.col("vehicle_id").is_not_null())
         .unique(["vehicle_id", "ts_utc"], keep="first").sort(["vehicle_id", "ts_utc"]))
    if d.height == 0:
        return d.with_columns(pl.col("ts_utc").alias("end_ts_utc"), pl.col("route_id").cast(pl.Int64, strict=False))
    # teleport: drop a ping implying > MAX_KMH from the previous ping of the same vehicle
    lat, lon = np.radians(d["lat"].to_numpy()), np.radians(d["lon"].to_numpy())
    ts = d["ts_utc"].to_numpy().astype(np.float64)
    veh = d["vehicle_id"].to_numpy()
    same = np.r_[False, veh[1:] == veh[:-1]]
    a = np.sin(np.r_[0, np.diff(lat)] / 2) ** 2 + np.cos(lat) * np.cos(np.r_[lat[0], lat[:-1]]) * np.sin(np.r_[0, np.diff(lon)] / 2) ** 2
    dist = 2 * 6371008.8 * np.arcsin(np.sqrt(np.clip(a, 0, 1)))
    kmh = dist / np.maximum(np.r_[1.0, np.diff(ts)], 1.0) * 3.6
    d = d.filter(pl.Series(~(same & (kmh > MAX_KMH))))
    # stationary compression: consecutive identical positions -> one record with a start and end time
    new = ((pl.col("vehicle_id") != pl.col("vehicle_id").shift()) | (pl.col("lat") != pl.col("lat").shift())
           | (pl.col("lon") != pl.col("lon").shift())).fill_null(True)
    return (d.with_columns(new.cum_sum().alias("_g"))
             .group_by("_g", maintain_order=True)
             .agg(pl.col("vehicle_id").first(), pl.col("trip_id").first(), pl.col("route_id").first(),
                  pl.col("ts_utc").min(), pl.col("ts_utc").max().alias("end_ts_utc"), pl.col("lat").first(),
                  pl.col("lon").first())
             .drop("_g").with_columns(pl.col("route_id").cast(pl.Int64, strict=False)))
