"""Build the 3D world for the ship viewer: buildings, and port equipment.

    python scripts/export_3d_world.py --osm path/to/singapore-driving/public/data --out web_static/world

Inputs
  * OpenStreetMap building footprints for Singapore (Geofabrik extract,
    ODbL), as the 500 m GeoJSON chunks of the open-source singapore-driving
    project (github.com/joetancy/singapore-driving, public/data/).
  * The study area's land polygons (src/saferoute/data/sg_land.geojson).

Outputs (in --out)
  b/<i>_<j>.txt   building tiles (base64 of the binary below), 0.04 deg square. Records:
                    uint16 nverts, uint16 height_dm, uint8 flags (1 = estimated height)
                    then nverts x (uint16 dx, uint16 dy) in 1e-6 deg from the tile's SW corner
  world.json      tile index, heights provenance, and quay crane / container
                  yard positions for each terminal (REPRESENTATIVE: placed along
                  the modelled shoreline next to each terminal, not surveyed)

Heights: OSM `height`, else `building:levels` x 3.2 m, else an estimate by
building type (flagged as estimated so the viewer can say so).
"""
from __future__ import annotations

import argparse
import base64
import glob
import json
import math
import os
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

import numpy as np                                     # noqa: E402
from shapely.geometry import LineString, Point, shape  # noqa: E402
from shapely.prepared import prep                      # noqa: E402

from saferoute import study_area as SA                 # noqa: E402

TILE = 0.04
EST = {"house": 9, "detached": 9, "terrace": 9, "semidetached_house": 9, "bungalow": 8,
       "residential": 36, "apartments": 40, "commercial": 24, "office": 30, "retail": 14,
       "industrial": 14, "warehouse": 14, "storage_tank": 18, "hotel": 50, "roof": 5,
       "school": 16, "hospital": 30, "church": 14, "temple": 12, "mosque": 14,
       "garage": 4, "shed": 4, "hut": 4, "train_station": 16, "transportation": 12}


def parse_height(p):
    for k in ("height",):
        v = p.get(k)
        if v:
            try:
                return float(str(v).replace("m", "").strip()), False
            except ValueError:
                pass
    v = p.get("building:levels")
    if v:
        try:
            return max(float(str(v).split(";")[0]) * 3.2, 3.0), False
        except ValueError:
            pass
    return float(EST.get(p.get("building"), 10)), True


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--osm", required=True)
    ap.add_argument("--out", default=os.path.join(ROOT, "web_static", "world"))
    a = ap.parse_args(argv)
    os.makedirs(os.path.join(a.out, "b"), exist_ok=True)

    seen, tiles = set(), {}
    n_est = n_all = 0
    for fn in sorted(glob.glob(os.path.join(a.osm, "*_*.geojson"))):
        for f in json.load(open(fn))["features"]:
            p = f["properties"]
            if "building" not in p or f["geometry"]["type"] not in ("Polygon", "MultiPolygon"):
                continue
            fid = str(f.get("id", "")).rsplit("-", 1)[0]
            if fid and fid in seen:
                continue
            seen.add(fid)
            g = shape(f["geometry"])
            polys = [g] if g.geom_type == "Polygon" else list(g.geoms)
            h, est = parse_height(p)
            h -= float(p.get("min_height") or 0) if str(p.get("min_height", "")).replace(".", "").isdigit() else 0
            for poly in polys:
                if poly.area < 1e-9:
                    continue
                ring = poly.exterior.simplify(0.000008)
                cs = list(ring.coords)[:-1]
                if len(cs) < 3 or len(cs) > 400:
                    continue
                c = poly.representative_point()
                key = (math.floor((c.x - SA.BBOX["lon_min"]) / TILE),
                       math.floor((c.y - SA.BBOX["lat_min"]) / TILE))
                lon0 = SA.BBOX["lon_min"] + key[0] * TILE
                lat0 = SA.BBOX["lat_min"] + key[1] * TILE
                q = [(int(round((x - lon0) * 1e6)), int(round((y - lat0) * 1e6))) for x, y in cs]
                if any(not (0 <= dx <= 65535 and 0 <= dy <= 65535) for dx, dy in q):
                    continue
                buf = tiles.setdefault(key, bytearray())
                buf += struct.pack("<HHB", len(q), min(max(int(round(h * 10)), 20), 65535), 1 if est else 0)
                for dx, dy in q:
                    buf += struct.pack("<HH", dx, dy)
                n_all += 1
                n_est += est
    index = []
    for (i, j), buf in tiles.items():
        # base64 text: static hosts (and the published artifact) serve .txt
        name = f"{i}_{j}.txt"
        with open(os.path.join(a.out, "b", name), "w") as fh:
            fh.write(base64.b64encode(bytes(buf)).decode("ascii"))
        index.append([i, j, len(buf)])

    # ---- representative port equipment along the terminals' shoreline ----
    land = shape(json.load(open(os.path.join(ROOT, "src", "saferoute", "data", "sg_land.geojson")))
                 ["features"][0]["geometry"])
    pl = prep(land)
    kx = 111320 * math.cos(math.radians(1.27))
    ky = 110574.0
    terminals = []
    for code in SA.TERMINALS:
        t = SA.PORTS[code]
        cranes = []
        for geom in land.geoms:
            ext = geom.exterior
            if ext.distance(Point(t.lon, t.lat)) > 0.02:
                continue
            L = ext.length
            n = int(L / 0.0009)                         # ~100 m spacing
            for k in range(n):
                p0 = ext.interpolate(k / n, normalized=True)
                p1 = ext.interpolate((k + .5) / n, normalized=True)
                if Point(t.lon, t.lat).distance(p0) > 0.012:     # within ~1.3 km of the berth
                    continue
                dx, dy = (p1.x - p0.x) * kx, (p1.y - p0.y) * ky
                ln = math.hypot(dx, dy) or 1
                # the seaward normal is whichever side is water 80 m out
                for sgn in (1, -1):
                    nx, ny = sgn * dy / ln, -sgn * dx / ln
                    wpt = Point(p0.x + nx * 80 / kx, p0.y + ny * 80 / ky)
                    ipt = Point(p0.x - nx * 80 / kx, p0.y - ny * 80 / ky)
                    if not pl.contains(wpt) and pl.contains(ipt):
                        brg = (math.degrees(math.atan2(nx, ny)) + 360) % 360
                        cranes.append([round(p0.y, 6), round(p0.x, 6), round(brg, 1)])
                        break
        cranes = cranes[:40]
        terminals.append(dict(code=code, name=t.name, cranes=cranes))
    world = dict(tile_deg=TILE, lon0=SA.BBOX["lon_min"], lat0=SA.BBOX["lat_min"], tiles=index,
                 buildings=n_all, estimated_heights=n_est,
                 source="OpenStreetMap contributors (ODbL), Geofabrik extract 2026-09, via "
                        "github.com/joetancy/singapore-driving",
                 terminals=terminals,
                 note="Quay cranes and container yards are representative: placed along the "
                      "modelled shoreline next to each terminal, not surveyed positions.")
    with open(os.path.join(a.out, "world.json"), "w") as fh:
        json.dump(world, fh, separators=(",", ":"))
    tot = sum(x[2] for x in index)
    print(f"{n_all} buildings ({n_est} estimated heights) in {len(index)} tiles, "
          f"{tot / 1e6:.1f} MB; cranes: " + ", ".join(f"{t['code']} {len(t['cranes'])}" for t in terminals))


if __name__ == "__main__":
    main()
