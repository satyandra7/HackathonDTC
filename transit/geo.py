"""Small geometry helpers. Delhi spans < 70 km, so a local equirectangular projection is accurate enough."""

from __future__ import annotations

import numpy as np

LAT0, LON0 = 28.62, 77.20
_KX = 111_320.0 * np.cos(np.radians(LAT0))
_KY = 110_540.0


def to_xy(lat, lon):
    """Degrees -> metres in a local plane centred on Delhi."""
    return (np.asarray(lon, dtype=np.float64) - LON0) * _KX, (np.asarray(lat, dtype=np.float64) - LAT0) * _KY


def haversine_m(lat1, lon1, lat2, lon2):
    lat1, lon1, lat2, lon2 = map(np.radians, (lat1, lon1, lat2, lon2))
    a = np.sin((lat2 - lat1) / 2) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin((lon2 - lon1) / 2) ** 2
    return 2 * 6_371_008.8 * np.arcsin(np.sqrt(a))


def from_xy(x, y):
    """Inverse of to_xy: metres in the local plane -> (lat, lon) degrees."""
    return np.asarray(y, dtype=np.float64) / _KY + LAT0, np.asarray(x, dtype=np.float64) / _KX + LON0
