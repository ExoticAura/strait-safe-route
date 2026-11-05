"""Extra detail for the 3D viewer: land height, sea depth, shore foam, container yards.

    python scripts/export_3d_detail.py --osm path/to/singapore-driving/public/data \
           [--dem data/dem/cop30_singapore.tif]

Adds to web_static/world/ (and to world.json):

  terrain.txt   land height, base64 uint8 grid, 0.0008 deg (~89 m), lon 103.60-104.10,
                lat 1.15-1.48, row 0 = north. height_m = value * 0.7 above LAND_TOP
                (value 0 = flat land / sea).
                Source: Copernicus DEM GLO-30 (a SURFACE model: it includes buildings
                and trees), via OpenTopography. To stop the buildings being drawn twice
                (once from OSM, once in the ground) the ground is estimated as a
                morphological opening of the DEM (erode then dilate over 450 m, smoothed):
                narrow towers vanish, hills stay. Approximate; ground above 6 m only.
  depth.png     sea depth under the water, 8 bit, depth_m = value * 60 / 255, from the
                route board (GEBCO 2026 with the dredged terminal pockets on top).
                Row 0 = north. Used to tint shallow water in the viewer.
  shore.json    a ~8 m ribbon of white water along the coastline (outside the land).
  world.json    "terrain", "depth" (bounds, scales) and per terminal "yards": container
                blocks as [lat, lon, bearing of the block's long axis], laid out in the
                free land behind the quay (REPRESENTATIVE, not surveyed).

Contains modified Copernicus data (DEM) and OpenStreetMap data (ODbL).
"""
from __future__ import annotations

import argparse
import base64
import glob
import json
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

import numpy as np                                                    # noqa: E402
from PIL import Image                                                 # noqa: E402
from scipy import ndimage                                             # noqa: E402
from shapely.geometry import Point, Polygon, box, mapping, shape      # noqa: E402
from shapely.ops import unary_union                                   # noqa: E402
from shapely.prepared import prep                                     # noqa: E402
from shapely.strtree import STRtree                                   # noqa: E402

from saferoute import seagrid as SG, study_area as SA                 # noqa: E402

LON0, LON1, LAT0, LAT1 = 103.60, 104.10, 1.15, 1.48
STEP = 0.0008
H_SCALE = 0.7                       # metres per terrain unit
KY = 110574.0
BLOCK_LONG, BLOCK_WIDE = 126.0, 17.4          # 10 bays of 12.6 m, 6 rows of 2.9 m
PITCH_LONG, PITCH_WIDE = 156.0, 25.0          # + transfer lanes
APRON_M = 45.0                                # quay apron kept clear (cranes, lanes)
YARD_R = {"SGTUA": 2600, "SGJUR": 1500, "SGPPT": 2200, "SGKEP": 1500}   # m round the berth


def terrain(dem_path, land_mask):
    import tifffile
    t = tifffile.TiffFile(dem_path)
    pg = t.pages[0]
    sx, sy = pg.tags["ModelPixelScaleTag"].value[:2]
    tp = pg.tags["ModelTiepointTag"].value
    lon_w, lat_n = tp[3], tp[4]
    dem = t.asarray().astype(np.float32)
    dem = np.where(dem < -30, 0, dem)
    ground = ndimage.maximum_filter(ndimage.minimum_filter(dem, size=15), size=15)   # opening, 15 px = 450 m
    ground = ndimage.gaussian_filter(ground, 3)
    lats = LAT0 + np.arange(int(round((LAT1 - LAT0) / STEP)) + 1) * STEP
    lons = LON0 + np.arange(int(round((LON1 - LON0) / STEP)) + 1) * STEP
    rr = (lat_n - lats) / sy
    cc = (lons - lon_w) / sx
    R, C = np.meshgrid(rr, cc, indexing="ij")
    g = ndimage.map_coordinates(ground, [R, C], order=1, mode="nearest")
    h = np.clip(g - 6.0, 0, 178)
    h = np.where(land_mask(lats, lons), h, 0)
    v = np.clip(np.round(h / H_SCALE), 0, 255).astype(np.uint8)[::-1]           # row 0 = north
    return lats, lons, v


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--osm", required=True)
    ap.add_argument("--dem", default=os.path.join(ROOT, "data", "dem", "cop30_singapore.tif"))
    ap.add_argument("--out", default=os.path.join(ROOT, "web_static", "world"))
    a = ap.parse_args(argv)
    wj = os.path.join(a.out, "world.json")
    world = json.load(open(wj))

    polys = SG.load_land_polygons()
    land = shape(json.load(open(SG.LAND_GEOJSON))["features"][0]["geometry"])

    # ---------------- land height
    lm = lambda la, lo: SG.rasterise_land(la, lo, polys)
    lats, lons, v = terrain(a.dem, lm)
    txt = base64.b64encode(v.tobytes()).decode("ascii")
    open(os.path.join(a.out, "terrain.txt"), "w").write(txt)
    world["terrain"] = dict(file="terrain.txt", lon0=LON0, lat0=LAT0, lon1=float(lons[-1]), lat1=float(lats[-1]),
                            w=int(v.shape[1]), h=int(v.shape[0]), scale_m=H_SCALE,
                            note="Copernicus DEM GLO-30 (surface model), buildings filtered out by a 450 m "
                                 "morphological opening; approximate ground height above 6 m.")
    print(f"terrain {v.shape}, max {v.max() * H_SCALE:.0f} m above 6 m, "
          f"{(v > 0).sum()} raised cells, {len(txt) / 1e3:.0f} kB")

    # ---------------- sea depth
    g = SG.build_seagrid()
    d = np.clip(np.round(g.depth_m / 60.0 * 255), 0, 255).astype(np.uint8)[::-1]
    Image.fromarray(d, "L").save(os.path.join(a.out, "depth.png"), optimize=True)
    world["depth"] = dict(file="depth.png", lon0=float(g.lons[0]), lat0=float(g.lats[0]),
                          lon1=float(g.lons[-1]), lat1=float(g.lats[-1]), scale_m=60.0 / 255,
                          source=g.depth_source or "proxy")
    print("depth.png", d.shape, "source", world["depth"]["source"])

    # ---------------- shore foam ribbon
    ribbon = land.buffer(0.000075).difference(land).simplify(0.000006)
    feats = []
    for p in (ribbon.geoms if ribbon.geom_type == "MultiPolygon" else [ribbon]):
        if p.area < 1e-10:
            continue
        rings = [list(p.exterior.coords)] + [list(h.coords) for h in p.interiors]
        feats.append([[[round(x, 6), round(y, 6)] for x, y in r] for r in rings])
    json.dump(feats, open(os.path.join(a.out, "shore.json"), "w"), separators=(",", ":"))
    world["shore"] = dict(file="shore.json", polygons=len(feats))
    print("shore ribbon:", len(feats), "polygons,", os.path.getsize(os.path.join(a.out, "shore.json")) // 1000, "kB")

    # ---------------- container yards behind each quay
    tiles = []
    for fn in glob.glob(os.path.join(a.osm, "*_*.geojson")):
        tiles.append(fn)
    bld = []
    boxes = {}
    for code in SA.TERMINALS:
        p = SA.PORTS[code]
        boxes[code] = box(p.lon - 0.035, p.lat - 0.03, p.lon + 0.035, p.lat + 0.03)
    allbox = unary_union(list(boxes.values()))
    for fn in tiles:
        for f in json.load(open(fn))["features"]:
            if "building" not in f["properties"] or f["geometry"]["type"] not in ("Polygon", "MultiPolygon"):
                continue
            gm = shape(f["geometry"])
            if gm.intersects(allbox):
                bld.append(gm)
    tree = STRtree(bld)
    print("buildings near terminals:", len(bld))
    for t in world["terminals"]:
        code = t["code"]
        p = SA.PORTS[code]
        kx = 111320 * math.cos(math.radians(p.lat))
        # local metres (east, north) from the berth
        to_m = lambda x, y: ((x - p.lon) * kx, (y - p.lat) * KY)
        from shapely import affinity
        sc = lambda gm: affinity.scale(affinity.translate(gm, -p.lon, -p.lat), kx, KY, origin=(0, 0))
        circle = Point(0, 0).buffer(YARD_R[code])
        near = sc(land).intersection(circle.buffer(300))
        inland = near.buffer(-APRON_M)
        hit = [bld[i] for i in tree.query(boxes[code])]
        bl = unary_union([sc(b) for b in hit]).buffer(10) if hit else Polygon()
        valid = prep(inland.difference(bl).intersection(circle))
        # blocks run perpendicular to the quay; the quay heading is the berth's
        qh = math.radians(p.quay_hdg)
        ux, uy = math.sin(qh), math.cos(qh)            # along the quay (east, north)
        nx, ny = -uy, ux                               # across it
        # which side is land: take the one where the berth-side water is not
        if not land.contains(Point(p.lon + nx * 100 / kx, p.lat + ny * 100 / KY)):
            nx, ny = -nx, -ny
        # block long axis = across the quay (n), pitch along the quay (u)
        out = []
        R = YARD_R[code]
        for i in range(-int(R / PITCH_WIDE) - 1, int(R / PITCH_WIDE) + 2):
            for j in range(0, int(R / PITCH_LONG) + 2):
                cx = ux * i * PITCH_WIDE + nx * (APRON_M + BLOCK_LONG / 2 + j * PITCH_LONG)
                cy = uy * i * PITCH_WIDE + ny * (APRON_M + BLOCK_LONG / 2 + j * PITCH_LONG)
                rect = Polygon([(cx + ux * hw + nx * hl, cy + uy * hw + ny * hl)
                                for hw, hl in ((BLOCK_WIDE / 2, BLOCK_LONG / 2), (-BLOCK_WIDE / 2, BLOCK_LONG / 2),
                                               (-BLOCK_WIDE / 2, -BLOCK_LONG / 2), (BLOCK_WIDE / 2, -BLOCK_LONG / 2))])
                if valid.contains(rect):
                    la, lo = p.lat + cy / KY, p.lon + cx / kx
                    brg = (math.degrees(math.atan2(nx, ny)) + 360) % 360
                    out.append([round(la, 6), round(lo, 6), round(brg, 1)])
        t["yards"] = out
        print(f"  {code}: {len(out)} yard blocks (about {len(out) * 60} container stacks)")
    world["note"] = ("Quay cranes and container yards are representative: cranes along the modelled "
                     "shoreline, yard blocks in the free land behind the quay; not surveyed positions. "
                     "Land height is approximate (Copernicus DEM with buildings filtered out).")
    json.dump(world, open(wj, "w"), separators=(",", ":"))


if __name__ == "__main__":
    main()
