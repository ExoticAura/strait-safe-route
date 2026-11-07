"""MPA GeoSpace-Sea chart layers: charted depths, hazards, berths, fairways.

The layers are IHO S-57 extracts of MPA's harbour-scale charts (usage band 5,
cells SG5C4030-SG5C4044, mostly dated Aug 2025), downloaded once with
scripts/download_gss.py into data/gss/<layer>/<Layer>.geojson. They are NOT
for navigation and their Terms of Use have not been checked, so nothing in
data/gss/ (or derived from it) is committed: everything here is optional and
the model falls back to GEBCO + the OpenStreetMap coastline without it.

How the board uses them (seagrid.build_seagrid):

* Land. In S-57 every bit of charted water is covered by a depth area
  (DEPARE) or a dredged area (DRGARE): DepthsA holds both. So inside the
  charted area, water = the union of DepthsA, and the holes in that union are
  land (islands, reclaimed land, pontoons, unsurveyed patches - all
  unnavigable). The coastline polygon is corrected with both: charted water
  removes OSM land, charted holes add land.
* Depth. Each charted area counts at its SHALLOWEST guaranteed depth:
  DRVAL1, the shallow end of the band (10 of "10-15 m"), for depth areas, the
  maintained depth for dredged areas, 0 for drying areas (negative DRVAL1).
  A board cell takes the depth over most of it (the lower median of 6 x 6
  sample points, CELL_DEPTH); SAFEROUTE_MPA_CELL_DEPTH=min makes any
  shallow corner close the cell. Charted cells use this instead of GEBCO and the
  dredged-pocket rule; uncharted cells keep GEBCO.
* Hazards (DangersP/L/A): a wreck, rock or obstruction with a known least
  depth (VALSOU) caps the depth of every cell it touches; one that is awash,
  dries or is a dangerous wreck of unknown depth makes it 0 m. Foul ground of
  unknown depth is shown on the chart but not routed round.

Vertical datum: charted depths are below chart datum (about the lowest
astronomical tide), GEBCO below mean sea level; neither adds tide. Charted
depth is therefore the cautious, low-water figure.
"""
from __future__ import annotations

import json
import math
import os
from functools import lru_cache
from typing import Dict, List, Optional, Tuple

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
GSS_DIR = os.environ.get("SAFEROUTE_GSS_DIR", os.path.join(ROOT, "data", "gss"))
DERIVED_DIR = os.path.join(GSS_DIR, "derived")
LAND_MPA = os.path.join(DERIVED_DIR, "land_mpa.geojson")

LAYER_FILES = {
    "depthsa": "DepthsA", "depthsl": "DepthsL",
    "dangersa": "DangersA", "dangersl": "DangersL", "dangersp": "DangersP",
    "tracksandroutesa": "TracksAndRoutesA", "tracksandroutesl": "TracksAndRoutesL",
    "tracksandroutesp": "TracksAndRoutesP",
    "portsandservicesp": "PortsAndServicesP", "portsandservicesa": "PortsAndServicesA",
    "regulatedareasandlimitsa": "RegulatedAreasAndLimitsA",
    "coastlinea": "CoastlineA", "coastlinel": "CoastlineL",
}
DRGARE = 5            # DepthsA FCSubtype of a dredged area (DEPARE is 1)

# Berths of each container terminal: the MPA berth names start with these.
TERMINAL_BERTHS = {
    "SGTUA": ("Tuas F2 Terminal Berth ",),
    "SGJUR": ("Jurong Terminal (Jurong Port Pte Ltd) Berth ",),
    "SGPPT": ("Pasir Panjang Terminal (PSA Singapore Terminals) Berth ",),
    "SGKEP": ("Keppel Terminal Berth ", "Brani Terminal Berth "),
}


def enabled() -> bool:
    """True when the MPA layers are on disk and not switched off (SAFEROUTE_MPA=0)."""
    if os.environ.get("SAFEROUTE_MPA", "1").strip().lower() in ("0", "false", "no", "off"):
        return False
    return os.path.exists(_path("depthsa"))


def _path(layer: str) -> str:
    return os.path.join(GSS_DIR, layer, LAYER_FILES[layer] + ".geojson")


@lru_cache(maxsize=None)
def features(layer: str) -> Tuple[dict, ...]:
    """The layer's GeoJSON features (empty if the file is missing)."""
    p = _path(layer)
    if not os.path.exists(p):
        return ()
    with open(p) as fh:
        return tuple(f for f in json.load(fh)["features"] if f.get("geometry"))


def source_date() -> str:
    """Most common source date (SORDAT) of the depth layer, e.g. '2025-08-14'."""
    from collections import Counter
    c = Counter(f["properties"].get("SORDAT") for f in features("depthsa"))
    d = (c.most_common(1)[0][0] or "") if c else ""
    return f"{d[:4]}-{d[4:6]}-{d[6:8]}" if len(d) == 8 else d


# --------------------------------------------------------------------------
# water, land and depth
# --------------------------------------------------------------------------
def area_depth(props: dict) -> float:
    """Depth (m, >= 0) a ship can count on anywhere in a depth / dredged area."""
    d1 = props.get("DRVAL1")
    if d1 is None:
        return 0.0
    return max(float(d1), 0.0)


@lru_cache(maxsize=1)
def water():
    """Union of all charted depth and dredged areas (shapely geometry)."""
    from shapely.geometry import shape
    from shapely.ops import unary_union
    return unary_union([shape(f["geometry"]).buffer(0) for f in features("depthsa")])


@lru_cache(maxsize=1)
def charted_land():
    """Holes in the charted water: land, pontoons and unsurveyed patches."""
    from shapely.geometry import Polygon
    from shapely.ops import unary_union
    w = water()
    parts = list(getattr(w, "geoms", [w]))
    return unary_union([Polygon(h) for g in parts for h in g.interiors])


def corrected_land(osm_land):
    """The coastline polygon corrected by the chart (see the module notes)."""
    out = osm_land.difference(water()).union(charted_land())
    return out.buffer(0)


def land_geojson(base_path: str) -> str:
    """Path of the chart-corrected land GeoJSON, built from `base_path` on
    first use and rebuilt when either input is newer."""
    if os.path.exists(LAND_MPA) and os.path.getmtime(LAND_MPA) >= max(
            os.path.getmtime(base_path), os.path.getmtime(_path("depthsa"))):
        return LAND_MPA
    from shapely.geometry import mapping, shape
    with open(base_path) as fh:
        gj = json.load(fh)
    feats = gj["features"] if gj.get("type") == "FeatureCollection" else [gj]
    from shapely.ops import unary_union
    base = unary_union([shape(f["geometry"] if "geometry" in f else f) for f in feats])
    land = corrected_land(base)
    os.makedirs(DERIVED_DIR, exist_ok=True)
    tmp = LAND_MPA + ".tmp"
    with open(tmp, "w") as fh:
        json.dump({"type": "FeatureCollection", "features": [
            {"type": "Feature", "properties": {
                "source": "OpenStreetMap coastline corrected by MPA GeoSpace-Sea DepthsA "
                          f"(charted water / holes), source date {source_date()}"},
             "geometry": mapping(land)}]}, fh)
    os.replace(tmp, LAND_MPA)
    return LAND_MPA


def _cell_window(bounds, lats, lons, res):
    minx, miny, maxx, maxy = bounds
    ix0 = max(int(math.floor((minx - lons[0]) / res)) - 1, 0)
    ix1 = min(int(math.ceil((maxx - lons[0]) / res)) + 2, len(lons))
    iy0 = max(int(math.floor((miny - lats[0]) / res)) - 1, 0)
    iy1 = min(int(math.ceil((maxy - lats[0]) / res)) + 2, len(lats))
    return iy0, iy1, ix0, ix1


# How a board cell's charted depth is read from its supersample points:
# "median" (the depth over most of the cell: a ~220 m cell is 7 Panamax
# beams wide, so a ship can use the deeper part) or "min" (any shallow corner
# closes the whole cell). Point hazards are always applied as a minimum.
CELL_DEPTH = os.environ.get("SAFEROUTE_MPA_CELL_DEPTH", "median")


def depth_raster(lats: np.ndarray, lons: np.ndarray, supersample: int = 3,
                 how: Optional[str] = None) -> Tuple[np.ndarray, np.ndarray]:
    """(charted, depth): cells with any charted sample point, and their charted
    depth from the supersample x supersample points (CELL_DEPTH; uncharted
    points are ignored)."""
    from shapely import contains_xy
    from shapely.geometry import shape
    how = how or CELL_DEPTH
    res = float(lats[1] - lats[0]) if len(lats) > 1 else 0.002
    offs = (np.arange(supersample) + 0.5) / supersample - 0.5
    ny, nx = len(lats), len(lons)
    samples = np.full((len(offs) ** 2, ny, nx), np.inf)
    for f in features("depthsa"):
        g = shape(f["geometry"])
        iy0, iy1, ix0, ix1 = _cell_window(g.bounds, lats, lons, res)
        if iy1 <= iy0 or ix1 <= ix0:
            continue
        d = area_depth(f["properties"])
        k = 0
        for oy in offs:
            for ox in offs:
                LA, LO = np.meshgrid(lats[iy0:iy1] + oy * res, lons[ix0:ix1] + ox * res, indexing="ij")
                m = contains_xy(g, LO, LA)
                if m.any():
                    sub = samples[k, iy0:iy1, ix0:ix1]
                    np.minimum(sub, np.where(m, d, np.inf), out=sub)
                k += 1
    charted = np.isfinite(samples).any(axis=0)
    if how == "min":
        depth = np.where(charted, samples.min(axis=0), 0.0)
    else:
        s = np.where(np.isfinite(samples), samples, np.nan)
        with np.errstate(all="ignore"):
            # lower median: of an even count, the shallower of the middle two
            srt = np.sort(s, axis=0)                     # NaNs sort last
            cnt = np.isfinite(s).sum(axis=0)
            k = np.clip((cnt - 1) // 2, 0, s.shape[0] - 1)
            depth = np.take_along_axis(srt, k[None], axis=0)[0]
        depth = np.where(charted, depth, 0.0)
    return charted, depth


# --------------------------------------------------------------------------
# hazards
# --------------------------------------------------------------------------
_AWASH = ("awash", "covers and uncovers", "always dry", "partly submerged at high water")


def hazard_depth(props: dict) -> Optional[float]:
    """Least depth over a hazard for routing, or None if it is not routed round."""
    v = props.get("VALSOU")
    if v is not None:
        return max(float(v), 0.0)
    if (props.get("WATLEV_TXT") or "") in _AWASH:
        return 0.0
    if (props.get("CATWRK_TXT") or "") == "dangerous wreck":
        return 0.0
    return None


def hazard_kind(props: dict) -> str:
    for k in ("CATWRK_TXT", "CATOBS_TXT", "CATWAT_TXT", "CATFIF_TXT"):
        if props.get(k):
            return props[k]
    if props.get("WATLEV_TXT"):
        return "obstruction (" + props["WATLEV_TXT"] + ")"
    return "hazard"


def hazards() -> List[dict]:
    """All charted hazards: geometry (shapely), depth (None = shown only), kind."""
    from shapely.geometry import shape
    out = []
    for layer in ("dangersp", "dangersl", "dangersa"):
        for f in features(layer):
            p = f["properties"]
            out.append(dict(geom=shape(f["geometry"]), depth=hazard_depth(p), kind=hazard_kind(p),
                            name=p.get("OBJNAM") or "", inform=(p.get("INFORM") or "")[:200],
                            layer=layer))
    return out


def hazard_raster(lats: np.ndarray, lons: np.ndarray) -> np.ndarray:
    """Per cell the least depth over any routed hazard touching it (inf = none)."""
    from shapely import intersects
    from shapely.geometry import box
    res = float(lats[1] - lats[0])
    out = np.full((len(lats), len(lons)), np.inf)
    for h in hazards():
        if h["depth"] is None:
            continue
        g = h["geom"]
        iy0, iy1, ix0, ix1 = _cell_window(g.bounds, lats, lons, res)
        for iy in range(iy0, iy1):
            for ix in range(ix0, ix1):
                cell = box(lons[ix] - res / 2, lats[iy] - res / 2, lons[ix] + res / 2, lats[iy] + res / 2)
                if intersects(cell, g):
                    out[iy, ix] = min(out[iy, ix], h["depth"])
    return out


# --------------------------------------------------------------------------
# berths
# --------------------------------------------------------------------------
def berth_points(code: str) -> List[Tuple[str, float, float]]:
    """(short name, lat, lon) of each MPA berth of a container terminal."""
    from shapely.geometry import shape
    prefixes = TERMINAL_BERTHS.get(code, ())
    out = []
    for f in features("portsandservicesp"):
        name = f["properties"].get("OBJNAM") or ""
        for pre in prefixes:
            if name.startswith(pre):
                c = shape(f["geometry"]).centroid
                short = ("Brani " if pre.startswith("Brani") else "") + name[len(pre):].strip()
                out.append((short, float(c.y), float(c.x)))
    return sorted(out, key=lambda t: (t[0][:1], len(t[0]), t[0]))


def berth_full_name(code: str, short: str) -> str:
    """MPA's full berth name from berth_points' short name."""
    for pre in TERMINAL_BERTHS.get(code, ()):
        if short.startswith("Brani ") and pre.startswith("Brani"):
            return pre + short[len("Brani "):]
        if not short.startswith("Brani ") and not pre.startswith("Brani"):
            return pre + short
    return short


def depth_at(lat: float, lon: float, radius_m: float = 0.0) -> Optional[float]:
    """Shallowest charted depth at (or within `radius_m` of) a point; None if uncharted."""
    from shapely.geometry import Point, shape
    p = Point(lon, lat)
    if radius_m:
        p = p.buffer(radius_m / 111_000.0)
    best = None
    for f in features("depthsa"):
        g = shape(f["geometry"])
        if g.intersects(p):
            d = area_depth(f["properties"])
            best = d if best is None else min(best, d)
    return best
