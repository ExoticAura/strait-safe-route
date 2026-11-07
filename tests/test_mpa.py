"""MPA chart layers (mpa.py) on a tiny synthetic chart: depth bands, dredged
areas, hazards, charted land and the berth names."""
import json
import os

import numpy as np
import pytest

from saferoute import mpa


def _fc(feats):
    return {"type": "FeatureCollection", "features": feats}


def _poly(x0, y0, x1, y1, hole=None):
    rings = [[[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]]
    if hole:
        hx0, hy0, hx1, hy1 = hole
        rings.append([[hx0, hy0], [hx0, hy1], [hx1, hy1], [hx1, hy0], [hx0, hy0]])
    return {"type": "Polygon", "coordinates": rings}


@pytest.fixture()
def chart(tmp_path, monkeypatch):
    d = tmp_path / "gss"
    def put(layer, feats):
        (d / layer).mkdir(parents=True, exist_ok=True)
        (d / layer / (mpa.LAYER_FILES[layer] + ".geojson")).write_text(json.dumps(_fc(feats)))
    put("depthsa", [
        # a 10-15 m band with an island (hole) in it, and a dredged berth pocket
        {"type": "Feature", "properties": {"DRVAL1": 10.0, "DRVAL2": 15.0, "FCSubtype": 1, "SORDAT": "20250814"},
         "geometry": _poly(103.0, 1.0, 103.02, 1.02, hole=(103.005, 1.005, 103.008, 1.008))},
        {"type": "Feature", "properties": {"DRVAL1": 16.0, "DRVAL2": 0.0, "FCSubtype": 5, "SORDAT": "20250814",
                                            "OBJNAM": "Keppel Terminal Berth K1"},
         "geometry": _poly(103.022, 1.012, 103.026, 1.016)},
        {"type": "Feature", "properties": {"DRVAL1": -2.5, "DRVAL2": 0.0, "FCSubtype": 1, "SORDAT": "20250814"},
         "geometry": _poly(103.02, 1.0, 103.022, 1.002)},
    ])
    put("dangersp", [
        {"type": "Feature", "properties": {"VALSOU": 4.2, "CATWRK_TXT": "dangerous wreck"},
         "geometry": {"type": "MultiPoint", "coordinates": [[103.0151, 1.0031]]}},
        {"type": "Feature", "properties": {"VALSOU": None, "CATOBS_TXT": "foul ground"},
         "geometry": {"type": "MultiPoint", "coordinates": [[103.0031, 1.0151]]}},
    ])
    monkeypatch.setattr(mpa, "GSS_DIR", str(d))
    monkeypatch.setattr(mpa, "DERIVED_DIR", str(d / "derived"))
    monkeypatch.setattr(mpa, "LAND_MPA", str(d / "derived" / "land_mpa.geojson"))
    monkeypatch.setenv("SAFEROUTE_MPA", "1")
    for f in (mpa.features, mpa.water, mpa.charted_land):
        f.cache_clear()
    yield d
    for f in (mpa.features, mpa.water, mpa.charted_land):
        f.cache_clear()


def test_enabled_and_switch(chart, monkeypatch):
    assert mpa.enabled()
    monkeypatch.setenv("SAFEROUTE_MPA", "0")
    assert not mpa.enabled()


def test_area_depth_uses_shallow_end_and_dries_to_zero():
    assert mpa.area_depth({"DRVAL1": 10.0, "DRVAL2": 15.0}) == 10.0
    assert mpa.area_depth({"DRVAL1": -2.8, "DRVAL2": 0.0}) == 0.0
    assert mpa.area_depth({"DRVAL1": None}) == 0.0


def test_hazard_rules():
    assert mpa.hazard_depth({"VALSOU": 7.5}) == 7.5
    assert mpa.hazard_depth({"VALSOU": None, "WATLEV_TXT": "awash"}) == 0.0
    assert mpa.hazard_depth({"VALSOU": None, "CATWRK_TXT": "dangerous wreck"}) == 0.0
    assert mpa.hazard_depth({"VALSOU": None, "CATOBS_TXT": "foul ground"}) is None


def test_depth_raster_bands_pocket_and_uncharted(chart):
    lats = np.arange(1.001, 1.03, 0.002)
    lons = np.arange(103.001, 103.03, 0.002)
    charted, depth = mpa.depth_raster(lats, lons, supersample=4)
    iy = lambda la: int(round((la - lats[0]) / 0.002))
    ix = lambda lo: int(round((lo - lons[0]) / 0.002))
    assert charted[iy(1.001), ix(103.001)] and depth[iy(1.001), ix(103.001)] == 10.0
    assert depth[iy(1.013), ix(103.023)] == 16.0                 # inside the dredged pocket
    assert not charted[iy(1.027), ix(103.027)]                   # beyond the chart
    _, dmin = mpa.depth_raster(lats, lons, supersample=4, how="min")
    assert (dmin[charted] <= depth[charted] + 1e-9).all()


def test_charted_land_is_the_hole(chart):
    from shapely.geometry import Point, box
    land = mpa.charted_land()
    assert land.contains(Point(103.0065, 1.0065))
    assert not land.contains(Point(103.001, 1.001))
    corrected = mpa.corrected_land(box(103.0, 1.0, 103.002, 1.002))   # OSM says land; chart says water
    assert not corrected.contains(Point(103.001, 1.001))
    assert corrected.contains(Point(103.0065, 1.0065))


def test_hazards_cap_cells(chart):
    lats = np.arange(1.001, 1.03, 0.002)
    lons = np.arange(103.001, 103.03, 0.002)
    hz = mpa.hazard_raster(lats, lons)
    assert np.isclose(np.nanmin(hz[np.isfinite(hz)]), 4.2)
    assert np.isfinite(hz).sum() >= 1
    kinds = {h["kind"]: h["depth"] for h in mpa.hazards()}
    assert kinds["dangerous wreck"] == 4.2 and kinds["foul ground"] is None


def test_berth_names():
    assert mpa.berth_full_name("SGKEP", "K14") == "Keppel Terminal Berth K14"
    assert mpa.berth_full_name("SGKEP", "Brani B3") == "Brani Terminal Berth B3"
    assert mpa.berth_full_name("SGPPT", "P35").endswith("Berth P35")
