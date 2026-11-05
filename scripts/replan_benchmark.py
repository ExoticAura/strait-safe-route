"""How fast can a route be re-planned as the traffic moves?

    python scripts/replan_benchmark.py                       # simulated traffic
    python scripts/replan_benchmark.py --ais 2025-11-01.csv  # replay a real AIS day
    options: --cycles 10,30  --vessel PANAMAX  --mode safest  --moving 40  --out outputs

The supervisor's requirement: re-generate the route every 10-30 seconds as
vessel positions change, on a normal workstation.

Each run sails our ship along every entrance <-> terminal route (16 trips).
Every cycle (10 s or 30 s of ship time) it:

  1. takes the traffic picture at that moment
       * simulated: the synthetic anchored ships, which yaw slowly with the
         tide, plus `--moving` ships sailing the TSS lanes at 10-16 kn
       * --ais: each vessel's last report in the 10 min before that moment,
         replayed from the file; anchored / moored ships plan round their
         swing circle, ships under way round their hull plus 100 m
  2. rebuilds the obstacle field (clearance from every cell to every ship)
  3. rebuilds the graph for our ship (board, edge costs)
  4. searches (A*), smooths, sails and measures a new route from where our
     ship is now to the destination berth

and times each step. Our ship then sails the new route for one cycle.

Ships under way are planned round where they are NOW: predicting where they
will be (CPA / TCPA) is the next step, and is not timed here.

Writes outputs/s4_replan_cycles.csv (one row per cycle) and
outputs/s4_replan_summary.csv (median / 95th percentile / worst per step).
"""
from __future__ import annotations

import argparse
import math
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

import numpy as np                                                    # noqa: E402
import pandas as pd                                                   # noqa: E402

from saferoute import cache, network as NW, obstacles as O, study_area as SA  # noqa: E402
from saferoute.ais import _offset_centreline                         # noqa: E402
from saferoute.geo import M_PER_NM, haversine_nm, initial_bearing_deg  # noqa: E402
from saferoute.routing import plan_route                              # noqa: E402

KN = M_PER_NM / 3600.0          # m/s per knot


# ------------------------------------------------------------- traffic
class SimTraffic:
    """Anchored ships yawing with the tide, plus ships sailing the lanes."""

    def __init__(self, grid, n_moving: int, seed: int = 7):
        self.base = cache.snapshot("synthetic", quiet=True).ships
        rng = np.random.default_rng(seed)
        self.lanes = {}
        for side in ("N", "S"):                      # N = westbound, S = eastbound
            off = SA.TSS_SEPARATION_HALF_WIDTH_NM + SA.TSS_LANE_WIDTH_NM / 2
            pts = _offset_centreline(side, off)
            if side == "N":
                pts = pts[::-1]
            seg = [float(haversine_nm(*a, *b)) for a, b in zip(pts[:-1], pts[1:])]
            self.lanes[side] = (pts, np.concatenate([[0.0], np.cumsum(seg)]))
        self.movers = []
        for k in range(n_moving):
            side = "N" if k % 2 else "S"
            L = self.lanes[side][1][-1]
            loa = float(rng.choice([150, 200, 260, 300, 335]))
            self.movers.append(dict(side=side, s0=float(rng.uniform(0, L)),
                                    sog=float(rng.uniform(10, 16)), loa=loa, id=f"MOV-{k + 1:03d}"))

    def _along(self, side, s):
        pts, cum = self.lanes[side]
        s = s % cum[-1]
        i = int(np.searchsorted(cum, s, side="right")) - 1
        i = min(max(i, 0), len(pts) - 2)
        t = (s - cum[i]) / max(cum[i + 1] - cum[i], 1e-9)
        a, b = pts[i], pts[i + 1]
        return a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, initial_bearing_deg(*a, *b)

    def at(self, t_s: float) -> O.Snapshot:
        ships = []
        yaw = 12.0 * math.sin(2 * math.pi * t_s / (6.2 * 3600))   # tide turns the fleet
        for s in self.base:
            ships.append(O.ShipObstacle(**{**s.__dict__, "heading_deg": (s.heading_deg + yaw) % 360}))
        for m in self.movers:
            la, lo, hdg = self._along(m["side"], m["s0"] + m["sog"] * t_s / 3600.0)
            ships.append(O.ShipObstacle(id=m["id"], lat=la, lon=lo, loa_m=m["loa"], beam_m=m["loa"] / 7,
                                        draught_m=12.0, heading_deg=hdg, ship_type="container",
                                        status="under way", sog_kn=m["sog"], source="simulated"))
        return O.Snapshot(ships, "simulated")


class AisReplay:
    """Each vessel's last report in the `window_min` before a moment."""

    def __init__(self, path: str, grid, window_min: float = 10.0):
        df = pd.read_csv(path)
        c = {k: O._pick(df, k) for k in O.ALIASES}
        df["_t"] = pd.to_datetime(df[c["time"]], errors="coerce", utc=True)
        self.df, self.c, self.grid, self.win = df.dropna(subset=["_t"]).sort_values("_t"), c, grid, window_min
        self.t0 = self.df["_t"].min()
        self.span_s = (self.df["_t"].max() - self.t0).total_seconds()

    def at(self, t_s: float) -> O.Snapshot:
        T = self.t0 + pd.Timedelta(seconds=t_s)
        stat = O.snapshot_from_ais_table(self.df, at=T, window_min=self.win, grid=self.grid)
        c, d = self.c, self.df
        d = d[(d["_t"] <= T) & (d["_t"] >= T - pd.Timedelta(minutes=self.win))]
        last = d.groupby(c["mmsi"], sort=False).tail(1) if c["mmsi"] else d
        have = {s.mmsi for s in stat.ships}
        ships = list(stat.ships)
        for r in last.to_dict("records"):
            mmsi = r.get(c["mmsi"]) if c["mmsi"] else None
            if mmsi in have:
                continue
            la, lo = float(r[c["lat"]]), float(r[c["lon"]])
            if not self.grid.inside(la, lo):
                continue
            hdg = O._f(r.get(c["heading"])) if c["heading"] else None
            if hdg is None or hdg >= 360:
                hdg = O._f(r.get(c["cog"]), 0.0) if c["cog"] else 0.0
            loa = O._f(r.get(c["length"])) if c["length"] else None
            ships.append(O.ShipObstacle(id=str(mmsi), lat=la, lon=lo, loa_m=loa or 150.0,
                                        beam_m=(loa or 150.0) / 7, draught_m=10.0, heading_deg=hdg,
                                        status="under way", mmsi=mmsi, source="ais",
                                        sog_kn=O._f(r.get(c["sog"]), 0.0) if c["sog"] else 0.0))
        return O.Snapshot(ships, "ais")


# ------------------------------------------------------------- one voyage
def sail_along(coords, metres):
    """Point `metres` along a (lat, lon) polyline, and whether we ran off its end."""
    left = metres
    for a, b in zip(coords[:-1], coords[1:]):
        d = float(haversine_nm(*a, *b)) * M_PER_NM
        if d >= left:
            t = left / max(d, 1e-9)
            return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t), False
        left -= d
    return tuple(coords[-1]), True


def voyage(grid, traffic, vessel, mode, origin, dest, cycle_s, max_cycles, t_start):
    rows = []
    cfg = NW.config_for(mode)
    here, t, k = origin, t_start, 0
    speed = SA.PLANNING_SPEED_KN * KN
    # a ship leaving a berth is on the fixed pilotage track, not on the board:
    # until it has sailed that track the plan starts from the terminal itself
    pilot_m = 0.0
    if isinstance(origin, str) and origin in SA.TERMINALS:
        pilot_m = SA.PORTS[origin].pilot_nm * M_PER_NM if hasattr(SA.PORTS[origin], "pilot_nm") else \
            sum(float(haversine_nm(*a, *b)) for a, b in zip(SA.PORTS[origin].pilot[:-1], SA.PORTS[origin].pilot[1:])) * M_PER_NM
    done_m = 0.0
    while k < max_cycles:
        t0 = time.perf_counter()
        snap = traffic.at(t)
        t1 = time.perf_counter()
        field = O.obstacle_field(grid, snap, cfg.swing)
        t2 = time.perf_counter()
        net = NW.build_network(grid, vessel, cfg, field)
        t3 = time.perf_counter()
        plans = plan_route(net, origin if done_m < pilot_m else here, dest, snap)
        t4 = time.perf_counter()
        ok = bool(plans)
        m = plans[0].metrics if ok else {}
        rows.append(dict(origin=origin if isinstance(origin, str) else "", destination=dest,
                         cycle_s=cycle_s, step=k, sim_time_s=t, ships=len(snap.ships),
                         moving=sum(s.status == "under way" for s in snap.ships),
                         traffic_ms=(t1 - t0) * 1e3, field_ms=(t2 - t1) * 1e3, graph_ms=(t3 - t2) * 1e3,
                         route_ms=(t4 - t3) * 1e3, total_ms=(t4 - t0) * 1e3,
                         found=ok, feasible=m.get("feasible"), remaining_nm=m.get("distance_nm"),
                         ships_met=m.get("obstacles_met"), min_hull_nm=m.get("min_hull_dist_nm"),
                         nodes_expanded=plans[0].search.get("nodes_expanded") if ok else None))
        if not ok:
            break
        here, arrived = sail_along(plans[0].coords, speed * cycle_s + (done_m if done_m < pilot_m else 0.0))
        done_m += speed * cycle_s
        t += cycle_s
        k += 1
        if arrived or m["distance_nm"] < 1.0:
            break
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--ais")
    ap.add_argument("--cycles", default="10,30")
    ap.add_argument("--vessel", default="PANAMAX")
    ap.add_argument("--mode", default="safest")
    ap.add_argument("--moving", type=int, default=40, help="simulated ships under way")
    ap.add_argument("--steps", type=int, default=12, help="re-plans per trip and cycle length")
    ap.add_argument("--out", default=os.path.join(ROOT, "outputs"))
    a = ap.parse_args(argv)
    grid = cache.grid(quiet=True)
    traffic = AisReplay(a.ais, grid) if a.ais else SimTraffic(grid, a.moving)
    v = SA.vessel(a.vessel)
    pairs = [(o, d) for o, d in SA.OD_PAIRS
             if SA.PORTS[o if o in SA.TERMINALS else d].max_draught_m >= v.draught_m * 1.1]
    rows = []
    for cyc in [float(x) for x in a.cycles.split(",")]:
        for i, (o, d) in enumerate(pairs):
            # start each trip at a different time of day so the traffic differs
            t_start = (i * 1800.0) % max(getattr(traffic, "span_s", 86400.0) - a.steps * cyc, 1.0)
            rows += voyage(grid, traffic, a.vessel, a.mode, o, d, cyc, a.steps, t_start)
            r = rows[-1]
            print(f"cycle {cyc:4.0f} s  {o:5s}->{d:5s}  re-plans {r['step'] + 1:3d}  "
                  f"last {r['total_ms']:6.0f} ms  ships {r['ships']} ({r['moving']} moving)", flush=True)
    df = pd.DataFrame(rows)
    os.makedirs(a.out, exist_ok=True)
    df.to_csv(os.path.join(a.out, "s4_replan_cycles.csv"), index=False)
    steps = ["traffic_ms", "field_ms", "graph_ms", "route_ms", "total_ms"]
    summ = []
    for cyc, g in df.groupby("cycle_s"):
        for s in steps:
            summ.append(dict(cycle_s=cyc, step=s.replace("_ms", ""), n=len(g),
                             median_ms=round(g[s].median(), 1), p95_ms=round(g[s].quantile(.95), 1),
                             worst_ms=round(g[s].max(), 1),
                             share_of_cycle_worst=round(g[s].max() / (cyc * 1e3), 3)))
    sm = pd.DataFrame(summ)
    sm.to_csv(os.path.join(a.out, "s4_replan_summary.csv"), index=False)
    print()
    print(sm.to_string(index=False))
    print(f"\nre-plans: {len(df)}; route found {df.found.mean():.0%}; "
          f"feasible {df.feasible.fillna(False).astype(bool).mean():.0%}; "
          f"source: {'AIS replay ' + a.ais if a.ais else f'simulated ({a.moving} moving ships)'}")


if __name__ == "__main__":
    main()
