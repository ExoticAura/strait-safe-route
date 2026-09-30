import numpy as np

from saferoute import study_area as SA
from saferoute.seagrid import (build_seagrid, depth_from_shore_distance, read_esri_ascii,
                               load_bathymetry_file)


def test_grid_shape_and_land(grid):
    assert grid.shape == (211, 526)
    assert 0.25 < grid.land.mean() < 0.5          # Singapore, Johor, Batam, Bintan


def test_known_land_and_water(grid):
    # Singapore island interior vs the open strait
    assert grid.land[grid.to_index(1.35, 103.82)]
    assert not grid.land[grid.to_index(1.20, 104.05)]


def test_every_approach_is_water_and_deep_enough(grid):
    for p in SA.PORTS.values():
        iy, ix = grid.to_index(*p.approach)
        assert not grid.land[iy, ix], p.code
        assert grid.dist_to_land_nm[iy, ix] >= 0.25, p.code


def test_tss_clear_of_land(grid):
    from saferoute.geo import densify
    d = [grid.dist_to_land_nm[grid.to_index(*q)] for q in densify(SA.TSS_CENTRELINE, 0.2)]
    assert min(d) > 1.0


def test_depth_proxy_monotone():
    d = depth_from_shore_distance(np.linspace(0, 5, 50))
    assert np.all(np.diff(d) > 0) and d[0] == 0


def test_esri_ascii_bathymetry_replaces_proxy(tmp_path):
    # a flat 9 m sea over part of the box: must override the proxy there
    p = tmp_path / "gebco.asc"
    nc, nr, cs = 40, 20, 0.01
    rows = "\n".join(" ".join(["-9"] * nc) for _ in range(nr))
    p.write_text(f"ncols {nc}\nnrows {nr}\nxllcorner 103.9\nyllcorner 1.15\n"
                 f"cellsize {cs}\nNODATA_value -32767\n{rows}\n")
    la, lo, z = read_esri_ascii(str(p))
    assert z.shape == (nr, nc) and la[0] > la[-1]
    g = build_seagrid(0.004, bathymetry=str(p))
    iy, ix = g.to_index(1.25, 104.1)
    assert g.depth_model == "bathymetry_file"
    assert abs(g.depth_m[iy, ix] - 9.0) < 1e-6
