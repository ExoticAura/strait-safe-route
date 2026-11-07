"""Export everything the in-browser version needs into one JSON file.

    python scripts/export_web_data.py            # -> web_static/data.json
                                                 #    (data_mpa.json + mpa.json with MPA data)

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
from saferoute import serve, mpa                                     # noqa: E402


def b64(a: np.ndarray) -> str:
    return base64.b64encode(np.ascontiguousarray(a).tobytes()).decode("ascii")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=None,
                    help="default web_static/data.json, or web_static/data_mpa.json when the "
                         "MPA layers are in data/gss (that file and mpa.json are git-ignored)")
    a = ap.parse_args(argv)
    use_mpa = mpa.enabled()
    a.out = a.out or ("web_static/data_mpa.json" if use_mpa else "web_static/data.json")
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
    if g.charted is not None:
        layers["charted"] = b64(g.charted.astype(np.uint8))
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
    if use_mpa:
        disp = os.path.join(os.path.dirname(a.out) or ".", "mpa.json")
        with open(disp, "w") as fh:
            json.dump(mpa_display(), fh, separators=(",", ":"))
        print(disp, os.path.getsize(disp) // 1024, "KB")


def mpa_display() -> dict:
    """MPA chart layers for the map (simplified to ~3 m, coordinates to 5 dp)."""
    from shapely.geometry import mapping, shape

    def geo(gm, tol=0.00003):
        gm = gm.simplify(tol, preserve_topology=True) if gm.geom_type != "Point" else gm
        def rnd(c):
            return [rnd(x) for x in c] if isinstance(c[0], (list, tuple)) else [round(c[0], 5), round(c[1], 5)]
        m = mapping(gm)
        return {"type": m["type"], "coordinates": rnd(m["coordinates"])}

    def fc(items):
        return {"type": "FeatureCollection", "features": [
            {"type": "Feature", "properties": p, "geometry": geo(gm)} for gm, p in items]}

    depth = []
    for f in mpa.features("depthsa"):
        p = f["properties"]
        dredged = p.get("FCSubtype") == mpa.DRGARE
        depth.append((shape(f["geometry"]), dict(
            d1=p.get("DRVAL1"), d2=None if dredged else p.get("DRVAL2"), dredged=dredged,
            name=p.get("OBJNAM") or "")))
    hz = [(h["geom"], dict(depth=h["depth"], kind=h["kind"], name=h["name"], inform=h["inform"],
                           routed=h["depth"] is not None)) for h in mpa.hazards()]
    tracks = []
    for layer in ("tracksandroutesa", "tracksandroutesl"):
        for f in mpa.features(layer):
            p = f["properties"]
            kind = (p.get("CATTSS_TXT") and "traffic lane (" + p["CATTSS_TXT"] + ")") or \
                   (p.get("CATNAV_TXT")) or (p.get("CATTRK_TXT") and "recommended track") or \
                   ("fairway" if layer.endswith("a") else "track")
            tracks.append((shape(f["geometry"]), dict(
                kind=kind, orient=p.get("ORIENT"), traffic=p.get("TRAFIC_TXT"),
                depth=p.get("DRVAL1"), name=p.get("OBJNAM") or "",
                inform=(p.get("INFORM") or "")[:200], area=layer.endswith("a"))))
    berths = []
    for f in mpa.features("portsandservicesp"):
        p = f["properties"]
        n = p.get("OBJNAM") or ""
        if "Berth" in n or n.startswith(("Pasir Panjang Terminal", "Keppel Terminal", "Brani Terminal")):
            berths.append((shape(f["geometry"]).centroid, dict(name=n)))
    return dict(source=f"MPA GeoSpace-Sea (IHO S-57 extracts, {mpa.source_date()}); not for navigation",
                depth=fc(depth), hazards=fc(hz), tracks=fc(tracks), berths=fc(berths))


if __name__ == "__main__":
    main()
