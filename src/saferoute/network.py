"""Navigational network construction, step 2: board + ships -> weighted graph.

Nodes are navigable cell centres; edges are moves to the 16 neighbours (the 8
king moves plus the 8 knight moves, which halve the worst-case heading error of
a lattice from 22.5 to 11.25 degrees). The graph is directed because the TSS
lanes are one-way.

A cell is a node only if the ship can physically and legally be there:

    land                      no
    depth < draught + UKC     no     (UKC: 3.5 m in the TSS - IMO rules for the
                                      Straits of Malacca and Singapore - and 10 %
                                      of draught, min 1 m, elsewhere)
    too close to shore        no     (max(0.08 NM, half a ship length))
    inside a ship's envelope  no     (swing circle + own half-beam + the hard
                                      CPA of the zone: 0.30 NM TSS, 0.20 NM
                                      approaches, 0.05 NM basins)

Edge cost (effective nautical miles):

    c(u, v) = d(u, v) * (1 + w_ship p_ship + w_shore p_shore + w_depth p_depth
                           + w_tss p_tss)

Every penalty p is >= 0, so c >= d and the straight-line heuristic of A* stays
admissible. The mode decides the weights:

    shortest   w_ship = w_shore = w_depth = 0: the shortest route that still
               respects every hard limit and the TSS rules
    balanced   moderate aversion to passing ships inside their target CPA
    safest     strong aversion: will sail noticeably further to keep every
               stationary ship outside its target passing distance
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
from scipy import ndimage

from . import study_area as SA
from .geo import M_PER_NM, haversine_nm
from .obstacles import ObstacleField
from .seagrid import CPA_ZONE_KEYS, SeaGrid

NEIGHBOURHOOD_8 = [(-1, -1), (-1, 0), (-1, 1), (0, -1),
                   (0, 1), (1, -1), (1, 0), (1, 1)]
NEIGHBOURHOOD_16 = NEIGHBOURHOOD_8 + [(-2, -1), (-2, 1), (-1, -2), (-1, 2),
                                      (1, -2), (1, 2), (2, -1), (2, 1)]

# Board cell codes (for the "board" chart layer and for reporting).
FREE, LAND, SHALLOW, SHORE, SHIP_HARD, SHIP_SOFT = 0, 1, 2, 3, 4, 5
BOARD_LABELS = {FREE: "free water", LAND: "land", SHALLOW: "too shallow",
                SHORE: "too close to shore", SHIP_HARD: "ship exclusion",
                SHIP_SOFT: "inside a ship's target CPA"}


@dataclass
class WeightConfig:
    mode: str = "balanced"
    w_ship: float = 1.5
    w_shore: float = 0.30
    w_depth: float = 0.35
    w_tss: float = 1.60
    # Fixed cost, in NM, for each stationary ship whose target-CPA ring the
    # route enters: this is what makes "fewest ships met" part of the objective
    # rather than only the depth of each incursion.
    w_entry: float = 1.0
    # hard feasibility
    ukc_tss_m: float = 3.5
    ukc_fraction: float = 0.10
    ukc_min_m: float = 1.0
    min_shore_nm: float = 0.08
    shore_per_loa: float = 0.5
    # ship obstacles
    swing: bool = True
    cpa_scale: float = 1.0          # multiplies both hard and target CPAs
    # TSS behaviour
    tss_wrong_way_deg: float = 112.5
    tss_cross_tolerance_deg: float = 35.0
    inshore_transit_penalty: float = 1.40
    wrong_side_penalty: float = 3.00

    def as_dict(self):
        return dict(self.__dict__)


def config_for(mode: str, **kw) -> WeightConfig:
    base = dict(
        shortest=dict(w_ship=0.0, w_shore=0.0, w_depth=0.0, w_entry=0.0),
        balanced=dict(w_ship=1.5, w_shore=0.30, w_depth=0.35, w_entry=1.0),
        safest=dict(w_ship=3.0, w_shore=0.80, w_depth=1.00, w_entry=8.0),
    )
    if mode not in base:
        raise KeyError(f"unknown mode {mode!r}; use shortest | balanced | safest")
    d = dict(base[mode])
    d.update(kw)
    return WeightConfig(mode=mode, **d)


MODES = ["shortest", "balanced", "safest"]


# --------------------------------------------------------------------------
@dataclass
class NavNetwork:
    grid: SeaGrid
    vessel: SA.VesselClass
    cfg: WeightConfig
    obst: Optional[ObstacleField]
    node_iy: np.ndarray
    node_ix: np.ndarray
    node_lat: np.ndarray
    node_lon: np.ndarray
    node_of_cell: np.ndarray
    indptr: np.ndarray
    indices: np.ndarray
    cost: np.ndarray
    length_nm: np.ndarray
    navigable: np.ndarray
    board: np.ndarray                    # (NY, NX) int8 cell codes
    hard_cpa: np.ndarray                 # (NY, NX) NM
    target_cpa: np.ndarray               # (NY, NX) NM
    req_depth: np.ndarray                # (NY, NX) m
    neighbourhood: int = 16
    stats: Dict[str, float] = field(default_factory=dict)

    @property
    def n_nodes(self) -> int:
        return len(self.node_lat)

    @property
    def n_edges(self) -> int:
        return len(self.indices)

    def node_at(self, lat: float, lon: float) -> int:
        iy, ix = self.grid.nearest_navigable(lat, lon, self.navigable)
        return int(self.node_of_cell[iy, ix])

    def coords(self, path: Sequence[int]) -> List[Tuple[float, float]]:
        return [(float(self.node_lat[i]), float(self.node_lon[i])) for i in path]

    def path_cost(self, path: Sequence[int]) -> Tuple[float, float]:
        cost = dist = 0.0
        for a, b in zip(path[:-1], path[1:]):
            lo, hi = self.indptr[a], self.indptr[a + 1]
            k = lo + int(np.where(self.indices[lo:hi] == b)[0][0])
            cost += self.cost[k]
            dist += self.length_nm[k]
        return cost, dist


# --------------------------------------------------------------------------
def required_depth(grid: SeaGrid, vessel: SA.VesselClass, cfg: WeightConfig) -> np.ndarray:
    in_tss = (grid.tss_zone >= 1) & (grid.tss_zone <= 3)
    ukc_else = max(cfg.ukc_min_m, cfg.ukc_fraction * vessel.draught_m)
    return vessel.draught_m + np.where(in_tss, cfg.ukc_tss_m, ukc_else)


def required_shore_nm(vessel: SA.VesselClass, cfg: WeightConfig) -> float:
    return max(cfg.min_shore_nm, cfg.shore_per_loa * vessel.loa_m / M_PER_NM)


def cpa_arrays(grid: SeaGrid, cfg: WeightConfig) -> Tuple[np.ndarray, np.ndarray]:
    hard = np.zeros(grid.shape)
    target = np.zeros(grid.shape)
    for z, key in CPA_ZONE_KEYS.items():
        r = SA.CPA_RULES[key]
        m = grid.cpa_zone == z
        hard[m] = r.hard_nm * cfg.cpa_scale
        target[m] = r.target_nm * cfg.cpa_scale
    return hard, target


def build_board(grid: SeaGrid, vessel: SA.VesselClass, cfg: WeightConfig,
                obst: Optional[ObstacleField]):
    """The puzzle board for this ship: every cell classified, plus the fields."""
    req_d = required_depth(grid, vessel, cfg)
    req_s = required_shore_nm(vessel, cfg)
    hard, target = cpa_arrays(grid, cfg)
    half_beam = 0.5 * vessel.beam_m / M_PER_NM

    # A straight leg between two cell centres can pass up to half a cell
    # diagonal closer to a ship than either centre, so the hard ring is widened
    # by that much: what is checked on the lattice then holds on the track.
    dy_nm, dx_nm = grid.cell_size_nm()
    lattice_margin = 0.5 * math.hypot(dy_nm, dx_nm)
    board = np.full(grid.shape, FREE, dtype=np.int8)
    if obst is not None:
        board[obst.clear_nm - half_beam < target] = SHIP_SOFT
        board[obst.clear_nm - half_beam < hard + lattice_margin] = SHIP_HARD
    # Same lattice argument for depth and shore: a cell is only deep enough if
    # its neighbours are too, since a leg between centres crosses them.
    depth_min = ndimage.minimum_filter(grid.depth_m, size=3, mode="nearest")
    if grid.charted is not None:
        # MPA-charted cells already take the depth over most of the cell from
        # 6 x 6 sample points (mpa.depth_raster) and every edge stays inside
        # navigable cells (knight moves need both cells they cross), so the
        # neighbour minimum would only close dredged channels 2-3 cells wide.
        depth_min = np.where(grid.charted, grid.depth_m, depth_min)
    board[grid.dist_to_land_nm - lattice_margin < req_s] = SHORE
    board[depth_min < req_d] = SHALLOW
    board[grid.land] = LAND
    return board, hard, target, req_d


def largest_component(mask: np.ndarray, seeds: Sequence[Tuple[int, int]]) -> np.ndarray:
    lab, _ = ndimage.label(mask, structure=np.ones((3, 3), dtype=int))
    keep = {int(lab[iy, ix]) for iy, ix in seeds if lab[iy, ix] > 0}
    if not keep:
        return mask
    return np.isin(lab, list(keep))


# --------------------------------------------------------------------------
def _tss_penalty(zone_u, zone_v, head_u, head_v, bearing, cfg: WeightConfig):
    """Penalty and feasibility of a directed move under COLREGs Rule 10."""
    n = len(bearing)
    pen = np.zeros(n)
    allowed = np.ones(n, dtype=bool)
    in_lane_u = (zone_u == 1) | (zone_u == 3)
    in_lane_v = (zone_v == 1) | (zone_v == 3)
    same_lane = in_lane_u & in_lane_v & (zone_u == zone_v)
    in_band = (zone_u == 4) | (zone_v == 4)

    if same_lane.any():
        idx = np.where(same_lane)[0]
        req = np.where(np.isnan(head_u[idx]), head_v[idx], head_u[idx])
        dev = np.abs((bearing[idx] - req + 180.0) % 360.0 - 180.0)
        wrong = dev > cfg.tss_wrong_way_deg
        allowed[idx[wrong]] = False                   # against the lane: forbidden
        ok = idx[~wrong]
        pen[ok] = (dev[~wrong] / cfg.tss_wrong_way_deg) ** 2

    if in_band.any():
        idx = np.where(in_band)[0]
        ref = np.where(np.isnan(head_u[idx]), head_v[idx], head_u[idx])
        have = ~np.isnan(ref)
        sub = idx[have]
        if len(sub):
            d = np.radians((bearing[sub] - ref[have] + 180.0) % 360.0 - 180.0)
            # Penalise the along-scheme COMPONENT of the move. A threshold on
            # the angle instead lets the search zigzag at just over the
            # threshold and run the band for free; with |cos| a zigzag always
            # costs more per mile made good than a straight run.
            frac = np.abs(np.cos(d))
            pen[sub] = np.maximum(pen[sub], cfg.inshore_transit_penalty * frac)
            # Running the band against the flow of the adjacent lane is sailing
            # on the wrong side of the scheme. It is priced heavily rather than
            # forbidden: COLREGs Rule 10(d) lets a ship bound to or from a port
            # inside the zone use it, so a short leg out of a terminal pocket
            # must stay possible, while a through-transit never pays.
            against = np.degrees(np.abs(d)) > cfg.tss_wrong_way_deg
            pen[sub[against]] = np.maximum(pen[sub[against]],
                                           cfg.wrong_side_penalty * frac[against])

    crossing = (in_lane_u | in_lane_v | (zone_u == 2) | (zone_v == 2)) & ~same_lane
    if crossing.any():
        idx = np.where(crossing)[0]
        ref = np.where(np.isnan(head_u[idx]), head_v[idx], head_u[idx])
        have = ~np.isnan(ref)
        sub = idx[have]
        if len(sub):
            d = np.abs((bearing[sub] - ref[have] + 180.0) % 360.0 - 180.0)
            slack = np.maximum(np.abs(d - 90.0) - cfg.tss_cross_tolerance_deg, 0.0)
            pen[sub] = np.maximum(pen[sub], 0.40 + 1.60 * (slack / 55.0) ** 2)

    in_sep = (zone_u == 2) | (zone_v == 2)
    if in_sep.any():
        idx = np.where(in_sep)[0]
        ref = np.where(np.isnan(head_u[idx]), head_v[idx], head_u[idx])
        have = ~np.isnan(ref)
        base = np.full(len(idx), 1.20)
        if have.any():
            d = np.abs((bearing[idx[have]] - ref[have] + 180.0) % 360.0 - 180.0)
            base[have] = 0.80 + 3.00 * (np.abs(d - 90.0) / 90.0) ** 2
        pen[idx] = np.maximum(pen[idx], base)
    return pen, allowed


def _knight_clear(mask, iy, ix, dy, dx):
    """A 2:1 move may not hop a one-cell isthmus or a ship's envelope."""
    if abs(dy) <= 1 and abs(dx) <= 1:
        if dy != 0 and dx != 0:            # diagonal: no corner cutting
            ok = np.ones(len(iy), dtype=bool)
            for my, mx in ((dy, 0), (0, dx)):
                yy, xx = iy + my, ix + mx
                inside = (yy >= 0) & (yy < mask.shape[0]) & (xx >= 0) & (xx < mask.shape[1])
                got = np.zeros(len(iy), dtype=bool)
                got[inside] = mask[yy[inside], xx[inside]]
                ok &= got
            return ok
        return np.ones(len(iy), dtype=bool)
    sy, sx = int(np.sign(dy)), int(np.sign(dx))
    ok = np.ones(len(iy), dtype=bool)
    for my, mx in {(sy, 0), (0, sx), (sy, sx), (dy - sy, dx - sx)}:
        yy, xx = iy + my, ix + mx
        inside = (yy >= 0) & (yy < mask.shape[0]) & (xx >= 0) & (xx < mask.shape[1])
        got = np.zeros(len(iy), dtype=bool)
        got[inside] = mask[yy[inside], xx[inside]]
        ok &= got
    return ok


def _penalties(grid, net_fields, cfg, iy, ix):
    """Per-point penalty terms, shared by the graph build and segment evaluation."""
    clear_ship, hard, target, req_d, req_s, half_beam = net_fields
    if clear_ship is not None:
        c = clear_ship[iy, ix] - half_beam
        h, t = hard[iy, ix], target[iy, ix]
        p_ship = np.clip((t - c) / np.maximum(t - h, 1e-6), 0.0, 1.0) ** 2
    else:
        p_ship = np.zeros(len(iy))
    p_shore = np.clip(np.exp(-(grid.dist_to_land_nm[iy, ix] - req_s) / 0.30), 0.0, 1.0)
    margin = (grid.depth_m[iy, ix] - req_d[iy, ix]) / np.maximum(req_d[iy, ix], 1.0)
    p_depth = np.clip(np.exp(-margin / 0.20), 0.0, 1.0)
    return p_ship, p_shore, p_depth


def build_network(grid: SeaGrid, vessel_code: str = SA.DEFAULT_VESSEL,
                  cfg: Optional[WeightConfig] = None,
                  obst: Optional[ObstacleField] = None,
                  neighbourhood: int = 16,
                  seed_ports: Optional[Sequence[str]] = None) -> NavNetwork:
    import time
    t0 = time.perf_counter()
    cfg = cfg or config_for("balanced")
    vessel = SA.vessel(vessel_code)
    offsets = NEIGHBOURHOOD_16 if neighbourhood == 16 else NEIGHBOURHOOD_8

    board, hard, target, req_d = build_board(grid, vessel, cfg, obst)
    mask = np.isin(board, (FREE, SHIP_SOFT))

    seeds = []
    for code in (seed_ports or list(SA.PORTS)):
        p = SA.PORTS[code]
        try:
            seeds.append(grid.nearest_navigable(*p.approach, mask))
        except ValueError:
            pass
    mask = largest_component(mask, seeds)

    node_of_cell = np.full(grid.shape, -1, dtype=np.int32)
    iy_all, ix_all = np.nonzero(mask)
    node_of_cell[iy_all, ix_all] = np.arange(len(iy_all), dtype=np.int32)
    node_lat, node_lon = grid.lats[iy_all], grid.lons[ix_all]
    n = len(iy_all)

    req_s = required_shore_nm(vessel, cfg)
    half_beam = 0.5 * vessel.beam_m / M_PER_NM
    fields = (obst.clear_nm if obst is not None else None, hard, target, req_d,
              req_s, half_beam)
    ps, pl, pd = _penalties(grid, fields, cfg, iy_all, ix_all)
    w_node = cfg.w_ship * ps + cfg.w_shore * pl + cfg.w_depth * pd
    zone = grid.tss_zone[iy_all, ix_all]
    head = grid.tss_heading[iy_all, ix_all]
    if obst is not None:
        inside = (obst.clear_nm - half_beam < target)[iy_all, ix_all]
        near = obst.nearest[iy_all, ix_all]
    else:
        inside = np.zeros(n, dtype=bool)
        near = np.full(n, -1)

    S, D, C, Lg = [], [], [], []
    for dy, dx in offsets:
        ty, tx = iy_all + dy, ix_all + dx
        inb = (ty >= 0) & (ty < grid.ny) & (tx >= 0) & (tx < grid.nx)
        cand = np.zeros(n, dtype=bool)
        cand[inb] = mask[ty[inb], tx[inb]]
        cand &= _knight_clear(mask, iy_all, ix_all, dy, dx)
        u = np.nonzero(cand)[0]
        if not len(u):
            continue
        v = node_of_cell[ty[u], tx[u]]
        d_nm = haversine_nm(node_lat[u], node_lon[u], node_lat[v], node_lon[v])
        bearing = (np.degrees(np.arctan2(
            np.radians(node_lon[v] - node_lon[u]) * np.cos(np.radians(node_lat[u])),
            np.radians(node_lat[v] - node_lat[u]))) + 360.0) % 360.0
        p_tss, allowed = _tss_penalty(zone[u], zone[v], head[u], head[v], bearing, cfg)
        w = 1.0 + 0.5 * (w_node[u] + w_node[v]) + cfg.w_tss * p_tss
        entry = inside[v] & (~inside[u] | (near[u] != near[v]))
        c = d_nm * w + cfg.w_entry * entry
        k = allowed
        S.append(u[k]); D.append(v[k]); C.append(c[k]); Lg.append(d_nm[k])

    src = np.concatenate(S); dst = np.concatenate(D)
    cost = np.concatenate(C); length = np.concatenate(Lg)
    order = np.argsort(src, kind="stable")
    src, dst, cost, length = src[order], dst[order], cost[order], length[order]
    indptr = np.zeros(n + 1, dtype=np.int64)
    np.add.at(indptr, src + 1, 1)
    indptr = np.cumsum(indptr)
    stats = dict(build_s=time.perf_counter() - t0,
                 cells=int(board.size),
                 **{f"cells_{BOARD_LABELS[k].replace(' ', '_').replace(chr(39), '')}":
                    int((board == k).sum()) for k in BOARD_LABELS})
    return NavNetwork(grid=grid, vessel=vessel, cfg=cfg, obst=obst,
                      node_iy=iy_all, node_ix=ix_all, node_lat=node_lat,
                      node_lon=node_lon, node_of_cell=node_of_cell, indptr=indptr,
                      indices=dst.astype(np.int32), cost=cost, length_nm=length,
                      navigable=mask, board=board, hard_cpa=hard,
                      target_cpa=target, req_depth=req_d,
                      neighbourhood=neighbourhood, stats=stats)


# --------------------------------------------------------------------------
# continuous legs: evaluation and smoothing
# --------------------------------------------------------------------------
def _sample(a, b, step_nm):
    d = float(haversine_nm(a[0], a[1], b[0], b[1]))
    n = max(2, int(math.ceil(d / step_nm)) + 1)
    t = np.linspace(0.0, 1.0, n)
    return a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, d


def segment_cost(net: NavNetwork, a, b, short_leg_factor: float = 1.6):
    """(cost, distance, feasible) of a straight leg, under the graph's objective."""
    g = net.grid
    dy_nm, dx_nm = g.cell_size_nm()
    # MPA-charted water has no neighbour-depth margin (build_board), so a leg
    # there is checked as finely as the route metrics sample the track
    fine = g.charted is not None
    step = (0.05 if fine else 0.4) * min(dy_nm, dx_nm)
    lat, lon, d = _sample(a, b, step)
    if d <= 1e-9:
        return 0.0, 0.0, True
    iy, ix = g.index_arrays(lat, lon)
    short = d <= short_leg_factor * math.hypot(dy_nm, dx_nm)
    if fine:
        ch = g.charted[iy, ix]
        if not net.navigable[iy[ch], ix[ch]].all():
            return math.inf, d, False
    if short:
        iy, ix = iy[[0, -1]], ix[[0, -1]]
    elif not net.navigable[iy, ix].all():
        return math.inf, d, False
    cfg = net.cfg
    fields = (net.obst.clear_nm if net.obst is not None else None, net.hard_cpa,
              net.target_cpa, net.req_depth,
              required_shore_nm(net.vessel, cfg), 0.5 * net.vessel.beam_m / M_PER_NM)
    ps, pl, pd = _penalties(g, fields, cfg, iy, ix)
    bearing = (math.degrees(math.atan2(
        math.radians(b[1] - a[1]) * math.cos(math.radians(a[0])),
        math.radians(b[0] - a[0]))) + 360.0) % 360.0
    zone, head = g.tss_zone[iy, ix], g.tss_heading[iy, ix]
    p_tss, allowed = _tss_penalty(zone, zone, head, head, np.full(len(iy), bearing), cfg)
    if not short and not allowed.all():
        return math.inf, d, False
    w = 1.0 + cfg.w_ship * ps + cfg.w_shore * pl + cfg.w_depth * pd + cfg.w_tss * p_tss
    entries = 0
    if net.obst is not None and cfg.w_entry > 0 and len(iy) > 1:
        ins = (net.obst.clear_nm[iy, ix] - fields[5]) < net.target_cpa[iy, ix]
        nr = net.obst.nearest[iy, ix]
        entries = int(np.sum(ins[1:] & (~ins[:-1] | (nr[1:] != nr[:-1]))))
    return float(np.mean(0.5 * (w[:-1] + w[1:])) * d) + cfg.w_entry * entries, d, True


def smooth_path(net: NavNetwork, coords: Sequence[Tuple[float, float]],
                tolerance: float = 1.002, max_lookahead: int = 256):
    """String-pull a lattice path into straight legs (Theta*-style post-pass).

    A shortcut is taken only if it is feasible and no more expensive under the
    same objective the search minimised, so smoothing can never turn a legal
    route into one the model would have rejected.
    """
    coords = [tuple(c) for c in coords]
    if len(coords) < 3:
        return coords
    prefix = [0.0]
    for a, b in zip(coords[:-1], coords[1:]):
        c, _, ok = segment_cost(net, a, b)
        prefix.append(prefix[-1] + (c if ok else math.inf))

    def ok_cut(i, j):
        c, _, ok = segment_cost(net, coords[i], coords[j])
        return ok and c <= (prefix[j] - prefix[i]) * tolerance

    out, i, n = [coords[0]], 0, len(coords)
    while i < n - 1:
        lo, step = i + 1, 1
        while i + 2 * step < n and step <= max_lookahead and ok_cut(i, i + 2 * step):
            lo = i + 2 * step
            step *= 2
        hi = min(n - 1, i + 2 * step)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if ok_cut(i, mid):
                lo = mid
            else:
                hi = mid - 1
        j = max(lo, i + 1)
        out.append(coords[j])
        i = j
    return out
