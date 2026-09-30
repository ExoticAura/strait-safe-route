import math

import pandas as pd

from saferoute import obstacles as O
from saferoute.geo import haversine_nm


def test_synthetic_snapshot_realistic(grid, snap):
    assert 150 <= len(snap) <= 350
    assert snap.source == "synthetic"
    assert all(s.mmsi is None for s in snap.ships)             # never invent MMSIs
    for s in snap.ships:
        iy, ix = grid.to_index(s.lat, s.lon)
        assert not grid.land[iy, ix]
        assert not (1 <= grid.tss_zone[iy, ix] <= 3)          # not anchored in the lanes


def test_swing_circles_do_not_overlap_much(snap):
    env = [s.envelope() for s in snap.ships]
    for i in range(len(env)):
        for j in range(i + 1, len(env)):
            d = haversine_nm(env[i][0], env[i][1], env[j][0], env[j][1])
            assert d >= 0.89 * (env[i][2] + env[j][2])


def test_anchor_is_ahead_of_the_ship():
    s = O.ShipObstacle("x", 1.2, 103.8, 300, 40, 12, heading_deg=90, status="anchored")
    la, lo = s.centre()
    assert abs(la - 1.2) < 1e-6 and lo > 103.8              # heading east -> anchor east
    assert s.radius_m() > s.loa_m                          # swing circle > hull
    m = O.ShipObstacle("y", 1.2, 103.8, 300, 40, 12, status="moored")
    assert m.radius_m() == 150 and m.centre() == (1.2, 103.8)


def test_raw_ais_table_picks_stationary(grid):
    df = pd.DataFrame({
        "ReceiveTime": ["2026-09-01 00:00", "2026-09-01 00:10", "2026-09-01 00:05", "2026-09-01 00:09"],
        "MMSI": [111, 111, 222, 333],
        "NavStatus": [0, 1, 0, 5],
        "SoG": [8.0, 0.1, 11.0, 0.0],
        "Lat": [1.20, 1.21, 1.22, 1.23], "Lon": [103.9, 103.9, 103.95, 104.0],
        "CoG": [90, 90, 90, 90], "Heading": [88, 88, 511, 45]})
    snap = O.snapshot_from_ais_table(df, grid=grid)
    ids = sorted(s.mmsi for s in snap.ships)
    assert ids == [111, 333]                                 # 222 is under way
    st = {s.mmsi: s.status for s in snap.ships}
    assert st == {111: "anchored", 333: "moored"}


def test_aisstream_messages_merge():
    state = {}
    O.merge_aisstream_message(state, {"MessageType": "PositionReport",
        "MetaData": {"MMSI": 563000001, "ShipName": "TEST", "time_utc": "t"},
        "Message": {"PositionReport": {"Latitude": 1.25, "Longitude": 103.9, "Sog": 0.1,
                                        "Cog": 10, "TrueHeading": 12, "NavigationalStatus": 1}}})
    O.merge_aisstream_message(state, {"MessageType": "ShipStaticData",
        "MetaData": {"MMSI": 563000001},
        "Message": {"ShipStaticData": {"Type": 71, "Dimension": {"A": 250, "B": 50, "C": 20, "D": 20},
                                        "MaximumStaticDraught": 12.5, "Name": "TEST"}}})
    snap = O.snapshot_from_stream_state(state)
    s = snap.ships[0]
    assert (s.loa_m, s.beam_m, s.ship_type, s.status) == (300, 40, "cargo", "anchored")


def test_snapshot_csv_roundtrip(tmp_path, grid, snap):
    p = tmp_path / "s.csv"
    O.write_snapshot_csv(snap, str(p))
    back = O.load_snapshot_csv(str(p), grid=grid)
    assert len(back) == len(snap)
    assert back.ships[0].id == snap.ships[0].id


def test_obstacle_field_negative_inside(grid, snap):
    f = O.obstacle_field(grid, snap)
    la, lo, r = snap.ships[0].envelope()
    iy, ix = grid.to_index(la, lo)
    assert f.clear_nm[iy, ix] < 0 and f.nearest[iy, ix] == 0
