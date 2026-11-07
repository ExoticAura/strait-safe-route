#!/usr/bin/env python3
"""Download GeoSpace-Sea (MPA) datasets as zipped files.

The API key is read ONLY from the environment variable MPA_API_KEY.
Never put the key in this file, in a .env that is committed, or in chat.

Usage (PowerShell or bash), from the repo folder:
    python scripts/download_gss.py                 # mid-term set (see MIDTERM)
    python scripts/download_gss.py depthsa seabeda # named layers
    python scripts/download_gss.py --all           # every layer
    python scripts/download_gss.py --list          # print layer names, no download
    python scripts/download_gss.py --force         # re-download existing zips

Output: data/gss/<layer>.zip and the unzipped files in data/gss/<layer>/.
data/gss/ is git-ignored: check the GeoSpace-Sea Terms of Use before sharing it.
The datasets are derived from IHO S-57 and are NOT to be used for navigation.
Standard library only.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

BASE = "https://oceans-x.mpa.gov.sg/api/v1/gssdataset/mpa/1.0.0"
OUT = Path("data/gss")

# Layer name -> endpoint is "/<name>-zip" (from the published swagger).
MIDTERM = [
    "depthsa", "depthsl",
    "tracksandroutesa", "tracksandroutesl", "tracksandroutesp",
    "dangersa", "dangersl", "dangersp",
    "coastlinea", "coastlinel",
    "regulatedareasandlimitsa",
    "portsandservicesa", "portsandservicesl", "portsandservicesp",
]
LATER = [
    "tidesandvariationsa", "tidesandvariationsp",
    "seabeda", "seabedp",
    "offshoreinstallationsa", "offshoreinstallationsl",
    "militaryfeaturesa", "aidstonavigationp",
    "culturalfeaturesa", "culturalfeaturesl", "culturalfeaturesp",
    "naturalfeaturesa", "naturalfeaturesl", "naturalfeaturesp",
]
SP1 = [f"singaporean-chart-sp1-edition-{n}" for n in range(1, 6)]
ALL = MIDTERM + LATER + SP1


def fetch(layer: str, key: str, retries: int = 4) -> bytes | None:
    """GET one layer. Returns zip bytes, or None if the API returns 204."""
    url = f"{BASE}/{layer}-zip"
    delay = 5.0
    for attempt in range(1, retries + 1):
        req = urllib.request.Request(url, headers={"apikey": key, "User-Agent": "saferoute-gss-downloader"})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                if r.status == 204:
                    return None
                return r.read()
        except urllib.error.HTTPError as e:
            # 400 is also used for "rate limit exceeded"; 429/5xx are transient.
            if e.code in (400, 429, 500, 502, 503, 504) and attempt < retries:
                print(f"    HTTP {e.code}, retry {attempt}/{retries - 1} in {delay:.0f}s")
                time.sleep(delay)
                delay *= 2
                continue
            if e.code in (401, 403):
                sys.exit(f"HTTP {e.code}: the API key was rejected. Check MPA_API_KEY (and that the key is still active).")
            if e.code == 404:
                print(f"    HTTP 404: no such layer '{layer}'")
                return None
            raise
        except urllib.error.URLError as e:
            if attempt < retries:
                print(f"    network error ({e.reason}), retry {attempt}/{retries - 1} in {delay:.0f}s")
                time.sleep(delay)
                delay *= 2
                continue
            raise
    return None


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("layers", nargs="*", help="layer names, e.g. depthsa (default: mid-term set)")
    ap.add_argument("--all", action="store_true", help="download every layer")
    ap.add_argument("--list", action="store_true", help="print layer names and exit")
    ap.add_argument("--force", action="store_true", help="re-download even if the zip exists")
    ap.add_argument("--out", default=str(OUT), help="output folder (default data/gss)")
    a = ap.parse_args()

    if a.list:
        print("mid-term:", ", ".join(MIDTERM))
        print("later:   ", ", ".join(LATER))
        print("SP1:     ", ", ".join(SP1))
        return

    layers = [x.lower() for x in a.layers] or (ALL if a.all else MIDTERM)
    key = os.environ.get("MPA_API_KEY", "").strip()
    if not key:
        sys.exit("MPA_API_KEY is not set. In PowerShell: setx MPA_API_KEY \"<key>\", then open a NEW terminal.")

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    ok = skipped = failed = 0
    for i, layer in enumerate(layers, 1):
        zpath = out / f"{layer}.zip"
        print(f"[{i}/{len(layers)}] {layer}")
        if zpath.exists() and not a.force:
            print("    already downloaded (use --force to refresh)")
            skipped += 1
            continue
        try:
            data = fetch(layer, key)
        except Exception as e:  # keep going so one bad layer does not stop the rest
            print(f"    FAILED: {e}")
            failed += 1
            continue
        if data is None:
            print("    no data returned")
            failed += 1
            continue
        zpath.write_bytes(data)
        try:
            with zipfile.ZipFile(zpath) as z:
                names = z.namelist()
                z.extractall(out / layer)
        except zipfile.BadZipFile:
            print(f"    FAILED: response was not a zip ({len(data)} bytes); first bytes: {data[:80]!r}")
            zpath.unlink()
            failed += 1
            continue
        kinds = sorted({Path(n).suffix.lower() for n in names if Path(n).suffix})
        print(f"    {len(data) / 1e6:.2f} MB, {len(names)} files, types: {' '.join(kinds)}")
        ok += 1
        time.sleep(1.0)  # be gentle: the API has a rate limit
    print(f"done: {ok} downloaded, {skipped} skipped, {failed} failed -> {out}")


if __name__ == "__main__":
    main()
