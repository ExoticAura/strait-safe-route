"""Re-planning as traffic moves (scripts/replan_benchmark.py)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import replan_benchmark as RB                      # noqa: E402


def test_replan_cycles_are_feasible_and_fast(grid):
    traffic = RB.SimTraffic(grid, n_moving=20)
    rows = RB.voyage(grid, traffic, "PANAMAX", "safest", "EAST", "SGPPT", 30.0, 3, 0.0)
    assert len(rows) == 3
    assert all(r["found"] and r["feasible"] for r in rows)
    assert all(r["moving"] == 20 for r in rows)
    # the ship moves on between cycles, so the remaining distance falls
    assert rows[-1]["remaining_nm"] < rows[0]["remaining_nm"]
    assert max(r["total_ms"] for r in rows) < 10_000          # inside a 10 s cycle


def test_simulated_movers_advance_along_the_lanes(grid):
    traffic = RB.SimTraffic(grid, n_moving=4)
    a, b = traffic.at(0.0), traffic.at(600.0)
    ma = [s for s in a.ships if s.status == "under way"]
    mb = [s for s in b.ships if s.status == "under way"]
    from saferoute.geo import haversine_nm
    for s, t in zip(ma, mb):
        d = float(haversine_nm(s.lat, s.lon, t.lat, t.lon))
        assert abs(d - s.sog_kn * 600 / 3600) < 0.3        # 10 min at its speed
