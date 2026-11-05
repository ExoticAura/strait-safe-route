"""Add land reclaimed 2016-2025 to the study area's coastline.

    python scripts/update_coastline.py path/to/reclamation.gpkg [--plot out.png]

The base coastline (src/saferoute/data/sg_land.geojson) is the OpenStreetMap
land extract from 2017, topped up where 2026 OSM building footprints stand
outside it. Reclaimed land with nothing built on it yet - most of Tuas Port's
new wharves and yards - was still missing.

Input: reclamation polygons detected from Sentinel-2 annual water/land masks
(persistent water -> land, 10 m pixels, clipped to Singapore), from the
sg-reclamation-habitat project (github.com/papersheep025/sg-reclamation-habitat,
data/processed/reclamation.gpkg, EPSG:3414, one polygon per patch with its
year). Contains modified Copernicus Sentinel data 2016-2026.

What it does: reprojects the polygons to WGS84, drops slivers under 1 ha,
closes 20 m gaps between neighbouring patches, smooths the 10 m pixel
steps (12 m tolerance), unions them with the current
land and writes sg_land.geojson back (idempotent: running it twice changes
nothing). Prints the land added per area of the strait.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT = os.path.join(ROOT, "src", "saferoute", "data", "sg_land.geojson")

AREAS = {   # lon_min, lat_min, lon_max, lat_max: for the report only
    "Tuas": (103.59, 1.20, 103.67, 1.30),
    "Jurong Island / Pasir Panjang": (103.67, 1.20, 103.80, 1.32),
    "Keppel / city / Marina": (103.80, 1.22, 103.90, 1.31),
    "East coast / Changi": (103.90, 1.28, 104.10, 1.45),
}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("gpkg")
    ap.add_argument("--min-ha", type=float, default=1.0)
    ap.add_argument("--plot")
    a = ap.parse_args(argv)

    import geopandas as gpd
    from shapely.geometry import box, mapping, shape
    from shapely.ops import unary_union

    rec = gpd.read_file(a.gpkg)
    rec = rec[rec.geometry.area >= a.min_ha * 1e4]
    # close the 10 m-pixel seams between adjacent patches, in metres (SVY21)
    merged = unary_union(list(rec.geometry)).buffer(20).buffer(-20).simplify(12)   # drop 10 m pixel steps
    merged = gpd.GeoSeries([merged], crs=rec.crs).to_crs(4326).iloc[0]

    fc = json.load(open(OUT))
    old = shape(fc["features"][0]["geometry"])
    new = unary_union([old, merged]).buffer(0)
    added = new.difference(old)

    k = 111.32 * 110.57 * 0.99975          # km^2 per deg^2 near 1.3 N
    print(f"reclamation polygons: {len(rec)} (>= {a.min_ha} ha), "
          f"{merged.area * k:.1f} km2; new land not already in the coastline: {added.area * k:.2f} km2")
    for name, bb in AREAS.items():
        print(f"  {name:32s} +{added.intersection(box(*bb)).area * k:5.2f} km2")

    src = fc["features"][0]["properties"].get("source", "")
    if "Sentinel-2" not in src:
        src += ("; plus land reclaimed 2016-2025 detected from Sentinel-2 (contains modified "
                "Copernicus Sentinel data; polygons from github.com/papersheep025/sg-reclamation-habitat)")
    geom = new if new.geom_type == "MultiPolygon" else new.__class__([new])
    fc["features"][0] = {"type": "Feature", "properties": {"source": src},
                         "geometry": json.loads(json.dumps(mapping(geom)))}
    # 6 decimals ~ 0.1 m: plenty, and keeps the file small
    def rnd(c):
        return [rnd(x) for x in c] if isinstance(c[0], (list, tuple)) else [round(c[0], 6), round(c[1], 6)]
    fc["features"][0]["geometry"]["coordinates"] = rnd(fc["features"][0]["geometry"]["coordinates"])
    json.dump(fc, open(OUT, "w"), separators=(",", ":"))
    print("->", OUT)

    if a.plot:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        fig, ax = plt.subplots(figsize=(10, 6))
        for g, col in ((old, "#d8c9a0"), (added, "#c0392b")):
            for p in getattr(g, "geoms", [g]):
                if p.geom_type == "Polygon":
                    x, y = p.exterior.xy
                    ax.fill(x, y, color=col, lw=0)
        ax.set_xlim(103.58, 103.72); ax.set_ylim(1.18, 1.32); ax.set_aspect(1)
        ax.set_title("Coastline near Tuas: 2017 OSM (+2026 buildings) in tan, reclaimed 2016-2025 in red")
        plt.savefig(a.plot, dpi=110, bbox_inches="tight")


if __name__ == "__main__":
    main()
