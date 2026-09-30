"""Geodesic primitives.

All distances are returned in nautical miles (NM) unless stated otherwise.
1 NM = 1852 m exactly (SI definition).
"""
from __future__ import annotations

import math
from typing import Iterable, Sequence, Tuple

import numpy as np

EARTH_RADIUS_M = 6_371_008.8  # IUGG mean radius
M_PER_NM = 1852.0


def haversine_m(lat1, lon1, lat2, lon2):
    """Great-circle distance in metres. Vectorised over numpy arrays."""
    lat1, lon1, lat2, lon2 = map(np.asarray, (lat1, lon1, lat2, lon2))
    p1, p2 = np.radians(lat1), np.radians(lat2)
    dp = p2 - p1
    dl = np.radians(lon2 - lon1)
    a = np.sin(dp / 2.0) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dl / 2.0) ** 2
    return 2.0 * EARTH_RADIUS_M * np.arcsin(np.sqrt(np.clip(a, 0.0, 1.0)))


def haversine_nm(lat1, lon1, lat2, lon2):
    return haversine_m(lat1, lon1, lat2, lon2) / M_PER_NM


def initial_bearing_deg(lat1, lon1, lat2, lon2):
    """Forward azimuth from point 1 to point 2, degrees true (0..360)."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def angular_difference_deg(a: float, b: float) -> float:
    """Smallest absolute difference between two bearings, in [0, 180]."""
    return abs((a - b + 180.0) % 360.0 - 180.0)


def path_length_nm(coords: Sequence[Tuple[float, float]]) -> float:
    """Total great-circle length of a (lat, lon) polyline, in NM."""
    if len(coords) < 2:
        return 0.0
    arr = np.asarray(coords, dtype=float)
    return float(
        np.sum(haversine_nm(arr[:-1, 0], arr[:-1, 1], arr[1:, 0], arr[1:, 1]))
    )


def densify(coords: Sequence[Tuple[float, float]], max_step_nm: float = 0.5):
    """Insert intermediate points so no segment exceeds max_step_nm."""
    out = []
    for i in range(len(coords) - 1):
        (a_lat, a_lon), (b_lat, b_lon) = coords[i], coords[i + 1]
        d = haversine_nm(a_lat, a_lon, b_lat, b_lon)
        n = max(1, int(math.ceil(d / max_step_nm)))
        for k in range(n):
            t = k / n
            out.append((a_lat + (b_lat - a_lat) * t, a_lon + (b_lon - a_lon) * t))
    out.append(tuple(coords[-1]))
    return out


def douglas_peucker(coords: Sequence[Tuple[float, float]], tol_deg: float = 1e-4):
    """Simplify a (lat, lon) polyline. Plain planar DP; tolerance in degrees.

    Adequate at this latitude/extent where 1 deg lon ~= 1 deg lat * cos(1.2 deg).
    """
    coords = list(coords)
    if len(coords) < 3:
        return coords

    def _dp(pts):
        if len(pts) < 3:
            return pts
        (x0, y0), (x1, y1) = pts[0], pts[-1]
        dx, dy = x1 - x0, y1 - y0
        den = math.hypot(dx, dy)
        best_i, best_d = 0, -1.0
        for i in range(1, len(pts) - 1):
            x, y = pts[i]
            if den == 0:
                d = math.hypot(x - x0, y - y0)
            else:
                d = abs(dy * x - dx * y + x1 * y0 - y1 * x0) / den
            if d > best_d:
                best_i, best_d = i, d
        if best_d <= tol_deg:
            return [pts[0], pts[-1]]
        return _dp(pts[: best_i + 1])[:-1] + _dp(pts[best_i:])

    return _dp(coords)


def turn_angles_deg(coords: Sequence[Tuple[float, float]]):
    """Course change at each interior vertex of a (lat, lon) polyline."""
    out = []
    for i in range(1, len(coords) - 1):
        b_in = initial_bearing_deg(*coords[i - 1], *coords[i])
        b_out = initial_bearing_deg(*coords[i], *coords[i + 1])
        out.append(angular_difference_deg(b_in, b_out))
    return out
