"""Paint the land-use texture draped over the 3D land in the ship viewer.

    python scripts/export_land_texture.py --osm path/to/singapore-driving/public/data

Draws, in order, onto one image covering Singapore (lon 103.60-104.10,
lat 1.15-1.48):

  * vegetation green as the base (undeveloped land, and Johor / the islands)
  * light concrete wherever there are buildings or roads close together (urban)
  * tan port and industrial ground (industrial / warehouse / tank footprints,
    and the container yards behind each terminal's quay cranes)
  * parks, and inland water (reservoirs, canals) from OSM landuse areas
  * the road network, width by class
  * building footprints (so the city reads beyond the 3D-building radius)

Inputs are OpenStreetMap (ODbL) extracts of the singapore-driving project
(github.com/joetancy/singapore-driving, public/data/): areas.geojson,
roads.geojson and the building chunks. Output: web_static/world/landuse.jpg
and its bounds in world.json ("landuse").
"""
from __future__ import annotations

import argparse
import glob
import json
import math
import os

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

LON0, LON1, LAT0, LAT1 = 103.60, 104.10, 1.15, 1.48
OUT_W = 4096
SS = 2                                     # supersample, then downsample
KX = 111320 * math.cos(math.radians(1.3))
KY = 110574.0

C = dict(base=(128, 152, 104), urban=(205, 202, 194), port=(206, 190, 158), park=(112, 158, 92),
         water=(96, 146, 178), road_major=(150, 150, 146), road=(176, 175, 170),
         building=(232, 230, 224))
ROAD_W = {"motorway": 32, "trunk": 26, "primary": 20, "secondary": 16, "tertiary": 12,
          "motorway_link": 14, "trunk_link": 12, "primary_link": 11, "secondary_link": 10,
          "tertiary_link": 9}
INDUSTRIAL = {"industrial", "warehouse", "storage_tank", "hangar", "manufacture", "factory", "silo"}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--osm", required=True)
    ap.add_argument("--out", default=os.path.join(ROOT, "web_static", "world"))
    a = ap.parse_args(argv)

    W = OUT_W * SS
    H = int(round(W * (LAT1 - LAT0) * KY / ((LON1 - LON0) * KX)))
    mpp = (LON1 - LON0) * KX / W                                      # metres per pixel
    px = lambda lon, lat: ((lon - LON0) / (LON1 - LON0) * W, (LAT1 - lat) / (LAT1 - LAT0) * H)
    ring_px = lambda ring: [px(x, y) for x, y in ring]
    print(f"{W}x{H} px at {mpp:.1f} m/px (output {mpp * SS:.1f} m/px)")

    # ---- collect OSM layers
    buildings, industrial = [], []
    for fn in sorted(glob.glob(os.path.join(a.osm, "*_*.geojson"))):
        for f in json.load(open(fn))["features"]:
            p, g = f["properties"], f["geometry"]
            if "building" not in p or g["type"] not in ("Polygon", "MultiPolygon"):
                continue
            polys = [g["coordinates"]] if g["type"] == "Polygon" else g["coordinates"]
            for poly in polys:
                r = ring_px(poly[0])
                buildings.append(r)
                if p.get("building") in INDUSTRIAL:
                    industrial.append(r)
    areas = json.load(open(os.path.join(a.osm, "areas.geojson")))["features"]
    roads = json.load(open(os.path.join(a.osm, "roads.geojson")))["features"]
    print(f"{len(buildings)} buildings ({len(industrial)} industrial), {len(areas)} areas, {len(roads)} roads")

    def mask():
        return Image.new("L", (W, H), 0)

    def road_lines(draw, widen=0.0, scale=1.0):
        for f in roads:
            g = f["geometry"]
            w = ROAD_W.get(f["properties"].get("highway"), 7) * scale + widen
            lines = [g["coordinates"]] if g["type"] == "LineString" else (
                g["coordinates"] if g["type"] == "MultiLineString" else [])
            for ln in lines:
                if len(ln) > 1:
                    draw.line(ring_px(ln), fill=255, width=max(1, int(round(w / mpp))), joint="curve")

    # urban: anything within ~60 m of a building or road
    urban = mask()
    d = ImageDraw.Draw(urban)
    for r in buildings:
        if len(r) > 2:
            d.polygon(r, fill=255)
    road_lines(d, widen=50)
    urban = urban.filter(ImageFilter.MaxFilter(9)).filter(ImageFilter.GaussianBlur(3))

    # port / industrial ground
    port = mask()
    d = ImageDraw.Draw(port)
    for r in industrial:
        if len(r) > 2:
            d.polygon(r, fill=255)
    port = port.filter(ImageFilter.MaxFilter(13)).filter(ImageFilter.MaxFilter(9))
    d = ImageDraw.Draw(port)
    world = json.load(open(os.path.join(a.out, "world.json")))
    for t in world.get("terminals", []):
        for lat, lon, brg in t["cranes"]:
            # container yard: 500 m deep behind the quay, centred inland of the crane
            b = math.radians(brg)
            cx, cy = lon - math.sin(b) * 280 / KX, lat - math.cos(b) * 280 / KY
            x, y = px(cx, cy)
            rr = 330 / mpp
            d.ellipse([x - rr, y - rr, x + rr, y + rr], fill=255)
    port = port.filter(ImageFilter.GaussianBlur(2))

    park, water = mask(), mask()
    dp, dw = ImageDraw.Draw(park), ImageDraw.Draw(water)
    for f in areas:
        rings = f["geometry"]["coordinates"]
        tgt = dw if f["properties"].get("kind") == "water" else dp
        tgt.polygon(ring_px(rings[0]), fill=255)
        for h in rings[1:]:
            tgt.polygon(ring_px(h), fill=0)
    park = park.filter(ImageFilter.GaussianBlur(1))

    roads_major, roads_minor = mask(), mask()
    dM, dm = ImageDraw.Draw(roads_major), ImageDraw.Draw(roads_minor)
    for f in roads:
        hw = f["properties"].get("highway")
        g = f["geometry"]
        lines = [g["coordinates"]] if g["type"] == "LineString" else (
            g["coordinates"] if g["type"] == "MultiLineString" else [])
        tgt = dM if hw in ("motorway", "trunk", "primary") else dm
        for ln in lines:
            if len(ln) > 1:
                tgt.line(ring_px(ln), fill=255, width=max(1, int(round(ROAD_W.get(hw, 7) / mpp))), joint="curve")

    bmask = mask()
    d = ImageDraw.Draw(bmask)
    for r in buildings:
        if len(r) > 2:
            d.polygon(r, fill=255)

    # ---- composite
    rng = np.random.default_rng(7)
    img = np.empty((H, W, 3), np.float32)
    img[:] = C["base"]
    # low-frequency variation so the ground is not a flat colour
    n = rng.random((H // 64 + 2, W // 64 + 2)).astype(np.float32)
    noise = np.asarray(Image.fromarray((n * 255).astype(np.uint8)).resize((W, H), Image.BICUBIC),
                       np.float32) / 255.0
    fine = rng.random((H // 4 + 1, W // 4 + 1)).astype(np.float32)
    fine = np.asarray(Image.fromarray((fine * 255).astype(np.uint8)).resize((W, H), Image.BILINEAR),
                      np.float32) / 255.0

    def over(m, colour, strength=1.0):
        al = (np.asarray(m, np.float32) / 255.0 * strength)[..., None]
        img[:] = img * (1 - al) + np.asarray(colour, np.float32) * al

    over(urban, C["urban"])
    over(port, C["port"])
    over(park, C["park"])
    over(water, C["water"])
    over(roads_minor, C["road"], .9)
    over(roads_major, C["road_major"])
    over(bmask, C["building"], .85)
    img *= (0.93 + 0.12 * noise)[..., None] * (0.97 + 0.06 * fine)[..., None]
    # border in the base colour: the viewer clamps the texture, so land outside
    # the painted area (Johor, Batam, Bintan) takes this colour
    b = 3 * SS
    img[:b], img[-b:], img[:, :b], img[:, -b:] = C["base"], C["base"], C["base"], C["base"]
    out = Image.fromarray(np.clip(img, 0, 255).astype(np.uint8)).resize((W // SS, H // SS), Image.LANCZOS)
    fn = os.path.join(a.out, "landuse.jpg")
    out.save(fn, quality=84, optimize=True, progressive=True)
    world["landuse"] = dict(file="landuse.jpg", lon0=LON0, lon1=LON1, lat0=LAT0, lat1=LAT1,
                            source="OpenStreetMap contributors (ODbL): buildings, roads, parks, water")
    with open(os.path.join(a.out, "world.json"), "w") as fh:
        json.dump(world, fh, separators=(",", ":"))
    print(f"wrote {fn}: {out.size[0]}x{out.size[1]}, {os.path.getsize(fn) / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
