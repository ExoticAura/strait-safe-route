"""Capture a REAL snapshot of the stationary ships around Singapore from AIS.

Run this on your own laptop (it needs internet access to aisstream.io):

    pip install websockets
    set AISSTREAM_API_KEY=...            (Windows)   export AISSTREAM_API_KEY=...   (mac/Linux)
    python scripts/capture_ais_snapshot.py --minutes 15

Get a free key by signing in at https://aisstream.io (GitHub login) -> API Keys.

What it does
------------
1. Opens aisstream.io's websocket for the study-area bounding box and listens
   for position reports and static data (name, AIS type, length, beam,
   draught) for `--minutes`. Anchored Class A ships report position every
   3 minutes and static data every 6 minutes, so 15 minutes catches nearly all
   of them; 30 minutes is safer for dimensions.
2. Keeps every vessel that is anchored (NavStatus 1), moored (5), aground (6)
   or never exceeded `--sog-max` knots during the capture. Ships under way
   are dropped - they are traffic, not fixed obstacles.
3. Writes  data/ais/snapshot_<UTC time>.csv  and  data/ais/latest.csv  in the
   snapshot schema (saferoute.obstacles.SNAPSHOT_COLUMNS). The app uses
   latest.csv automatically ("Real AIS" in the Ship snapshot selector).

AIS coverage from aisstream's terrestrial receivers is good in the Singapore
Strait but not guaranteed; the file records how many vessels were heard.
AIS type codes 70-79 are "cargo" and do not distinguish container ships from
bulk carriers - see the note in obstacles.py.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

from saferoute import obstacles as O          # noqa: E402
from saferoute import study_area as SA        # noqa: E402

URL = "wss://stream.aisstream.io/v0/stream"


async def capture(api_key: str, minutes: float, verbose: bool = True):
    import websockets                          # pip install websockets
    b = SA.BBOX
    sub = {"APIKey": api_key,
           "BoundingBoxes": [[[b["lat_min"], b["lon_min"]], [b["lat_max"], b["lon_max"]]]],
           "FilterMessageTypes": ["PositionReport", "ShipStaticData",
                                  "StandardClassBPositionReport",
                                  "ExtendedClassBPositionReport"]}
    state = {}
    t_end = time.time() + minutes * 60
    n = 0
    async with websockets.connect(URL, ping_interval=20, max_size=2 ** 22) as ws:
        await ws.send(json.dumps(sub))
        last = 0.0
        while time.time() < t_end:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=30)
            except asyncio.TimeoutError:
                continue
            msg = json.loads(raw)
            if "error" in msg:
                raise RuntimeError(f"aisstream.io: {msg['error']}")
            if O.merge_aisstream_message(state, msg):
                n += 1
            if verbose and time.time() - last > 15:
                last = time.time()
                left = max(0, t_end - time.time()) / 60
                print(f"  {n:6d} messages, {len(state):4d} vessels heard, "
                      f"{left:4.1f} min left", flush=True)
    return state


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--minutes", type=float, default=15.0)
    ap.add_argument("--sog-max", type=float, default=0.5)
    ap.add_argument("--api-key", default=os.environ.get("AISSTREAM_API_KEY", ""))
    ap.add_argument("--out-dir", default=os.path.join(ROOT, "data", "ais"))
    a = ap.parse_args(argv)
    if not a.api_key:
        sys.exit("Set AISSTREAM_API_KEY or pass --api-key (free at https://aisstream.io).")

    print(f"Listening to aisstream.io over the Singapore Strait for {a.minutes:g} min ...")
    state = asyncio.run(capture(a.api_key, a.minutes))
    snap = O.snapshot_from_stream_state(state, sog_max_kn=a.sog_max)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%MZ")
    for s in snap.ships:
        s.timestamp_utc = s.timestamp_utc or stamp
    os.makedirs(a.out_dir, exist_ok=True)
    path = os.path.join(a.out_dir, f"snapshot_{stamp}.csv")
    O.write_snapshot_csv(snap, path)
    O.write_snapshot_csv(snap, os.path.join(a.out_dir, "latest.csv"))
    print(f"\n{len(state)} vessels heard; {len(snap)} stationary written to\n  {path}\n"
          f"  {os.path.join(a.out_dir, 'latest.csv')}")
    print("By AIS category:", snap.counts())


if __name__ == "__main__":
    main()
