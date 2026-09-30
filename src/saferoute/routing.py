"""Plan a route and measure how safe it actually is.

`plan_route` runs the search on a built network, string-pulls the lattice path
into straight legs, and then measures the continuous track against the same
physical world - every stationary ship, the shore and the depth - so the
numbers reported are properties of the track a ship would steer, not of the
grid.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from . import study_area as SA
from .algorithms import SearchResult, astar, dijkstra, k_shortest_paths
from .geo import M_PER_NM, densify, haversine_nm, initial_bearing_deg, path_length_nm, turn_angles_deg
from .network import NavNetwork, smooth_path
from .obstacles import Snapshot


@dataclass
class RoutePlan:
    label: str
    mode: str
    coords: List[Tuple[float, float]]
    metrics: Dict
    search: Optional[Dict] = None
    encounters: List[Dict] = field(default_factory=list)

    def as_row(self) -> Dict:
        d = dict(label=self.label, mode=self.mode, **self.metrics)
        return d


# --------------------------------------------------------------------------
def track_metrics(net: NavNetwork, coords: Sequence[Tuple[float, float]],
                  snap: Optional[Snapshot], step_nm: float = 0.02,
                  max_list: int = 12) -> Tuple[Dict, List[Dict]]:
    """Safety and length of a continuous track.

    Ship clearance is measured two ways:
      * to the planning envelope (swing circle for anchored ships) - what the
        router kept clear of;
      * to the hull at its snapshot position (a circle of LOA/2) - the
        physical distance a watchkeeper would see at that moment.
    An "obstacle met" is a ship whose envelope comes inside the target CPA for
    the zone where the closest approach happens.
    """
    g, v = net.grid, net.vessel
    coords = [tuple(c) for c in coords]
    dist = path_length_nm(coords)
    pts = np.asarray(densify(coords, step_nm)) if len(coords) > 1 else np.asarray(coords)
    iy, ix = g.index_arrays(pts[:, 0], pts[:, 1])
    half_beam = 0.5 * v.beam_m / M_PER_NM

    seg = haversine_nm(pts[:-1, 0], pts[:-1, 1], pts[1:, 0], pts[1:, 1]) if len(pts) > 1 else np.zeros(0)
    cum = np.concatenate([[0.0], np.cumsum(seg)])

    depth = g.depth_m[iy, ix]
    req = net.req_depth[iy, ix]
    shore = g.dist_to_land_nm[iy, ix]
    m = dict(
        distance_nm=dist,
        passage_h=dist / SA.PLANNING_SPEED_KN,
        legs=max(len(coords) - 1, 0),
        max_turn_deg=max(turn_angles_deg(coords), default=0.0),
        min_shore_nm=float(shore.min()) if len(shore) else float("nan"),
        min_depth_m=float(depth.min()) if len(depth) else float("nan"),
        min_ukc_m=float((depth - v.draught_m).min()) if len(depth) else float("nan"),
        shallow_nm=float(np.sum((depth < req)[:-1] * seg)) if len(seg) else 0.0,
        on_land=int(g.land[iy, ix].sum()),
        straight_line_nm=float(haversine_nm(coords[0][0], coords[0][1],
                                            coords[-1][0], coords[-1][1])) if coords else 0.0,
    )
    m["detour_pct"] = 100.0 * (dist / m["straight_line_nm"] - 1.0) if m["straight_line_nm"] > 0 else 0.0

    enc: List[Dict] = []
    if snap is not None and len(snap.ships):
        env = np.array([s.envelope(net.cfg.swing) for s in snap.ships])
        hull = np.array([(s.lat, s.lon, s.loa_m / 2.0 / M_PER_NM) for s in snap.ships])
        # (points x ships) is fine at strait scale: ~2,000 x ~300.
        d_env = haversine_nm(pts[:, None, 0], pts[:, None, 1], env[None, :, 0], env[None, :, 1]) \
            - env[None, :, 2] - half_beam
        d_hull = haversine_nm(pts[:, None, 0], pts[:, None, 1], hull[None, :, 0], hull[None, :, 1]) \
            - hull[None, :, 2] - half_beam
        k_min = np.argmin(d_env, axis=0)
        cpa_env = d_env[k_min, np.arange(len(env))]
        cpa_hull = d_hull.min(axis=0)
        tgt = net.target_cpa[iy[k_min], ix[k_min]]
        hard = net.hard_cpa[iy[k_min], ix[k_min]]
        met = np.nonzero(cpa_env < tgt)[0]
        order = met[np.argsort(cpa_env[met])]
        for j in order:
            s = snap.ships[j]
            k = int(k_min[j])
            enc.append(dict(id=s.id, name=s.name, ship_type=s.ship_type, status=s.status,
                            loa_m=s.loa_m, area=s.area,
                            cpa_nm=round(float(cpa_env[j]), 3),
                            hull_nm=round(float(cpa_hull[j]), 3),
                            target_nm=float(tgt[j]), hard_nm=float(hard[j]),
                            at=[round(float(pts[k, 0]), 5), round(float(pts[k, 1]), 5)],
                            along_nm=round(float(cum[k]), 2),
                            ship=[s.lat, s.lon]))
        m.update(
            obstacles_met=int(len(met)),
            hard_breaches=int(np.sum(cpa_env < hard)),
            min_ship_clear_nm=float(cpa_env.min()),
            min_hull_dist_nm=float(cpa_hull.min()),
            ships_within_1nm=int(np.sum(cpa_hull < 1.0)),
            ships_considered=int(len(env)),
        )
        # exposure: NM sailed inside some ship's target CPA
        inside_any = (d_env < net.target_cpa[iy, ix][:, None]).any(axis=1)
        m["nm_inside_cpa"] = float(np.sum(inside_any[:-1] * seg)) if len(seg) else 0.0
    else:
        m.update(obstacles_met=0, hard_breaches=0, min_ship_clear_nm=float("nan"),
                 min_hull_dist_nm=float("nan"), ships_within_1nm=0,
                 ships_considered=0, nm_inside_cpa=0.0)
    m["feasible"] = bool(m["on_land"] == 0 and m["shallow_nm"] < 1e-6 and m["hard_breaches"] == 0)
    return m, enc[:max_list] if max_list else enc


# --------------------------------------------------------------------------
def endpoints(net: NavNetwork, origin, destination):
    """Resolve port codes or (lat, lon) pairs to network nodes."""
    def pt(x):
        if isinstance(x, str):
            return SA.port(x).approach
        return (float(x[0]), float(x[1]))
    a, b = pt(origin), pt(destination)
    return net.node_at(*a), net.node_at(*b), a, b


def plan_route(net: NavNetwork, origin, destination, snap: Optional[Snapshot],
               algorithm: str = "astar", label: Optional[str] = None,
               alternatives: int = 1) -> List[RoutePlan]:
    src, dst, a, b = endpoints(net, origin, destination)
    t0 = time.perf_counter()
    if alternatives > 1:
        results = k_shortest_paths(net, src, dst, k=alternatives)
    else:
        results = [dijkstra(net, src, dst) if algorithm == "dijkstra" else astar(net, src, dst)]
    search_s = time.perf_counter() - t0
    plans = []
    for i, r in enumerate(results):
        if not r.found:
            continue
        raw = net.coords(r.path)
        # The request point may sit a cell or two off the snapped node; keep
        # the track starting and ending on the node the search actually used.
        sm = smooth_path(net, raw)
        met, enc = track_metrics(net, sm, snap)
        met["grid_distance_nm"] = net.path_cost(r.path)[1]
        met["cost_effective_nm"] = r.cost
        lab = label or net.cfg.mode
        if i > 0:
            lab = f"{lab} - alternative {chr(64 + i)}"
        plans.append(RoutePlan(lab, net.cfg.mode, sm, met,
                               search=dict(r.as_row(), search_s=search_s),
                               encounters=enc))
    return plans


def compare_search(net: NavNetwork, origin, destination, reps: int = 1) -> Dict:
    src, dst, _, _ = endpoints(net, origin, destination)
    rows = []
    for _ in range(reps):
        rows.append((dijkstra(net, src, dst).as_row(), astar(net, src, dst).as_row()))
    mean = lambda k, i: float(np.mean([r[i][k] for r in rows]))
    keys = ("nodes_expanded", "edges_relaxed", "runtime_ms", "cost_effective_nm")
    return dict(dijkstra={k: mean(k, 0) for k in keys},
                astar={k: mean(k, 1) for k in keys},
                cost_gap=abs(rows[0][0]["cost_effective_nm"] - rows[0][1]["cost_effective_nm"]))
