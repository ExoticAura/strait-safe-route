"""Plan a route and measure how safe it actually is.

`plan_route` runs the search on a built network, string-pulls the lattice path
into straight legs, sails those legs with a turning model (sailing.py), and
then measures the sailed track against the same physical world - every
stationary ship, the shore and the depth - so the numbers reported are
properties of the track a ship would actually steer, not of the grid.
"""
from __future__ import annotations

import dataclasses
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
from .sailing import TurnModel, sail, track_summary


@dataclass
class RoutePlan:
    label: str
    mode: str
    coords: List[Tuple[float, float]]
    metrics: Dict
    search: Optional[Dict] = None
    encounters: List[Dict] = field(default_factory=list)
    waypoints: List[Tuple[float, float]] = field(default_factory=list)   # the planned legs
    heading: List[float] = field(default_factory=list)                   # ship's head along coords
    turn_radii: List[Optional[float]] = field(default_factory=list)      # per waypoint, m

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


def _pushed_off(net: NavNetwork, base_nav: np.ndarray, blocked: np.ndarray) -> NavNetwork:
    """The same network with `blocked` cells closed (used to push a route off
    an obstacle its turn would cut into)."""
    nav = base_nav & ~blocked
    node_blocked = np.zeros(net.n_nodes, dtype=bool)
    ids = net.node_of_cell[blocked]
    node_blocked[ids[ids >= 0]] = True
    cost = np.where(node_blocked[net.indices], math.inf, net.cost)
    return dataclasses.replace(net, navigable=nav, cost=cost)


def _search(net, src, dst, algorithm, alternatives):
    if alternatives > 1:
        return k_shortest_paths(net, src, dst, k=alternatives)
    return [dijkstra(net, src, dst) if algorithm == "dijkstra" else astar(net, src, dst)]


def plan_route(net: NavNetwork, origin, destination, snap: Optional[Snapshot],
               algorithm: str = "astar", label: Optional[str] = None,
               alternatives: int = 1, turn_model: Optional[TurnModel] = None,
               max_pushes: int = 5) -> List[RoutePlan]:
    """Search, string-pull, sail, measure.

    The metrics describe the SAILED track (see sailing.py): the route a ship
    of this length actually steers, turning at a realistic radius, not the
    polyline of the search. If a turn would cut into a hard limit it is first
    tightened; if that is not enough the route is pushed off the obstacle and
    searched again (up to `max_pushes` times).
    """
    model = turn_model or TurnModel()
    src, dst, a, b = endpoints(net, origin, destination)
    g = net.grid
    base_nav = net.navigable
    blocked = np.zeros(g.shape, dtype=bool)
    keep = np.zeros(g.shape, dtype=bool)               # never close the ends
    for node in (src, dst):
        y, x = int(net.node_iy[node]), int(net.node_ix[node])
        keep[max(0, y - 2):y + 3, max(0, x - 2):x + 3] = True
    # which ends are terminals (sailed under pilotage) rather than strait gateways
    pilot = tuple(isinstance(x, str) and x in SA.TERMINALS for x in (origin, destination))
    t0 = time.perf_counter()
    best = None
    for push in range(max_pushes + 1):
        net_a = net if not blocked.any() else _pushed_off(net, base_nav, blocked)
        results = [r for r in _search(net_a, src, dst, algorithm, alternatives) if r.found]
        if not results:
            break
        sailed = []
        total_bad = 0
        for r in results:
            sm = smooth_path(net_a, net.coords(r.path))
            tr = sail(net, sm, model, pilotage=pilot)
            sailed.append((r, tr))
            total_bad += len(tr["too_tight"])
        if best is None or total_bad < best[1]:
            best = (sailed, total_bad, push)
        if total_bad == 0:
            break
        # close the water round each waypoint the ship cannot turn at, so the
        # next search swings wider there
        rad = 1
        for _, tr in sailed:
            for i in tr["too_tight"]:
                y, x = g.to_index(*tr["legs"][i])
                blocked[max(0, y - rad):y + rad + 1, max(0, x - rad):x + rad + 1] = True
        blocked &= ~keep
    search_s = time.perf_counter() - t0
    plans = []
    if best is None:
        return plans
    sailed, _, pushes = best
    for i, (r, tr) in enumerate(sailed):
        coords, turn = track_summary(tr, SA.PLANNING_SPEED_KN)
        met, enc = track_metrics(net, coords, snap)
        met.update(turn)
        met["max_turn_deg"] = max(turn_angles_deg(tr["legs"]), default=0.0)
        met["legs"] = max(len(tr["legs"]) - 1, 0)
        met["turn_ok"] = not tr["too_tight"]
        met["route_pushes"] = int(pushes)
        met["feasible"] = bool(met["feasible"] and met["turn_ok"])
        met["grid_distance_nm"] = net.path_cost(r.path)[1]
        met["planned_distance_nm"] = path_length_nm(tr["legs"])
        met["cost_effective_nm"] = r.cost
        lab = label or net.cfg.mode
        if i > 0:
            lab = f"{lab} - alternative {chr(64 + i)}"
        plans.append(RoutePlan(lab, net.cfg.mode, coords, met,
                               search=dict(r.as_row(), search_s=search_s),
                               encounters=enc, waypoints=[tuple(c) for c in tr["legs"]],
                               heading=[round(float(math.degrees(h)) % 360.0, 2)
                                        for h in tr["psi"][::3]],
                               turn_radii=[round(float(tr["radius"][k]), 1)
                                           if 0 < k < len(tr["legs"]) - 1 and abs(tr["turn"][k]) >= 1e-4
                                           else None for k in range(len(tr["legs"]))]))
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
