"""Export everything the in-browser version needs into one JSON file.

    python scripts/export_web_data.py --out web_static/data.json

The static page (web_static/) runs the same board, search and metrics in
JavaScript, so it can be played with in a browser without the Python server.
Grid layers are packed little-endian and base64-encoded.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "src"))

import numpy as np                                                   # noqa: E402

from saferoute import cache, network as NW, obstacles as O, study_area as SA, viz   # noqa: E402
from saferoute import serve                                          # noqa: E402


def b64(a: np.ndarray) -> str:
    return base64.b64encode(np.ascontiguousarray(a).tobytes()).decode("ascii")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="web_static/data.json")
    a = ap.parse_args(argv)
    g = cache.grid(quiet=True)
    meta = serve.meta_payload()
    meta.pop("snapshots", None)
    heading = np.where(np.isnan(g.tss_heading), 65535,
                       np.round(g.tss_heading * 10)).astype("<u2")
    layers = dict(
        land=b64(g.land.astype(np.uint8)),
        dist_mm=b64(np.clip(np.round(g.dist_to_land_nm * 1000), 0, 65535).astype("<u2")),
        depth_cm=b64(np.clip(np.round(g.depth_m * 100), 0, 65535).astype("<u2")),
        tss_zone=b64(g.tss_zone.astype(np.uint8)),
        tss_heading_d10=b64(heading),
        cpa_zone=b64(g.cpa_zone.astype(np.uint8)),
    )
    snap = cache.snapshot("synthetic", quiet=True)
    ships = []
    for s in snap.ships:
        d = s.as_dict()
        d["depth_hint_m"] = s.depth_hint_m
        d["env_swing"] = list(s.envelope(True))
        d["env_hull"] = list(s.envelope(False))
        ships.append(d)
    out = dict(meta=meta, grid=dict(ny=g.ny, nx=g.nx, lat0=float(g.lats[0]), lon0=float(g.lons[0]),
                                    res=g.resolution_deg), layers=layers,
               snapshot=dict(key="synthetic", label=snap.label, source=snap.source,
                             notes=snap.notes, ships=ships),
               tss_centreline=SA.TSS_CENTRELINE)
    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    with open(a.out, "w") as fh:
        json.dump(out, fh, separators=(",", ":"))
    print(a.out, os.path.getsize(a.out) // 1024, "KB")


if __name__ == "__main__":
    main()
