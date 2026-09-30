"""Historical voyages: the baseline that routes are validated against.

Real AIS voyage data for the Singapore Strait comes from the sibling capstone
group; `load_real_ais` reads their cleaned output (one row per position report
with a voyage id). Until it is available, `generate_baseline_voyages` produces
a SYNTHETIC corpus that emulates current practice - join the TSS lane, run it,
leave it towards the terminal approach - with correlated lateral wander.

The baseline is built by a mechanism that never consults the cost graph or the
ship snapshot, so comparing it with the optimiser measures the value of
planning around obstacles rather than comparing an algorithm with a blurred
copy of itself. Every result derived from the synthetic corpus is a result
about the METHOD, not about real vessel behaviour.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd
from scipy import ndimage

from . import study_area as SA
from .geo import densify, haversine_nm, initial_bearing_deg, path_length_nm
from .seagrid import NM_PER_DEG, SeaGrid


# --------------------------------------------------------------------------
@dataclass
class Voyage:
    voyage_id: str
    origin: str
    destination: str
    vessel_class: str
    track: List[Tuple[float, float]]
    times_h: List[float]
    synthetic: bool = True

    @property
    def distance_nm(self) -> float:
        return path_length_nm(self.track)

    @property
    def duration_h(self) -> float:
        return self.times_h[-1] - self.times_h[0]

    def to_frame(self) -> pd.DataFrame:
        return pd.DataFrame({
            "voyage_id": self.voyage_id, "origin": self.origin,
            "destination": self.destination, "vessel_class": self.vessel_class,
            "t_h": self.times_h,
            "lat": [p[0] for p in self.track], "lon": [p[1] for p in self.track]})

# --------------------------------------------------------------------------
# lane geometry helpers
# --------------------------------------------------------------------------
def _offset_centreline(side: str, offset_nm: float) -> List[Tuple[float, float]]:
    """Return the TSS centreline shifted perpendicular by `offset_nm`.

    side 'N' shifts to the left of the west->east centreline (westbound lane),
    side 'S' shifts to the right (eastbound lane).
    """
    cl = np.asarray(SA.TSS_CENTRELINE, dtype=float)
    out = []
    for i, (lat, lon) in enumerate(cl):
        j0 = max(i - 1, 0)
        j1 = min(i + 1, len(cl) - 1)
        brg = math.radians(initial_bearing_deg(cl[j0][0], cl[j0][1],
                                               cl[j1][0], cl[j1][1]))
        # unit normal, left of track
        nx, ny = -math.cos(brg), math.sin(brg)      # (east, north) components
        sign = 1.0 if side == "N" else -1.0
        dlat = sign * offset_nm * ny / NM_PER_DEG
        dlon = sign * offset_nm * nx / (NM_PER_DEG * math.cos(math.radians(lat)))
        out.append((lat + dlat, lon + dlon))
    return out


def _lane_for(origin: SA.Port, destination: SA.Port) -> str:
    """Which TSS lane a voyage should use: 'N' (westbound) or 'S' (eastbound)."""
    return "S" if destination.app_lon >= origin.app_lon else "N"


def _nearest_index(pts: Sequence[Tuple[float, float]], lat: float, lon: float) -> int:
    arr = np.asarray(pts)
    d = haversine_nm(arr[:, 0], arr[:, 1], lat, lon)
    return int(np.argmin(d))


# --------------------------------------------------------------------------
# land-avoidance repair
# --------------------------------------------------------------------------
def _repair_track(grid: SeaGrid, track: List[Tuple[float, float]],
                  min_clearance_nm: float, iterations: int = 60
                  ) -> List[Tuple[float, float]]:
    """Push track points away from land until they clear `min_clearance_nm`.

    Uses the gradient of the shoreline distance transform as the push
    direction, then re-smooths so the result stays a plausible ship track
    rather than a sawtooth.
    """
    gy, gx = np.gradient(grid.dist_to_land_nm)
    pts = np.asarray(track, dtype=float)
    res = grid.resolution_deg

    for _ in range(iterations):
        iy = np.clip(np.round((pts[:, 0] - grid.lats[0]) / res).astype(int),
                     0, grid.ny - 1)
        ix = np.clip(np.round((pts[:, 1] - grid.lons[0]) / res).astype(int),
                     0, grid.nx - 1)
        clearance = grid.dist_to_land_nm[iy, ix]
        bad = clearance < min_clearance_nm
        if not bad.any():
            break
        deficit = (min_clearance_nm - clearance)[bad]
        vy, vx = gy[iy[bad], ix[bad]], gx[iy[bad], ix[bad]]
        norm = np.hypot(vy, vx)
        norm[norm < 1e-9] = 1e-9
        step_deg = np.minimum(deficit, 0.6) / NM_PER_DEG
        pts[bad, 0] += step_deg * vy / norm
        pts[bad, 1] += step_deg * vx / norm
        # keep the endpoints fixed
        pts[0] = track[0]
        pts[-1] = track[-1]
        # light smoothing of the interior
        if len(pts) > 4:
            pts[1:-1, 0] = ndimage.uniform_filter1d(pts[:, 0], 3)[1:-1]
            pts[1:-1, 1] = ndimage.uniform_filter1d(pts[:, 1], 3)[1:-1]

    return [tuple(p) for p in pts]


# --------------------------------------------------------------------------
def _corridor_route(grid: SeaGrid, origin: SA.Port, destination: SA.Port,
                    rng: np.random.Generator, min_clearance_nm: float,
                    lane_offset_nm: Optional[float] = None,
                    unaware_net=None) -> List[Tuple[float, float]]:
    """Current-practice route: join the lane, run it, leave it.

    With `unaware_net` (a network built WITHOUT any ship obstacles) each leg
    between lane waypoints is steered round land and shallows by that network,
    so the baseline is a competent passage that simply does not know where the
    anchored ships are. Without it, legs are straight and repaired off the
    shore - a cruder baseline that can clip islands.
    """
    side = _lane_for(origin, destination)
    direct_nm = haversine_nm(*origin.approach, *destination.approach)
    if lane_offset_nm is None:
        # Masters do not sit on the lane axis; they favour the outer third and
        # the spread between vessels is what produces the density field.
        lane_offset_nm = float(
            SA.TSS_SEPARATION_HALF_WIDTH_NM + SA.TSS_LANE_WIDTH_NM *
            rng.uniform(0.30, 0.80))
    lane = _offset_centreline(side, lane_offset_nm)

    i0 = _nearest_index(lane, *origin.approach)
    i1 = _nearest_index(lane, *destination.approach)
    if i0 == i1:
        core = []
    elif i0 < i1:
        core = lane[i0:i1 + 1]
    else:
        core = lane[i1:i0 + 1][::-1]

    raw = [origin.approach] + core + [destination.approach]

    # Drop any lane vertex that would make the vessel double back on itself.
    cleaned = [raw[0]]
    for p in raw[1:]:
        if haversine_nm(*cleaned[-1], *p) > 0.25:
            cleaned.append(p)
    if haversine_nm(*cleaned[-1], *raw[-1]) > 1e-6:
        cleaned.append(raw[-1])

    # A master joins the scheme only when it is on the way; for a short local
    # passage current practice is to go direct.
    lane_nm = path_length_nm(cleaned)
    if lane_nm > 1.35 * direct_nm + 2.0:
        cleaned = [origin.approach, destination.approach]

    if unaware_net is not None:
        from .algorithms import astar
        from .network import smooth_path
        legs = [cleaned[0]]
        for a, b in zip(cleaned[:-1], cleaned[1:]):
            r = astar(unaware_net, unaware_net.node_at(*a), unaware_net.node_at(*b))
            if r.found:
                legs.extend(smooth_path(unaware_net, unaware_net.coords(r.path))[1:])
            else:
                legs.append(b)
        legs[-1] = cleaned[-1]
        cleaned = legs
    # the same pilotage legs to and from the berth as the planned routes
    if origin.pilot:
        cleaned = [tuple(c) for c in origin.pilot[::-1]] + list(cleaned[1:])
    if destination.pilot:
        cleaned = list(cleaned[:-1]) + [tuple(c) for c in destination.pilot]
    track = densify(cleaned, max_step_nm=0.30)

    # Correlated lateral noise: real tracks wander, they do not jitter.
    n = len(track)
    noise = ndimage.gaussian_filter1d(rng.normal(0.0, 1.0, n), sigma=max(3, n // 25))
    noise = noise / (np.std(noise) + 1e-9) * rng.uniform(0.06, 0.20)   # NM
    out = []
    for k, (lat, lon) in enumerate(track):
        j0, j1 = max(k - 1, 0), min(k + 1, n - 1)
        brg = math.radians(initial_bearing_deg(*track[j0], *track[j1]))
        nx, ny = -math.cos(brg), math.sin(brg)
        out.append((lat + noise[k] * ny / NM_PER_DEG,
                    lon + noise[k] * nx / (NM_PER_DEG * math.cos(math.radians(lat)))))
    out[0], out[-1] = track[0], track[-1]
    if unaware_net is not None:
        # wander only where the wandered point is still good water
        iy, ix = grid.index_arrays([p[0] for p in out], [p[1] for p in out])
        ok = unaware_net.navigable[iy, ix]
        return [o if k else t for o, t, k in zip(out, track, ok)]
    return _repair_track(grid, out, min_clearance_nm)


# --------------------------------------------------------------------------
def generate_baseline_voyages(grid: SeaGrid,
                              od_pairs: Optional[Sequence[Tuple[str, str]]] = None,
                              n_per_od: int = 20,
                              classes: Sequence[str] = ("FEEDER", "PANAMAX"),
                              min_clearance_nm: float = 0.30,
                              seed: int = 20260917,
                              unaware_nets: Optional[dict] = None) -> List[Voyage]:
    """Synthesise a corpus of current-practice voyages for every OD pair.

    `unaware_nets` maps vessel class -> a network built with no ship obstacles
    (see _corridor_route). Recommended: it makes the baseline land- and
    depth-safe, so the comparison isolates obstacle awareness.
    """
    rng = np.random.default_rng(seed)
    out: List[Voyage] = []
    for o, d in (od_pairs or SA.OD_PAIRS):
        for k in range(n_per_od):
            vc = str(classes[k % len(classes)])
            track = _corridor_route(grid, SA.port(o), SA.port(d), rng, min_clearance_nm,
                                    unaware_net=(unaware_nets or {}).get(vc))
            v = SA.PLANNING_SPEED_KN * rng.uniform(0.9, 1.1)
            times, acc = [0.0], 0.0
            for i in range(len(track) - 1):
                acc += haversine_nm(*track[i], *track[i + 1]) / v
                times.append(acc)
            out.append(Voyage(f"{o}_{d}-{k:03d}", o, d, vc, track, times))
    return out


def voyages_to_frame(voyages: Sequence[Voyage]) -> pd.DataFrame:
    return pd.concat([v.to_frame() for v in voyages], ignore_index=True)


def load_real_ais(csv_path: str) -> List[Voyage]:
    """Drop-in replacement for `generate_baseline_voyages`.

    Expects columns voyage_id, origin, destination, vessel_class, t_h, lat, lon
    (the schema `voyages_to_frame` writes). origin/destination must be port
    codes from study_area.PORTS; segmenting raw AIS into voyages is upstream.
    """
    df = pd.read_csv(csv_path).sort_values(["voyage_id", "t_h"])
    out = []
    for vid, g in df.groupby("voyage_id", sort=False):
        out.append(Voyage(str(vid), str(g["origin"].iloc[0]),
                          str(g["destination"].iloc[0]), str(g["vessel_class"].iloc[0]),
                          list(zip(g["lat"], g["lon"])), list(g["t_h"]), synthetic=False))
    return out
