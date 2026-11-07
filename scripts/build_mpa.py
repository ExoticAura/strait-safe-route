#!/usr/bin/env python3
"""One command after downloading MPA's GeoSpace-Sea layers: rebuild everything
that uses them.

    python scripts/download_gss.py     # once (needs MPA_API_KEY)
    python scripts/build_mpa.py        # berths + dashboard data

1. data/gss/derived/land_mpa.geojson  the coastline corrected by the chart
2. src/saferoute/data/berths_mpa.json  MPA's named berths, pilotage tracks, depths
3. web_static/data_mpa.json, mpa.json  the board and chart layers for the dashboard

All three are derived from MPA data and git-ignored. Without data/gss the
model runs on GEBCO + OpenStreetMap as before (web_static/data.json).
"""
import os
import runpy
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "src"))

from saferoute import mpa, seagrid as SG          # noqa: E402

if not mpa.enabled():
    sys.exit(f"MPA layers not found in {mpa.GSS_DIR} (run scripts/download_gss.py first), "
             "or SAFEROUTE_MPA=0 is set")
print("1/3 coastline ->", mpa.land_geojson(SG.LAND_GEOJSON))
print("2/3 berths")
sys.argv = ["build_berths.py"]
runpy.run_path(os.path.join(HERE, "build_berths.py"), run_name="__main__")
print("3/3 dashboard data (berths are re-read in a fresh process)")
os.execv(sys.executable, [sys.executable, os.path.join(HERE, "export_web_data.py")])
