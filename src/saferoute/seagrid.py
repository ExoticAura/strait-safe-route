"""Navigational network construction, step 1: the static raster "board".

The study area is a plate-carree lat/lon raster. At ~1.2 deg N one degree of
longitude is cos(1.2 deg) = 0.99978 of a degree of latitude, so the raster is
treated as locally metric for transforms; every *distance* is still computed
with the haversine formula.

Layers
------
land            bool   True where the cell touches land (OpenStreetMap coast)
dist_to_land_nm float  distance to the nearest land cell, NM
depth_m         float  charted depth, metres positive down (real bathymetry if
                       a file is supplied, otherwise a DECLARED PROXY)
tss_zone        int8   0 outside, 1 westbound lane, 2 separation zone,
                       3 eastbound lane, 4 inshore band next to a lane
tss_heading     float  required along-lane heading, deg true (NaN outside)
cpa_zone        int8   which passing-distance rule applies here:
                       0 port approach / fairway, 1 TSS, 2 berthing basin

Ship obstacles are NOT a grid layer here: they change with every AIS snapshot,
so obstacles.py rasterises them on top of this board per snapshot.
"""
from __future__ import annotations

import glob
import json
import math
import os
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

import numpy as np
from scipy import ndimage

from . import study_area as SA
from .geo import haversine_nm

NM_PER_DEG = 60.0
PKG_DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
LAND_GEOJSON = os.path.join(PKG_DATA, "sg_land.geojson")
# Real bathymetry is picked up automatically from here (see load_bathymetry_file).
BATHY_DIR = os.environ.get(
    "SAFEROUTE_BATHY_DIR",
    os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))), "data", "bathymetry"))

CPA_ZONE_APPROACH, CPA_ZONE_TSS, CPA_ZONE_BASIN = 0, 1, 2
CPA_ZONE_KEYS = {CPA_ZONE_APPROACH: "approach", CPA_ZONE_TSS: "tss",
                 CPA_ZONE_BASIN: "basin"}
BASIN_SHORE_NM = 0.40          # within this of the shore = basin / narrow water
# Container terminals keep their berths and approach pockets dredged to a
# published depth that no shore-distance proxy can know about. Within this
# radius of a terminal's approach position the charted depth is raised to at
# least the terminal's maintained depth (Port.max_draught_m). Applied to the
# proxy AND on top of a real bathymetry file: GEBCO's ~460 m cells cannot resolve them.
DREDGED_POCKET_NM = 1.0
FAIRWAY_HALF_NM = 0.3


# --------------------------------------------------------------------------
# depth proxy
# --------------------------------------------------------------------------
DEPTH_PROXY = dict(d_max_m=32.0, length_scale_nm=0.90)


def depth_from_shore_distance(dist_nm, d_max_m: float = DEPTH_PROXY["d_max_m"],
                              length_scale_nm: float = DEPTH_PROXY["length_scale_nm"]):
    """DECLARED PROXY for bathymetry, used only when no depth file is supplied.

        depth(d) = d_max * (1 - exp(-d / L)),   d_max = 32 m, L = 0.9 NM

    Monotone in distance from the (OpenStreetMap) shoreline, so it marks the
    shallow nearshore strip that a deep-draught ship must keep out of, and it
    returns 21-29 m 1-2 NM offshore, consistent with the 20-25 m controlling
    depth of the strait's deep-water route. It does NOT reproduce individual
    shoals, and any absolute depth read off it is meaningless. Drop a GEBCO
    extract into data/bathymetry/ and it is replaced automatically.
    """
    return d_max_m * (1.0 - np.exp(-np.asarray(dist_nm) / length_scale_nm))


# --------------------------------------------------------------------------
@dataclass
class SeaGrid:
    lats: np.ndarray
    lons: np.ndarray
    land: np.ndarray
    dist_to_land_nm: np.ndarray
    depth_m: np.ndarray
    tss_zone: np.ndarray
    tss_heading: np.ndarray
    cpa_zone: np.ndarray
    resolution_deg: float
    depth_model: str = "shore_distance_proxy"
    depth_source: str = ""
    land_source: str = ""

    @property
    def shape(self):
        return self.land.shape

    @property
    def ny(self):
        return self.land.shape[0]

    @property
    def nx(self):
        return self.land.shape[1]

    def cell_size_nm(self) -> Tuple[float, float]:
        mid = float(np.mean(self.lats))
        return (self.resolution_deg * NM_PER_DEG,
                self.resolution_deg * NM_PER_DEG * math.cos(math.radians(mid)))

    def to_index(self, lat: float, lon: float) -> Tuple[int, int]:
        iy = int(round((lat - self.lats[0]) / self.resolution_deg))
        ix = int(round((lon - self.lons[0]) / self.resolution_deg))
        return min(max(iy, 0), self.ny - 1), min(max(ix, 0), self.nx - 1)

    def index_arrays(self, lat, lon):
        lat, lon = np.asarray(lat, float), np.asarray(lon, float)
        iy = np.clip(np.round((lat - self.lats[0]) / self.resolution_deg).astype(int),
                     0, self.ny - 1)
        ix = np.clip(np.round((lon - self.lons[0]) / self.resolution_deg).astype(int),
                     0, self.nx - 1)
        return iy, ix

    def inside(self, lat: float, lon: float) -> bool:
        return (self.lats[0] - 1e-9 <= lat <= self.lats[-1] + 1e-9 and
                self.lons[0] - 1e-9 <= lon <= self.lons[-1] + 1e-9)

    def nearest_navigable(self, lat: float, lon: float, navigable: np.ndarray,
                          max_radius_cells: int = 60) -> Tuple[int, int]:
        """Snap a position to the closest navigable cell (ring search)."""
        iy0, ix0 = self.to_index(lat, lon)
        if navigable[iy0, ix0]:
            return iy0, ix0
        for r in range(1, max_radius_cells + 1):
            y0, y1 = max(iy0 - r, 0), min(iy0 + r, self.ny - 1)
            x0, x1 = max(ix0 - r, 0), min(ix0 + r, self.nx - 1)
            sub = navigable[y0:y1 + 1, x0:x1 + 1]
            if not sub.any():
                continue
            yy, xx = np.nonzero(sub)
            yy, xx = yy + y0, xx + x0
            d = haversine_nm(lat, lon, self.lats[yy], self.lons[xx])
            k = int(np.argmin(d))
            return int(yy[k]), int(xx[k])
        raise ValueError(
            f"no navigable cell within {max_radius_cells} cells of "
            f"({lat:.4f}, {lon:.4f}) - check the depth/clearance settings")

    # -- real bathymetry ----------------------------------------------------
    def apply_bathymetry(self, lats: np.ndarray, lons: np.ndarray,
                         depth_m: np.ndarray, source: str = "") -> None:
        """Replace the depth proxy with a real grid (metres, positive down).

        Bilinearly resampled onto this grid. Cells the bathymetry marks as dry
        (depth <= 0) become depth 0, i.e. unnavigable for any ship, even where
        the coastline polygon says water - drying banks are exactly the hazard
        this layer exists for.
        """
        from scipy.interpolate import RegularGridInterpolator
        lats = np.asarray(lats, float)
        lons = np.asarray(lons, float)
        depth_m = np.asarray(depth_m, float)
        if lats[0] > lats[-1]:
            lats, depth_m = lats[::-1], depth_m[::-1]
        if lons[0] > lons[-1]:
            lons, depth_m = lons[::-1], depth_m[:, ::-1]
        interp = RegularGridInterpolator((lats, lons), depth_m,
                                         bounds_error=False, fill_value=np.nan)
        LA, LO = np.meshgrid(self.lats, self.lons, indexing="ij")
        d = interp(np.stack([LA.ravel(), LO.ravel()], axis=-1)).reshape(self.shape)
        # Outside the file's coverage keep the proxy rather than inventing 0 m.
        d = np.where(np.isnan(d), self.depth_m, d)
        self.depth_m = np.where(self.land, 0.0, np.maximum(d, 0.0))
        self.depth_model = "bathymetry_file"
        self.depth_source = source


# --------------------------------------------------------------------------
# land from the OpenStreetMap coastline
# --------------------------------------------------------------------------
def load_land_polygons(path: str = LAND_GEOJSON) -> List[List[np.ndarray]]:
    """Land as a list of polygons, each a list of (lon, lat) rings (outer first)."""
    with open(path) as fh:
        gj = json.load(fh)
    feats = gj["features"] if gj.get("type") == "FeatureCollection" else [gj]
    polys = []
    for f in feats:
        g = f["geometry"] if "geometry" in f else f
        parts = [g["coordinates"]] if g["type"] == "Polygon" else g["coordinates"]
        for p in parts:
            polys.append([np.asarray(r, float) for r in p])
    return polys


def rasterise_land(lats: np.ndarray, lons: np.ndarray,
                   polys: Sequence[Sequence[np.ndarray]], supersample: int = 3
                   ) -> np.ndarray:
    """Cells that touch land.

    Each cell is tested at `supersample` x `supersample` points and marked land
    if ANY point is on land. Conservative on purpose: a jetty or islet thinner
    than a cell still blocks the cell, where centre-point sampling would let a
    route slip through it.
    """
    from matplotlib.path import Path
    res = float(lats[1] - lats[0]) if len(lats) > 1 else 0.002
    offs = (np.arange(supersample) + 0.5) / supersample - 0.5
    land = np.zeros((len(lats), len(lons)), dtype=bool)
    for oy in offs:
        for ox in offs:
            LA, LO = np.meshgrid(lats + oy * res, lons + ox * res, indexing="ij")
            pts = np.column_stack([LO.ravel(), LA.ravel()])
            hit = np.zeros(len(pts), dtype=bool)
            for rings in polys:
                outer = rings[0]
                x0, y0 = outer.min(axis=0)
                x1, y1 = outer.max(axis=0)
                cand = ((pts[:, 0] >= x0) & (pts[:, 0] <= x1) &
                        (pts[:, 1] >= y0) & (pts[:, 1] <= y1) & ~hit)
                if not cand.any():
                    continue
                idx = np.nonzero(cand)[0]
                inside = Path(outer).contains_points(pts[idx])
                for hole in rings[1:]:
                    inside &= ~Path(hole).contains_points(pts[idx])
                hit[idx[inside]] = True
            land |= hit.reshape(LA.shape)
    return land


# --------------------------------------------------------------------------
# TSS field construction
# --------------------------------------------------------------------------
def _project_nm(lats, lons, lat0, lon0):
    k = math.cos(math.radians(lat0))
    return (lons - lon0) * NM_PER_DEG * k, (lats - lat0) * NM_PER_DEG


def _tss_fields(lats: np.ndarray, lons: np.ndarray,
                centreline: Sequence[Tuple[float, float]]):
    """Signed cross-track offset (NM, + = north/left) and along-track bearing."""
    lat0, lon0 = float(np.mean(lats)), float(np.mean(lons))
    LA, LO = np.meshgrid(lats, lons, indexing="ij")
    PX, PY = _project_nm(LA, LO, lat0, lon0)
    cl = np.asarray(centreline, dtype=float)
    CX, CY = _project_nm(cl[:, 0], cl[:, 1], lat0, lon0)

    best_d = np.full(PX.shape, np.inf)
    best_sign = np.zeros(PX.shape)
    best_seg = np.zeros(PX.shape, dtype=int)
    best_t = np.zeros(PX.shape)
    for i in range(len(CX) - 1):
        ax, ay, bx, by = CX[i], CY[i], CX[i + 1], CY[i + 1]
        vx, vy = bx - ax, by - ay
        L2 = vx * vx + vy * vy
        t = np.clip(((PX - ax) * vx + (PY - ay) * vy) / L2, 0.0, 1.0)
        qx, qy = ax + t * vx, ay + t * vy
        d = np.hypot(PX - qx, PY - qy)
        cross = vx * (PY - ay) - vy * (PX - ax)
        upd = d < best_d
        best_d = np.where(upd, d, best_d)
        best_sign = np.where(upd, np.sign(cross), best_sign)
        best_seg = np.where(upd, i, best_seg)
        best_t = np.where(upd, t, best_t)

    seg_bearing = np.array([(math.degrees(math.atan2(CX[i + 1] - CX[i],
                                                     CY[i + 1] - CY[i])) + 360.0) % 360.0
                            for i in range(len(CX) - 1)])
    bearing_e = seg_bearing[best_seg]
    offset = best_d * np.where(best_sign == 0, 1.0, best_sign)
    # Past either end of the scheme there is no lane.
    beyond = (((best_seg == 0) & (best_t <= 0.0)) |
              ((best_seg == len(CX) - 2) & (best_t >= 1.0)))
    return offset, bearing_e, beyond


def _classify_tss(offset_nm: np.ndarray, bearing_east: np.ndarray):
    """0 outside, 1 westbound lane, 2 separation zone, 3 eastbound lane,
    4 inshore band next to a lane.

    The band matters because COLREGs Rule 10 makes the lanes the place for
    through traffic: without it the optimiser discovers the obvious cheat of
    running the strait a mile outside the lanes.
    """
    sep, lane, band = (SA.TSS_SEPARATION_HALF_WIDTH_NM, SA.TSS_LANE_WIDTH_NM,
                       SA.TSS_INSHORE_BAND_NM)
    zone = np.zeros(offset_nm.shape, dtype=np.int8)
    heading = np.full(offset_nm.shape, np.nan)
    a = np.abs(offset_nm)
    north_lane = (offset_nm > sep) & (offset_nm <= sep + lane)
    south_lane = (offset_nm < -sep) & (offset_nm >= -(sep + lane))
    band_ = (a > sep + lane) & (a <= sep + lane + band)
    zone[a <= sep] = 2
    zone[north_lane] = 1
    zone[south_lane] = 3
    zone[band_] = 4
    heading[north_lane] = (bearing_east[north_lane] + 180.0) % 360.0
    heading[south_lane] = bearing_east[south_lane]
    # Each inshore band carries the direction of the lane it borders, so a
    # vessel running the band the wrong way (a westbound ship along the
    # southern side of the scheme) can be told apart from a crossing ship.
    nb = band_ & (offset_nm > 0)
    sb = band_ & (offset_nm < 0)
    heading[nb] = (bearing_east[nb] + 180.0) % 360.0
    heading[sb] = bearing_east[sb]
    return zone, heading


# --------------------------------------------------------------------------
# bathymetry files
# --------------------------------------------------------------------------
def read_esri_ascii(path: str):
    """ESRI ASCII grid (GEBCO's 'Esri ASCII' download). Returns lats, lons, z."""
    hdr = {}
    with open(path) as fh:
        for _ in range(6):
            pos = fh.tell()
            line = fh.readline()
            k, *v = line.split()
            if k.lower() in ("ncols", "nrows", "xllcorner", "yllcorner", "xllcenter",
                             "yllcenter", "cellsize", "nodata_value"):
                hdr[k.lower()] = float(v[0])
            else:
                fh.seek(pos)
                break
        z = np.loadtxt(fh)
    nc, nr, cs = int(hdr["ncols"]), int(hdr["nrows"]), hdr["cellsize"]
    x0 = hdr.get("xllcenter", hdr.get("xllcorner", 0.0) + cs / 2)
    y0 = hdr.get("yllcenter", hdr.get("yllcorner", 0.0) + cs / 2)
    lons = x0 + cs * np.arange(nc)
    lats = y0 + cs * np.arange(nr)[::-1]          # first row is the north edge
    if "nodata_value" in hdr:
        z = np.where(z == hdr["nodata_value"], np.nan, z)
    return lats, lons, z


def read_geotiff(path: str):
    """GeoTIFF with ModelPixelScale/ModelTiepoint tags (GEBCO 'Data GeoTIFF')."""
    import tifffile                              # optional dependency
    with tifffile.TiffFile(path) as tf:
        page = tf.pages[0]
        z = page.asarray().astype(float)
        scale = page.tags["ModelPixelScaleTag"].value
        tie = page.tags["ModelTiepointTag"].value
    dx, dy = scale[0], scale[1]
    x0, y0 = tie[3], tie[4]                      # upper-left corner
    lons = x0 + dx * (np.arange(z.shape[1]) + 0.5)
    lats = y0 - dy * (np.arange(z.shape[0]) + 0.5)
    return lats, lons, z


def read_netcdf(path: str):
    """GEBCO netCDF (lat, lon, elevation). Needs netCDF4 or h5py."""
    try:
        import netCDF4                           # optional dependency
        with netCDF4.Dataset(path) as ds:
            lat = np.asarray(ds.variables["lat"][:], float)
            lon = np.asarray(ds.variables["lon"][:], float)
            var = "elevation" if "elevation" in ds.variables else "z"
            z = np.asarray(ds.variables[var][:], float)
    except ImportError:
        import h5py                              # optional dependency
        with h5py.File(path, "r") as ds:
            lat, lon = np.asarray(ds["lat"], float), np.asarray(ds["lon"], float)
            z = np.asarray(ds["elevation"] if "elevation" in ds else ds["z"], float)
    return lat, lon, z


def read_xyz_csv(path: str):
    """lat, lon, depth_m (positive down) on a regular grid, CSV with header."""
    import pandas as pd
    df = pd.read_csv(path)
    cols = {c.lower(): c for c in df.columns}
    lat, lon = df[cols["lat"]], df[cols["lon"]]
    dcol = cols.get("depth_m") or cols.get("depth")
    ecol = cols.get("elevation") or cols.get("z")
    val = df[dcol] if dcol else -df[ecol]
    tab = pd.DataFrame({"lat": lat, "lon": lon, "d": val}).pivot_table(
        index="lat", columns="lon", values="d")
    # stored as ELEVATION so every reader returns the same sign convention
    return tab.index.to_numpy(), tab.columns.to_numpy(), -tab.to_numpy()


def load_bathymetry_file(path: str):
    """Read any supported file. Returns (lats, lons, depth_m positive down)."""
    ext = os.path.splitext(path)[1].lower()
    if ext in (".asc", ".txt"):
        lats, lons, z = read_esri_ascii(path)
    elif ext in (".tif", ".tiff"):
        lats, lons, z = read_geotiff(path)
    elif ext in (".nc", ".nc4"):
        lats, lons, z = read_netcdf(path)
    elif ext == ".csv":
        lats, lons, z = read_xyz_csv(path)
    else:
        raise ValueError(f"unsupported bathymetry format: {ext}")
    # GEBCO and every format above is ELEVATION (negative below sea level).
    return lats, lons, -z


def find_bathymetry_file(directory: str = BATHY_DIR) -> Optional[str]:
    if not os.path.isdir(directory):
        return None
    for pat in ("*.asc", "*.tif", "*.tiff", "*.nc", "*.csv"):
        hits = sorted(glob.glob(os.path.join(directory, pat)))
        if hits:
            return hits[0]
    return None


# --------------------------------------------------------------------------
_FAIRWAYS: dict = {}


def fairway_path(code: str) -> List[Tuple[float, float]]:
    """A terminal's fairway as a polyline that stays in water: the cheapest
    water path from its approach to the TSS end of study_area.fairway(code),
    keeping to mid-channel. The straight line study_area.fairway() gives can
    cross islands (Pulau Bukom, off Pasir Panjang); the dredged corridor and
    the ship-free corridor follow this path instead."""
    if code in _FAIRWAYS:
        return _FAIRWAYS[code]
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import dijkstra
    res = SA.DEFAULT_RESOLUTION_DEG
    lats = np.arange(SA.BBOX["lat_min"], SA.BBOX["lat_max"] + 1e-9, res)
    lons = np.arange(SA.BBOX["lon_min"], SA.BBOX["lon_max"] + 1e-9, res)
    land = rasterise_land(lats, lons, load_land_polygons(LAND_GEOJSON))
    dist = ndimage.distance_transform_edt(~land) * res * 60.0          # NM, roughly
    ny, nx = land.shape
    idx = -np.ones(land.shape, int)
    idx[~land] = np.arange((~land).sum())
    rows, cols, w = [], [], []
    for dy, dx in ((0, 1), (1, 0), (1, 1), (1, -1)):
        sa = (slice(0, ny - dy), slice(max(0, -dx), nx - max(0, dx)))
        sb = (slice(dy, ny), slice(max(0, dx), nx - max(0, -dx)))
        A, B = idx[sa], idx[sb]
        m = (A >= 0) & (B >= 0)
        cm = 1.0 + 4.0 * np.exp(-0.5 * (dist[sa] + dist[sb]) / 0.25)
        rows += list(A[m]); cols += list(B[m]); w += list(math.hypot(dy, dx) * cm[m])
    n = int((~land).sum())
    G = coo_matrix((w, (rows, cols)), shape=(n, n)).tocsr()
    (a_la, a_lo), (b_la, b_lo) = SA.fairway(code)

    def cell(la, lo):
        iy, ix = int(round((la - lats[0]) / res)), int(round((lo - lons[0]) / res))
        if land[iy, ix]:
            d = np.where(land, 1e9, np.hypot(np.arange(ny)[:, None] - iy, np.arange(nx)[None, :] - ix))
            iy, ix = np.unravel_index(np.argmin(d), d.shape)
        return iy, ix
    (ay, ax), (by, bx) = cell(a_la, a_lo), cell(b_la, b_lo)
    s, t = idx[ay, ax], idx[by, bx]
    _, pred = dijkstra(G, directed=False, indices=s, return_predecessors=True)
    inv = np.argwhere(~land)
    path, c = [], t
    while c >= 0:
        y, x = inv[c]
        path.append((float(lats[y]), float(lons[x])))
        if c == s:
            break
        c = pred[c]
    path = path[::-1] if path and path[-1] == (float(lats[ay]), float(lons[ax])) else path
    out = [(a_la, a_lo)] + path[::3] + [(b_la, b_lo)]
    _FAIRWAYS[code] = out
    return out


def dredged_pocket_mask(lats, lons, land, port) -> np.ndarray:
    """Water dredged to the terminal's depth: round the approach, round the
    berth, along the pilotage track between them (0.3 NM either side) and
    along the fairway from the approach to the TSS (study_area.fairway)."""
    LA, LO = np.meshgrid(lats, lons, indexing="ij")
    m = haversine_nm(LA, LO, port.app_lat, port.app_lon) <= DREDGED_POCKET_NM
    m |= haversine_nm(LA, LO, port.lat, port.lon) <= DREDGED_POCKET_NM
    # ... and along the fairway out to the TSS (FAIRWAY_HALF_NM either side)
    if port.kind == "terminal":
        fw = fairway_path(port.code)
        for (a_la, a_lo), (b_la, b_lo) in zip(fw[:-1], fw[1:]):
            n = max(2, int(haversine_nm(a_la, a_lo, b_la, b_lo) / 0.1) + 1)
            for t in np.linspace(0.0, 1.0, n):
                m |= haversine_nm(LA, LO, a_la + (b_la - a_la) * t, a_lo + (b_lo - a_lo) * t) <= FAIRWAY_HALF_NM
    for (a_la, a_lo), (b_la, b_lo) in zip(port.pilot[:-1], port.pilot[1:]):
        n = max(2, int(haversine_nm(a_la, a_lo, b_la, b_lo) / 0.1) + 1)
        for t in np.linspace(0.0, 1.0, n):
            m |= haversine_nm(LA, LO, a_la + (b_la - a_la) * t, a_lo + (b_lo - a_lo) * t) <= 0.3
    return (~land) & m


def apply_dredged_pockets(lats, lons, land, depth) -> np.ndarray:
    out = depth.copy()
    for p in SA.PORTS.values():
        if p.kind != "terminal":
            continue
        m = dredged_pocket_mask(lats, lons, land, p)
        out[m] = np.maximum(out[m], p.max_draught_m)
    return out


# --------------------------------------------------------------------------
def build_seagrid(resolution_deg: float = SA.DEFAULT_RESOLUTION_DEG,
                  bbox: Optional[dict] = None,
                  land_geojson: str = LAND_GEOJSON,
                  bathymetry: Optional[str] = "auto") -> SeaGrid:
    """Build the static board: land, clearance, depth, TSS and CPA zones.

    `bathymetry`: "auto" uses the first file in data/bathymetry/ if there is
    one; a path uses that file; None forces the proxy.
    """
    bbox = bbox or SA.BBOX
    lats = np.arange(bbox["lat_min"], bbox["lat_max"] + 1e-9, resolution_deg)
    lons = np.arange(bbox["lon_min"], bbox["lon_max"] + 1e-9, resolution_deg)

    land = rasterise_land(lats, lons, load_land_polygons(land_geojson))

    dy_nm = resolution_deg * NM_PER_DEG
    dx_nm = dy_nm * math.cos(math.radians(float(lats.mean())))
    dist = ndimage.distance_transform_edt(~land, sampling=(dy_nm, dx_nm)).astype(float)
    depth = np.where(land, 0.0, depth_from_shore_distance(dist))

    offset, bearing_e, beyond = _tss_fields(lats, lons, SA.TSS_CENTRELINE)
    zone, heading = _classify_tss(offset, bearing_e)
    zone = np.where(beyond | land, 0, zone).astype(np.int8)
    heading = np.where(beyond | land, np.nan, heading)

    depth = apply_dredged_pockets(lats, lons, land, depth)

    cpa = np.full(land.shape, CPA_ZONE_APPROACH, dtype=np.int8)
    cpa[(zone >= 1) & (zone <= 3)] = CPA_ZONE_TSS
    cpa[(dist < BASIN_SHORE_NM) & ~land] = CPA_ZONE_BASIN

    grid = SeaGrid(lats=lats, lons=lons, land=land, dist_to_land_nm=dist,
                   depth_m=depth, tss_zone=zone, tss_heading=heading,
                   cpa_zone=cpa, resolution_deg=resolution_deg,
                   land_source="OpenStreetMap coastline (ODbL), 2017 extract")

    path = find_bathymetry_file() if bathymetry == "auto" else bathymetry
    if path:
        la, lo, d = load_bathymetry_file(path)
        grid.apply_bathymetry(la, lo, d, source=os.path.basename(path))
        # GEBCO is ~460 m: it cannot see the dredged terminal pockets, berth
        # basins and fairways (it shows 0-11 m at the Jurong / Pasir Panjang
        # approaches). Keep their maintained depth on top of the real grid.
        grid.depth_m = apply_dredged_pockets(lats, lons, land, grid.depth_m)
    return grid
