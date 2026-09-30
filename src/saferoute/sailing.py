"""Sail the planned legs the way a ship actually turns.

The search and the string-pulling pass return straight legs joined by sharp
corners. A ship cannot turn on the spot, so before anything is measured the
legs are turned into the track a ship would steer, and every safety number is
taken from that track.

This follows how a passage plan is drawn on an ECDIS: each waypoint gets a
turn radius, and the ship sails the straight legs joined by circular arcs
(the arc starts at the wheel-over point).

1. Straighten. Lattice staircases (small alternating jogs left by the grid)
   are removed where the straight line between the neighbouring waypoints is
   legal and no dropped waypoint is more than `straighten_m` off it - a ship
   steers straight through them.
2. Turn radius per waypoint. The planning radius is 2.5 L (L = LOA; for a
   294 m Panamax at 12 kn that is 735 m, ~29 degrees a minute, a normal
   coastal course alteration). It is reduced where the legs are too short to
   fit the arcs, and where the arc would cut into a hard limit (land, depth +
   UKC, shore distance, a ship's hard CPA, the wrong way down a TSS lane),
   until the arc is legal.
3. The tightest radius a waypoint may use is 1.5 L at sea (about hard-over
   rudder) and 1.0 L in pilotage waters - from the pilot boarding ground the
   route passes (or 3 NM out, whichever is further) to the berth - where
   ships are under pilotage with tugs. A waypoint that needs less is
   reported (`too_tight`) and `routing.plan_route` pushes the route off the
   obstacle and searches again. The first 2 NM from a strait gateway are
   exempt: the gateway is a boundary point the ship reaches already on course.

The ship's head is not quite its course: in a turn the bow points inside the
track and the hull skids outward (drift angle, up to 5 degrees at the planning
radius), and the rudder takes about 0.4 L of way to bite, so the rate of turn
builds up and eases off. These shape the heading shown in the 3D view; the
positions measured are the track itself.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import numpy as np

from .geo import M_PER_NM, densify

KY = 110574.0


@dataclass
class TurnModel:
    radius_loa: float = 2.5          # planning turn radius
    min_radius_loa: float = 1.5      # tightest at sea (about hard-over)
    tug_radius_loa: float = 1.0      # tightest under pilotage with tugs
    pilotage_nm: float = 3.0         # from a terminal end
    shrink: float = 0.85             # radius reduction step when an arc is illegal
    straighten_m: float = 150.0      # largest jog steered straight through
    drift_deg: float = 5.0           # drift angle at the planning radius
    drift_max_deg: float = 8.0
    lag_loa: float = 0.4             # rudder lag, as distance sailed
    step_m: float = 10.0             # sample spacing of the track
    end_slack_nm: float = 0.25       # berth swing, exempt from the check
    gateway_free_nm: float = 2.0     # course at a strait gateway is free


class _Frame:
    """Local flat frame in metres (x east, y north) round the first point."""

    def __init__(self, lat0: float, lon0: float):
        self.lat0, self.lon0 = lat0, lon0
        self.kx = 111320.0 * math.cos(math.radians(lat0))

    def xy(self, pts):
        return [((lo - self.lon0) * self.kx, (la - self.lat0) * KY) for la, lo in pts]

    def ll(self, x, y):
        return self.lat0 + np.asarray(y) / KY, self.lon0 + np.asarray(x) / self.kx


def _off(p, a, b) -> float:
    vx, vy = b[0] - a[0], b[1] - a[1]
    l2 = vx * vx + vy * vy or 1e-9
    t = max(0.0, min(1.0, ((p[0] - a[0]) * vx + (p[1] - a[1]) * vy) / l2))
    return math.hypot(a[0] + vx * t - p[0], a[1] + vy * t - p[1])


def straighten(net, legs, model: TurnModel):
    """Drop waypoints that only make a small jog, where the chord is legal."""
    from .network import segment_cost
    legs = [tuple(c) for c in legs]
    if len(legs) < 3:
        return legs
    fr = _Frame(*legs[0])
    P = fr.xy(legs)
    keep = [0]
    i = 1
    while i < len(legs) - 1:
        # extend the chord from the last kept waypoint as far as it stays legal
        # and within straighten_m of every waypoint it skips
        j = i + 1
        a = keep[-1]
        if all(_off(P[m], P[a], P[j]) < model.straighten_m for m in range(a + 1, j)) \
                and segment_cost(net, legs[a], legs[j])[2]:
            i += 1                                   # legs[i] dropped
            continue
        keep.append(i)
        i += 1
    keep.append(len(legs) - 1)
    return [legs[k] for k in keep]


def _geometry(P):
    """Unit direction and length of each leg, signed turn at each waypoint."""
    u, L = [], []
    for i in range(1, len(P)):
        dx, dy = P[i][0] - P[i - 1][0], P[i][1] - P[i - 1][1]
        d = math.hypot(dx, dy) or 1e-9
        u.append((dx / d, dy / d))
        L.append(d)
    th = [0.0]
    for i in range(1, len(P) - 1):
        a1 = math.atan2(u[i - 1][0], u[i - 1][1])
        a2 = math.atan2(u[i][0], u[i][1])
        th.append(math.atan2(math.sin(a2 - a1), math.cos(a2 - a1)))   # + = to starboard
    th.append(0.0)
    return u, L, th


def _fit_radii(L, th, want):
    """Largest radii <= want whose arcs fit on the legs."""
    n = len(th)
    tan = [math.tan(abs(t) / 2.0) for t in th]
    t_len = [want[i] * tan[i] for i in range(n)]
    for _ in range(3):
        for j in range(n - 1):                        # leg j joins waypoints j and j+1
            need = t_len[j] + t_len[j + 1]
            if need > L[j]:
                f = L[j] / need
                t_len[j] *= f
                t_len[j + 1] *= f
    return [t_len[i] / tan[i] if tan[i] > 1e-9 else want[i] for i in range(n)]


def _arc(P, u, th, i, R, ds):
    """Points (x, y, course) along the arc at waypoint i with radius R."""
    t = R * math.tan(abs(th[i]) / 2.0)
    ax, ay = P[i][0] - u[i - 1][0] * t, P[i][1] - u[i - 1][1] * t
    sgn = 1.0 if th[i] > 0 else -1.0
    # centre: to starboard of the inbound leg for a turn to starboard
    cx, cy = ax + u[i - 1][1] * sgn * R, ay - u[i - 1][0] * sgn * R
    a0 = math.atan2(ax - cx, ay - cy)
    c0 = math.atan2(u[i - 1][0], u[i - 1][1])
    n = max(2, int(math.ceil(abs(th[i]) * R / ds)) + 1)
    out = []
    for k in range(n):
        f = k / (n - 1)
        a = a0 + th[i] * f
        out.append((cx + R * math.sin(a), cy + R * math.cos(a), c0 + th[i] * f))
    return out


def _points_ok(net, fr, pts, tol):
    from .network import _tss_penalty
    xs = np.array([p[0] for p in pts])
    ys = np.array([p[1] for p in pts])
    lat, lon = fr.ll(xs, ys)
    g = net.grid
    iy, ix = g.index_arrays(lat, lon)
    ok = net.navigable[iy, ix] | tol[iy, ix]
    zone, head = g.tss_zone[iy, ix], g.tss_heading[iy, ix]
    rule = lambda c: _tss_penalty(zone, zone, head, head, (np.degrees(c) + 360.0) % 360.0, net.cfg)[1]
    allowed = rule(np.array([p[2] for p in pts]))
    # Rule 10 is only enforced where the planned legs themselves obey it
    # there (a leg out of a gateway at the end of a lane may not)
    c_in, c_out = pts[0][2], pts[-1][2]
    held = rule(np.full(len(pts), c_in)) & rule(np.full(len(pts), c_out))
    return ok & (allowed | ~held)


def tolerance_mask(net, legs) -> np.ndarray:
    """Cells the planned legs themselves use (short terminal legs may cross
    cells the board closes; the sailed track is not held to more than that)."""
    g = net.grid
    dy, dx = g.cell_size_nm()
    pts = np.asarray(densify(legs, 0.25 * min(dy, dx)))
    iy, ix = g.index_arrays(pts[:, 0], pts[:, 1])
    m = np.zeros(g.shape, dtype=bool)
    m[iy, ix] = True
    return m


def sail(net, legs, model: Optional[TurnModel] = None,
         pilotage: Tuple[bool, bool] = (False, False),
         quay: Tuple[Optional[float], Optional[float]] = (None, None)) -> Dict:
    """Turn the planned legs into the sailed track. See the module doc.

    `pilotage` gives the pilotage zone at each end in NM (0 = a strait
    gateway, no pilot).
    """
    model = model or TurnModel()
    Lship = float(net.vessel.loa_m)
    legs = straighten(net, legs, model)
    fr = _Frame(*legs[0])
    P = fr.xy(legs)
    u, L, th = _geometry(P)
    n = len(P)
    cum = [0.0]
    for d in L:
        cum.append(cum[-1] + d)
    total = cum[-1]
    tol = tolerance_mask(net, legs)
    pz = model.pilotage_nm * M_PER_NM
    slack = model.end_slack_nm * M_PER_NM
    gate = model.gateway_free_nm * M_PER_NM
    floor = []
    for i in range(n):
        # `pilotage`: length (NM) of the pilotage zone at each end, from the
        # boarding point to the berth (routing.pilotage_zone_nm); 0 = none
        pilot = (pilotage[0] and cum[i] <= float(pilotage[0]) * M_PER_NM) or \
                (pilotage[1] and total - cum[i] <= float(pilotage[1]) * M_PER_NM)
        floor.append((model.tug_radius_loa if pilot else model.min_radius_loa) * Lship)
    R_plan = model.radius_loa * Lship
    R = _fit_radii(L, th, [R_plan] * n)
    too_tight = []
    for i in range(1, n - 1):
        if abs(th[i]) < 1e-4:
            continue
        r = R[i]
        exempt = cum[i] < slack or total - cum[i] < slack
        while not exempt and r >= 5.0 and not _points_ok(net, fr, _arc(P, u, th, i, r, model.step_m), tol).all():
            r *= model.shrink
        R[i] = r
        # a strait gateway is a boundary point, not a place the ship turns: it
        # arrives there already on course, so turns in its first 2 NM are free
        at_gate = (not pilotage[0] and cum[i] < gate) or (not pilotage[1] and total - cum[i] < gate)
        if r < floor[i] - 1e-6 and not at_gate:
            too_tight.append(i)

    # ---- the track: straight runs joined by arcs, sampled every step_m
    ds = model.step_m
    t_in = [R[i] * math.tan(abs(th[i]) / 2.0) if 0 < i < n - 1 and abs(th[i]) >= 1e-4 else 0.0
            for i in range(n)]
    xs, ys, chi, kap = [], [], [], []
    for j in range(n - 1):
        s0, s1 = t_in[j], L[j] - t_in[j + 1]
        c = math.atan2(u[j][0], u[j][1])
        m = max(1, int(math.ceil(max(s1 - s0, 0.0) / ds)))
        for k in range(m):
            s = s0 + (s1 - s0) * k / m
            xs.append(P[j][0] + u[j][0] * s)
            ys.append(P[j][1] + u[j][1] * s)
            chi.append(c)
            kap.append(0.0)
        i = j + 1
        if i < n - 1 and t_in[i] > 0:
            kk = math.copysign(1.0 / R[i], th[i])
            for (x, y, cc) in _arc(P, u, th, i, R[i], ds)[:-1]:
                xs.append(x)
                ys.append(y)
                chi.append(cc)
                kap.append(kk)
    xs.append(P[-1][0])
    ys.append(P[-1][1])
    chi.append(math.atan2(u[-1][0], u[-1][1]))
    kap.append(0.0)
    xs, ys, chi, kap = (np.asarray(v, dtype=float) for v in (xs, ys, chi, kap))
    d = np.concatenate([[0.0], np.cumsum(np.hypot(np.diff(xs), np.diff(ys)))])

    # ship's head: course + drift, with rudder lag on the rate of turn
    kf = np.zeros_like(kap)
    lag = model.lag_loa * Lship
    for k in range(1, len(kap)):
        kf[k] = kf[k - 1] + (kap[k] - kf[k - 1]) * min(1.0, (d[k] - d[k - 1]) / lag)
    dmax = math.radians(model.drift_max_deg)
    psi = chi + np.clip(math.radians(model.drift_deg) * R_plan * kf, -dmax, dmax)
    # alongside: over the last (first) few hundred metres the tugs swing the
    # ship parallel to the quay, bow the way she was heading
    swing = min(model.end_slack_nm * M_PER_NM, 2.0 * Lship)
    for end, q in ((0, quay[0]), (1, quay[1])):
        if q is None or not np.isfinite(q):
            continue
        k_end = -1 if end else 0
        qa = math.radians(q)
        # quay line bearing -> course angle convention used here (atan2(x, y))
        c_end = psi[k_end]
        cand = [qa, qa + math.pi]
        tgt = min(cand, key=lambda a: abs(math.atan2(math.sin(a - c_end), math.cos(a - c_end))))
        dist_end = (d[-1] - d) if end else d
        w = np.clip(1.0 - dist_end / swing, 0.0, 1.0) ** 2
        dpsi = np.arctan2(np.sin(tgt - psi), np.cos(tgt - psi))
        psi = psi + w * dpsi
    lat, lon = fr.ll(xs, ys)
    return dict(lat=lat, lon=lon, x=xs, y=ys, chi=chi, psi=psi, k=kap, kf=kf, d=d,
                length_m=float(d[-1]), total_m=total, legs=legs, legs_xy=P,
                radius=R, turn=th, floor=floor, too_tight=too_tight)


def track_summary(tr: Dict, speed_kn: float, thin: int = 3) -> Tuple[List[Tuple[float, float]], Dict]:
    """Thinned track coordinates plus the turning figures for the metrics."""
    n = len(tr["lat"])
    idx = list(range(0, n, thin))
    if idx[-1] != n - 1:
        idx.append(n - 1)
    coords = [(float(tr["lat"][i]), float(tr["lon"][i])) for i in idx]
    turns = [i for i in range(1, len(tr["turn"]) - 1) if abs(tr["turn"][i]) >= 1e-4]
    rmin = min((tr["radius"][i] for i in turns), default=None)
    m_per_min = speed_kn * M_PER_NM / 60.0
    P, xs, ys = tr["legs_xy"], tr["x"], tr["y"]
    best = np.full(n, np.inf)
    for i in range(1, len(P)):
        ax, ay = P[i - 1]
        vx, vy = P[i][0] - ax, P[i][1] - ay
        l2 = vx * vx + vy * vy or 1e-9
        t = np.clip(((xs - ax) * vx + (ys - ay) * vy) / l2, 0.0, 1.0)
        best = np.minimum(best, np.hypot(ax + vx * t - xs, ay + vy * t - ys))
    return coords, dict(
        waypoints=len(tr["legs"]),
        min_turn_radius_m=round(float(rmin), 1) if rmin is not None else None,
        max_rot_deg_min=round(math.degrees(1.0 / rmin) * m_per_min, 1) if rmin else 0.0,
        max_xtrack_m=round(float(best.max()), 1) if n else 0.0,
        tight_turns=len(tr["too_tight"]),
    )
