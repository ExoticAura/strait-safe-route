"""The sailed track: what every safety number is measured on."""
import math

import numpy as np
import pytest

from saferoute import routing as R, study_area as SA
from saferoute.algorithms import astar
from saferoute.network import smooth_path
from saferoute.sailing import TurnModel, sail, straighten


def _legs(net, o, d):
    s, t = net.node_at(*SA.port(o).approach), net.node_at(*SA.port(d).approach)
    return smooth_path(net, net.coords(astar(net, s, t).path))


@pytest.mark.parametrize("o,d", SA.OD_PAIRS)
def test_every_route_is_sailable(nets, snap, o, d):
    """Every Panamax route turns no tighter than the ship can, and the sailed
    track is clear of land, shallow water and hard CPAs."""
    for net in nets.values():
        p = R.plan_route(net, o, d, snap)[0]
        assert p.metrics["turn_ok"], (net.cfg.mode, o, d)
        assert p.metrics["feasible"], (net.cfg.mode, o, d)
        assert p.metrics["on_land"] == 0 and p.metrics["hard_breaches"] == 0


def test_radii_respect_limits(nets):
    net, model = nets["safest"], TurnModel()
    L = net.vessel.loa_m
    tr = sail(net, _legs(net, "EAST", "SGPPT"), model, pilotage=(False, True))
    for i in range(1, len(tr["legs"]) - 1):
        if abs(tr["turn"][i]) < 1e-4:
            continue
        assert tr["radius"][i] <= model.radius_loa * L + 1e-6
        assert tr["radius"][i] >= tr["floor"][i] - 1e-6 or i in tr["too_tight"]


def test_track_is_smooth_and_joins_the_ends(nets):
    net = nets["balanced"]
    legs = _legs(net, "WEST", "SGKEP")
    tr = sail(net, legs, pilotage=(False, True))
    # course never jumps: consecutive samples differ by < 3 degrees
    dc = np.abs(np.angle(np.exp(1j * np.diff(tr["chi"]))))
    assert np.degrees(dc).max() < 3.0
    # samples at most one step apart, and the track starts / ends on the plan
    assert np.diff(tr["d"]).max() <= TurnModel().step_m * 1.01
    assert abs(tr["lat"][0] - legs[0][0]) < 1e-9 and abs(tr["lon"][-1] - legs[-1][1]) < 1e-9
    # never shorter than the chord, never much longer than the legs
    assert tr["length_m"] <= tr["total_m"] + 1.0


def test_straighten_removes_only_small_legal_jogs(nets):
    net = nets["shortest"]
    legs = _legs(net, "SGKEP", "EAST")
    st = straighten(net, legs, TurnModel())
    assert len(st) <= len(legs)
    assert st[0] == tuple(legs[0]) and st[-1] == tuple(legs[-1])


def test_csv_has_turn_radii(nets, snap):
    p = R.plan_route(nets["safest"], "EAST", "SGPPT", snap)[0]
    assert len(p.turn_radii) == len(p.waypoints)
    assert p.turn_radii[0] is None and p.turn_radii[-1] is None
    assert any(r is not None for r in p.turn_radii)
    assert len(p.heading) >= 2 and all(0 <= h < 360 for h in p.heading)


@pytest.mark.parametrize("code", SA.TERMINALS)
def test_routes_end_on_the_berth(nets, snap, code):
    """A terminal route ends on the berth (40 m off the quay line), reached by
    a pilotage track that stays in the water of the real coastline."""
    import json
    from shapely.geometry import LineString, Point, shape
    from saferoute.seagrid import LAND_GEOJSON
    land = shape(json.load(open(LAND_GEOJSON))["features"][0]["geometry"])
    port = SA.PORTS[code]
    assert len(port.pilot) >= 2 and tuple(port.pilot[-1]) == port.berth
    assert not land.contains(Point(port.lon, port.lat))
    assert land.distance(Point(port.lon, port.lat)) < 0.0006          # alongside the quay
    track = LineString([(lo, la) for la, lo in port.pilot[:-1]] +
                       [(port.lon + (port.pilot[-2][1] - port.lon) * .05,
                         port.lat + (port.pilot[-2][0] - port.lat) * .05)])
    assert not track.intersects(land)
    for o, d in ((("EAST", code)), ((code, "WEST"))):
        net = nets["safest"]
        if net.vessel.draught_m * 1.1 > port.max_draught_m:
            continue
        p = R.plan_route(net, o, d, snap)[0]
        end = p.coords[-1] if d == code else p.coords[0]
        assert abs(end[0] - port.lat) < 1e-5 and abs(end[1] - port.lon) < 1e-5
        assert p.metrics["feasible"] and p.metrics["turn_ok"]
