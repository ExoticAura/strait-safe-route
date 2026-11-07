"""Payload builders for the chart: GeoJSON and packed rasters."""
from __future__ import annotations

import base64
import math
from typing import Dict, List, Sequence, Tuple

import numpy as np

from . import study_area as SA
from .geo import initial_bearing_deg
from .seagrid import LAND_GEOJSON, NM_PER_DEG, SeaGrid, load_land_polygons


def land_geojson() -> Dict:
    from . import mpa
    path = mpa.land_geojson(LAND_GEOJSON) if mpa.enabled() else LAND_GEOJSON
    feats = []
    for rings in load_land_polygons(path):
        feats.append({"type": "Feature", "properties": {"kind": "land"},
                      "geometry": {"type": "Polygon",
                                   "coordinates": [[[round(float(x), 5), round(float(y), 5)]
                                                    for x, y in r] for r in rings]}})
    return {"type": "FeatureCollection", "features": feats}


def offset_centreline(side: str, offset_nm: float) -> List[Tuple[float, float]]:
    cl = SA.TSS_CENTRELINE
    out = []
    for i, (lat, lon) in enumerate(cl):
        j0, j1 = max(i - 1, 0), min(i + 1, len(cl) - 1)
        brg = math.radians(initial_bearing_deg(cl[j0][0], cl[j0][1], cl[j1][0], cl[j1][1]))
        nx, ny = -math.cos(brg), math.sin(brg)
        sgn = 1.0 if side == "N" else -1.0
        out.append((lat + sgn * offset_nm * ny / NM_PER_DEG,
                    lon + sgn * offset_nm * nx / (NM_PER_DEG * math.cos(math.radians(lat)))))
    return out


def _ls(coords, props):
    return {"type": "Feature", "properties": props,
            "geometry": {"type": "LineString",
                         "coordinates": [[round(lo, 5), round(la, 5)] for la, lo in coords]}}


def tss_geojson() -> Dict:
    sep, lane = SA.TSS_SEPARATION_HALF_WIDTH_NM, SA.TSS_LANE_WIDTH_NM
    feats = [_ls(SA.TSS_CENTRELINE, {"kind": "tss_axis"})]
    for side, name in (("N", "westbound"), ("S", "eastbound")):
        inner = offset_centreline(side, sep)
        outer = offset_centreline(side, sep + lane)
        feats.append(_ls(inner + outer[::-1] + [inner[0]],
                         {"kind": "tss_lane", "direction": name}))
    return {"type": "FeatureCollection", "features": feats}


def pack_int8(a: np.ndarray, grid: SeaGrid, stride: int = 1) -> Dict:
    a = np.asarray(a)[::stride, ::stride].astype(np.uint8)
    return dict(lat0=float(grid.lats[0]), lon0=float(grid.lons[0]),
                dlat=float(grid.resolution_deg * stride), dlon=float(grid.resolution_deg * stride),
                ny=int(a.shape[0]), nx=int(a.shape[1]),
                b64=base64.b64encode(a.tobytes()).decode("ascii"))


def depth_payload(grid: SeaGrid, stride: int = 1) -> Dict:
    """Depth quantised to 0.25 m steps (0..63.5 m), 255 = land."""
    d = np.clip(np.round(grid.depth_m / 0.25), 0, 254).astype(np.uint8)
    d[grid.land] = 255
    p = pack_int8(d, grid, stride)
    p["scale"] = 0.25
    return p


def round_coords(coords, nd=5):
    return [[round(float(a), nd), round(float(b), nd)] for a, b in coords]
