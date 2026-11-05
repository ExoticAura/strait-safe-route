"""The 3D viewer's extra data (scripts/export_3d_detail.py)."""
import base64
import json
import os

import numpy as np

W = os.path.join(os.path.dirname(__file__), "..", "web_static", "world")


def test_world_has_terrain_depth_shore_and_yards():
    w = json.load(open(os.path.join(W, "world.json")))
    for k in ("terrain", "depth", "shore"):
        assert k in w and os.path.exists(os.path.join(W, w[k]["file"]))
    t = w["terrain"]
    v = np.frombuffer(base64.b64decode(open(os.path.join(W, t["file"])).read()), np.uint8)
    assert v.size == t["w"] * t["h"]
    assert 40 < v.max() * t["scale_m"] < 200            # hills, not flat and not Everest
    for term in w["terrain"] and w["terminals"]:
        assert len(term["yards"]) > 100, term["code"]   # dense terminals


def test_yard_blocks_are_on_land_not_in_the_sea():
    from shapely.geometry import Point, shape
    from saferoute.seagrid import LAND_GEOJSON
    land = shape(json.load(open(LAND_GEOJSON))["features"][0]["geometry"])
    w = json.load(open(os.path.join(W, "world.json")))
    for term in w["terminals"]:
        bad = sum(not land.contains(Point(lo, la)) for la, lo, _ in term["yards"])
        assert bad == 0, (term["code"], bad)
