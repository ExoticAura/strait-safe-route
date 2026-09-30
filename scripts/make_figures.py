"""Figures for the report:  python scripts/make_figures.py [--out outputs/figures]

F1  study area: coastline, TSS, MPA anchorages, the ship snapshot, ports
F2  the router's board for a Panamax (land / shallow / ship exclusion / CPA)
F3  shortest vs safest route, EAST -> Pasir Panjang, with the ships met
F4  baseline validation: ships met and distance, historical vs planned
"""
from __future__ import annotations

import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "src"))

import matplotlib                          # noqa: E402
matplotlib.use("Agg")
import matplotlib.pyplot as plt            # noqa: E402
import numpy as np                         # noqa: E402
import pandas as pd                        # noqa: E402
from matplotlib.colors import ListedColormap   # noqa: E402

from saferoute import cache, routing as R, study_area as SA, viz   # noqa: E402

TYPE_COL = {"container": "#2E6DB4", "tanker": "#B03A2E", "bulker": "#8A6D3B", "cargo": "#5C7A3A",
            "gas": "#C67A21", "passenger": "#7E60A6", "service": "#5F737F", "other": "#7C8A93"}
MODE_COL = {"shortest": "#C67A21", "balanced": "#2E6DB4", "safest": "#12876B"}


def base(ax, g, extent=None):
    ax.set_facecolor("#D9E4EB")
    ax.contourf(g.lons, g.lats, g.land.astype(float), levels=[.5, 1.5], colors=["#DED2B2"])
    tss = viz.tss_geojson()
    for f in tss["features"]:
        c = np.array(f["geometry"]["coordinates"])
        ax.plot(c[:, 0], c[:, 1], color="#7E60A6", lw=.7, ls="--" if f["properties"]["kind"] == "tss_axis" else "-")
    for p in SA.PORTS.values():
        ax.plot(p.app_lon, p.app_lat, "ko", ms=4)
        ax.annotate(p.name, (p.app_lon, p.app_lat), xytext=(4, 4), textcoords="offset points", fontsize=7)
    ax.set_aspect(1 / np.cos(np.radians(1.25)))
    if extent:
        ax.set_xlim(extent[0], extent[1]); ax.set_ylim(extent[2], extent[3])
    else:
        ax.set_xlim(g.lons[0], g.lons[-1]); ax.set_ylim(g.lats[0], g.lats[-1])
    ax.tick_params(labelsize=7)


def ships(ax, snap, enc_ids=()):
    for s in snap.ships:
        la, lo, r = s.envelope()
        ax.add_patch(plt.Circle((lo, la), r / 60,
                                fill=False, lw=.4, color="#B03A2E" if s.id in enc_ids else "#888"))
        ax.plot(s.lon, s.lat, "o", ms=2.2, color=TYPE_COL.get(s.ship_type, "#888"))


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="outputs/figures")
    ap.add_argument("--results", default="outputs")
    ap.add_argument("--snapshot", default="synthetic")
    a = ap.parse_args(argv)
    os.makedirs(a.out, exist_ok=True)
    g = cache.grid(quiet=True)
    snap = cache.snapshot(a.snapshot, quiet=True)

    # F1
    fig, ax = plt.subplots(figsize=(12, 5.4))
    base(ax, g); ships(ax, snap)
    for c, n, d, la, lo in SA.ANCHORAGE_POINTS:
        ax.plot(lo, la, "+", color="#6B5B95", ms=4)
    handles = [plt.Line2D([], [], marker="o", ls="", color=v, label=k) for k, v in TYPE_COL.items()
               if k in snap.counts()]
    ax.legend(handles=handles, fontsize=7, loc="upper right", title=f"{len(snap)} stationary ships", title_fontsize=7)
    ax.set_title(f"F1  Study area, TSS (schematic), MPA anchorages (+) and ship snapshot: {snap.label}", fontsize=9)
    fig.savefig(os.path.join(a.out, "f1_study_area.png"), dpi=160, bbox_inches="tight"); plt.close(fig)

    # F2
    net = cache.network("PANAMAX", "shortest", a.snapshot, quiet=True)
    fig, ax = plt.subplots(figsize=(12, 5.4))
    cmap = ListedColormap(["#F4F7F9", "#DED2B2", "#7FA6CF", "#D9A866", "#C94A3B", "#F0C27B"])
    ax.pcolormesh(g.lons, g.lats, net.board, cmap=cmap, vmin=-.5, vmax=5.5, shading="nearest")
    ax.set_aspect(1 / np.cos(np.radians(1.25)))
    ax.set_xlim(103.55, 104.15); ax.set_ylim(1.10, 1.36)
    handles = [plt.Rectangle((0, 0), 1, 1, color=cmap(i)) for i in range(6)]
    ax.legend(handles, ["free water", "land", "too shallow", "too close to shore",
                        "ship exclusion (hard)", "inside target CPA (soft)"], fontsize=7, loc="lower right")
    ax.set_title(f"F2  The router's board for a Panamax (draught 12.5 m): {net.n_nodes:,} navigable nodes, "
                 f"{net.n_edges:,} directed edges", fontsize=9)
    fig.savefig(os.path.join(a.out, "f2_board.png"), dpi=160, bbox_inches="tight"); plt.close(fig)

    # F3
    fig, ax = plt.subplots(figsize=(12, 5.4))
    base(ax, g, (103.55, 104.45, 1.10, 1.36))
    enc_all = set()
    for mode in ("shortest", "safest"):
        n = cache.network("PANAMAX", mode, a.snapshot, quiet=True)
        p = R.plan_route(n, "EAST", "SGPPT", snap)[0]
        enc_all |= {e["id"] for e in p.encounters}
        c = np.array(p.coords)
        ax.plot(c[:, 1], c[:, 0], color=MODE_COL[mode], lw=1.8,
                label=f"{mode}: {p.metrics['distance_nm']:.1f} NM, {p.metrics['obstacles_met']} ships met, "
                      f"closest hull {p.metrics['min_hull_dist_nm']:.2f} NM")
    ships(ax, snap, enc_all)
    ax.legend(fontsize=8, loc="lower right")
    ax.set_title("F3  Strait entrance (East) -> Pasir Panjang, Panamax: shortest vs safest", fontsize=9)
    fig.savefig(os.path.join(a.out, "f3_routes.png"), dpi=160, bbox_inches="tight"); plt.close(fig)

    # F4
    path = os.path.join(a.results, "s3_reliability.csv")
    if os.path.exists(path):
        rel = pd.read_csv(path)
        fig, axs = plt.subplots(1, 3, figsize=(12, 3.4))
        labels = rel["approach"].str.replace("planned ", "").str.replace("historical ", "")
        cols = ["#7C8A93"] + [MODE_COL.get(m, "#888") for m in ("shortest", "balanced", "safest")][:len(rel) - 1]
        for ax, (col, ttl) in zip(axs, [("mean_obstacles_met", "ships met per voyage"),
                                        ("mean_distance_nm", "distance (NM)"),
                                        ("hard_breach_free_pct", "% voyages with no hard CPA breach")]):
            ax.bar(labels, rel[col], color=cols)
            ax.set_title(ttl, fontsize=9); ax.tick_params(labelsize=7)
        fig.suptitle("F4  Baseline validation against historical voyages (same snapshot)", fontsize=9)
        fig.savefig(os.path.join(a.out, "f4_validation.png"), dpi=160, bbox_inches="tight"); plt.close(fig)
    print("figures ->", a.out)


if __name__ == "__main__":
    main()
