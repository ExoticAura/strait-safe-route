import math

import numpy as np
import pytest

from saferoute import network as NW, routing as R, study_area as SA
from saferoute.algorithms import astar, dijkstra

PAIRS = [("EAST", "SGPPT"), ("WEST", "SGKEP"), ("SGTUA", "EAST"), ("SGPPT", "WEST")]


def test_costs_never_below_distance(nets):
    for net in nets.values():
        assert np.all(net.cost >= net.length_nm - 1e-12)


@pytest.mark.parametrize("o,d", PAIRS)
def test_astar_equals_dijkstra(nets, o, d):
    net = nets["safest"]
    s, t = net.node_at(*SA.port(o).approach), net.node_at(*SA.port(d).approach)
    a, b = astar(net, s, t), dijkstra(net, s, t)
    assert a.found and b.found
    assert abs(a.cost - b.cost) < 1e-6
    assert a.nodes_expanded < b.nodes_expanded


@pytest.mark.parametrize("o,d", PAIRS)
def test_every_mode_is_feasible(nets, snap, o, d):
    for mode, net in nets.items():
        p = R.plan_route(net, o, d, snap)[0]
        m = p.metrics
        assert m["feasible"], (mode, o, d, m)
        assert m["on_land"] == 0 and m["shallow_nm"] == 0 and m["hard_breaches"] == 0


def test_safest_meets_fewer_ships_than_shortest(nets, snap):
    tot = {m: 0 for m in ("shortest", "safest")}
    for o, d in SA.OD_PAIRS:
        for m in tot:
            tot[m] += R.plan_route(nets[m], o, d, snap)[0].metrics["obstacles_met"]
    assert tot["safest"] < tot["shortest"]


def test_no_wrong_way_in_lanes(nets, snap):
    net = nets["shortest"]
    g = net.grid
    for o, d in [("EAST", "SGPPT"), ("WEST", "SGKEP")]:
        p = R.plan_route(net, o, d, snap)[0]
        from saferoute.geo import densify, initial_bearing_deg
        c = p.coords
        for a, b in zip(c[:-1], c[1:]):
            brg = initial_bearing_deg(*a, *b)
            for q in densify([a, b], 0.1)[1:-1]:
                iy, ix = g.to_index(*q)
                if g.tss_zone[iy, ix] in (1, 3):
                    dev = abs((brg - g.tss_heading[iy, ix] + 180) % 360 - 180)
                    assert dev <= 113, (o, d, q, brg)


def test_deep_draught_blocked_from_shallow_terminal(grid, snap):
    from saferoute import cache
    net = cache.network("ULCS", "shortest", "synthetic", quiet=True)
    from saferoute.serve import _endpoint_check
    assert _endpoint_check(net, "SGPPT") is not None          # 16 m pocket < 16.5 m + UKC
    assert _endpoint_check(net, "SGTUA") is None               # Tuas is dredged deeper


def test_ignoring_ships_gives_shorter_or_equal_route(snap, grid):
    from saferoute import cache
    full = cache.network("PANAMAX", "shortest", "synthetic", quiet=True)
    none = NW.build_network(grid, "PANAMAX", NW.config_for("shortest"), None)
    a = R.plan_route(full, "EAST", "SGPPT", snap)[0].metrics["grid_distance_nm"]
    b = R.plan_route(none, "EAST", "SGPPT", None)[0].metrics["grid_distance_nm"]
    assert b <= a + 1e-9
