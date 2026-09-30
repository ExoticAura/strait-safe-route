"""Run the whole study and write every results table to --out.

    python scripts/run_pipeline.py --out outputs [--snapshot synthetic|ais:latest.csv]
                                   [--voyages path/to/real_voyages.csv] [--quick]

Scope 1  Navigational network construction  -> s1_board_stats.csv, s1_snapshot.csv,
                                                s1_proxy_vs_charted_depth.csv
Scope 2  Static pathfinding                  -> s2_routes.csv, s2_algorithm_benchmark.csv,
                                                s2_encounters.csv
Scope 3  Baseline route validation           -> s3_voyage_comparison.csv,
                                                s3_paired_tests.csv, s3_reliability.csv,
                                                s3_by_route.csv
"""
from __future__ import annotations

import argparse
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "src"))

import numpy as np          # noqa: E402
import pandas as pd         # noqa: E402

from saferoute import ais, cache, evaluation as EV, obstacles as O   # noqa: E402
from saferoute import routing as R, study_area as SA                # noqa: E402
from saferoute.network import BOARD_LABELS                          # noqa: E402

MODES = ("shortest", "balanced", "safest")
CLASSES = ("FEEDER", "PANAMAX")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="outputs")
    ap.add_argument("--snapshot", default="synthetic")
    ap.add_argument("--voyages", default=None, help="real voyages CSV (ais.load_real_ais)")
    ap.add_argument("--quick", action="store_true")
    a = ap.parse_args(argv)
    os.makedirs(a.out, exist_ok=True)
    t0 = time.time()

    g = cache.grid()
    snap = cache.snapshot(a.snapshot)
    O.write_snapshot_csv(snap, os.path.join(a.out, "s1_snapshot.csv"))

    # ---- scope 1: the board --------------------------------------------------
    rows = []
    for v in SA.VESSEL_CLASSES:
        net = cache.network(v, "shortest", a.snapshot)
        r = dict(vessel=v, nodes=net.n_nodes, edges=net.n_edges,
                 build_s=round(net.stats["build_s"], 2))
        for k, lab in BOARD_LABELS.items():
            r[f"cells_{lab}"] = int((net.board == k).sum())
        rows.append(r)
    pd.DataFrame(rows).to_csv(os.path.join(a.out, "s1_board_stats.csv"), index=False)

    chk = []
    for code, name, charted, la, lo in SA.ANCHORAGE_POINTS:
        iy, ix = g.to_index(la, lo)
        chk.append(dict(anchorage=code, name=name, lat=la, lon=lo, charted_m=charted,
                        model_m=round(float(g.depth_m[iy, ix]), 1),
                        depth_model=g.depth_model))
    dchk = pd.DataFrame(chk)
    dchk["error_m"] = dchk["model_m"] - dchk["charted_m"]
    dchk.to_csv(os.path.join(a.out, "s1_proxy_vs_charted_depth.csv"), index=False)
    print(f"depth model vs {len(dchk)} MPA charted anchorage depths: "
          f"MAE {dchk['error_m'].abs().mean():.1f} m, bias {dchk['error_m'].mean():+.1f} m")

    # ---- scope 2: routes and algorithms ------------------------------------
    route_rows, enc_rows, bench_rows = [], [], []
    for v in CLASSES:
        for mode in MODES:
            net = cache.network(v, mode, a.snapshot)
            for o, d in SA.OD_PAIRS:
                ps = R.plan_route(net, o, d, snap)
                if not ps:
                    route_rows.append(dict(vessel=v, mode=mode, origin=o, destination=d,
                                           found=False))
                    continue
                p = ps[0]
                route_rows.append(dict(vessel=v, mode=mode, origin=o, destination=d,
                                       found=True, **{k: p.metrics[k] for k in p.metrics}))
                for e in p.encounters:
                    enc_rows.append(dict(vessel=v, mode=mode, origin=o, destination=d,
                                         **{k: e[k] for k in ("id", "ship_type", "area",
                                                              "cpa_nm", "hull_nm", "target_nm",
                                                              "along_nm")}))
                if mode != "shortest" or (a.quick and v != "PANAMAX"):
                    continue
                for nb in (8, 16):
                    n2 = net if nb == 16 else __import__("saferoute.network", fromlist=["x"]).build_network(
                        g, v, net.cfg, net.obst, neighbourhood=8)
                    b = R.compare_search(n2, o, d, reps=1 if a.quick else 3)
                    for alg in ("dijkstra", "astar"):
                        bench_rows.append(dict(vessel=v, origin=o, destination=d,
                                               neighbourhood=nb, algorithm=alg, **b[alg],
                                               cost_gap=b["cost_gap"]))
    pd.DataFrame(route_rows).to_csv(os.path.join(a.out, "s2_routes.csv"), index=False)
    pd.DataFrame(enc_rows).to_csv(os.path.join(a.out, "s2_encounters.csv"), index=False)
    bdf = pd.DataFrame(bench_rows)
    bdf.to_csv(os.path.join(a.out, "s2_algorithm_benchmark.csv"), index=False)
    if len(bdf):
        piv = bdf.pivot_table(index=["vessel", "origin", "destination", "neighbourhood"],
                              columns="algorithm", values="nodes_expanded")
        red = 100 * (1 - piv["astar"] / piv["dijkstra"])
        print(f"A* expands {red.mean():.0f}% fewer nodes than Dijkstra "
              f"(max cost gap {bdf['cost_gap'].max():.2e})")

    # ---- scope 3: baseline validation --------------------------------------
    if a.voyages:
        voyages = ais.load_real_ais(a.voyages)
    else:
        # Ship-unaware networks: land, depth and shore limits only.
        from saferoute import network as NW
        unaware = {v: NW.build_network(g, v, NW.config_for("shortest"), None) for v in CLASSES}
        voyages = ais.generate_baseline_voyages(g, n_per_od=6 if a.quick else 20,
                                                classes=CLASSES, unaware_nets=unaware)
    ais.voyages_to_frame(voyages).to_csv(os.path.join(a.out, "s3_baseline_tracks.csv.gz"),
                                         index=False, compression="gzip")
    nets = {(v, m): cache.network(v, m, a.snapshot) for v in CLASSES for m in MODES}
    df = EV.compare_to_baseline(nets, voyages, snap, modes=MODES)
    df.to_csv(os.path.join(a.out, "s3_voyage_comparison.csv"), index=False)
    tests = pd.concat([EV.paired_tests(df, m) for m in MODES], ignore_index=True)
    tests.to_csv(os.path.join(a.out, "s3_paired_tests.csv"), index=False)
    rel = EV.reliability_summary(df, MODES)
    rel.to_csv(os.path.join(a.out, "s3_reliability.csv"), index=False)
    EV.by_route(df, MODES).to_csv(os.path.join(a.out, "s3_by_route.csv"), index=False)

    pd.set_option("display.width", 160)
    print(rel.round(2).to_string(index=False))
    print(tests[tests.metric.isin(["distance_nm", "obstacles_met", "min_ship_clear_nm"])]
          [["mode", "metric", "n", "baseline_mean", "planned_mean", "pct_improvement", "t_p"]]
          .round(4).to_string(index=False))
    print(f"\ndone in {time.time() - t0:.0f} s -> {a.out}/")


if __name__ == "__main__":
    main()
