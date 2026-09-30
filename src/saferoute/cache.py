"""Build-once caching: the board is static, snapshots and graphs are memoised.

The grid (land rasterisation, distance transform, TSS fields) takes about a
second and is built once per process. A snapshot's obstacle field is built
once per (snapshot, swing) and a network once per (vessel, mode, snapshot,
swing, cpa scale). SAFEROUTE_MAX_NETWORKS bounds the graph cache for hosts with
little memory (each strait graph is ~1.5 M edges, ~40 MB).
"""
from __future__ import annotations

import os
import threading
import time
from collections import OrderedDict
from typing import Dict, Optional, Tuple

from . import network as NW, obstacles as O, study_area as SA
from .seagrid import SeaGrid, build_seagrid

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
AIS_DIR = os.environ.get("SAFEROUTE_AIS_DIR", os.path.join(ROOT, "data", "ais"))
MAX_NETWORKS = int(os.environ.get("SAFEROUTE_MAX_NETWORKS", "8") or 8)

LOCK = threading.RLock()
_GRID: Dict[float, SeaGrid] = {}
_SNAPS: Dict[str, O.Snapshot] = {}
_FIELDS: Dict[Tuple, O.ObstacleField] = {}
_NETS: "OrderedDict[Tuple, NW.NavNetwork]" = OrderedDict()


def _log(msg, quiet=False):
    if not quiet:
        print(f"  [cache] {msg}", flush=True)


def grid(resolution: float = SA.DEFAULT_RESOLUTION_DEG, quiet: bool = False) -> SeaGrid:
    with LOCK:
        if resolution not in _GRID:
            t0 = time.time()
            _GRID[resolution] = build_seagrid(resolution)
            g = _GRID[resolution]
            _log(f"board {g.shape} at {resolution} deg, depth: {g.depth_model} "
                 f"({time.time() - t0:.1f} s)", quiet)
        return _GRID[resolution]


def available_snapshots() -> Dict[str, str]:
    """key -> label. 'synthetic' always; 'ais:<file>' for each CSV in data/ais."""
    out = {"synthetic": "Synthetic - ships in MPA anchorages"}
    if os.path.isdir(AIS_DIR):
        files = sorted(f for f in os.listdir(AIS_DIR) if f.lower().endswith(".csv"))
        if "latest.csv" in files:
            files.remove("latest.csv")
            files.insert(0, "latest.csv")
        for f in files:
            out[f"ais:{f}"] = f"Real AIS - {f}"
    for k in _SNAPS:
        if k.startswith("upload:"):
            out[k] = _SNAPS[k].label
    return out


def default_snapshot_key() -> str:
    keys = available_snapshots()
    return "ais:latest.csv" if "ais:latest.csv" in keys else "synthetic"


def snapshot(key: str = "synthetic", quiet: bool = False) -> O.Snapshot:
    with LOCK:
        if key not in _SNAPS:
            g = grid(quiet=quiet)
            if key == "synthetic":
                _SNAPS[key] = O.synthetic_snapshot(g)
            elif key.startswith("ais:"):
                path = os.path.join(AIS_DIR, os.path.basename(key[4:]))
                snap = O.load_snapshot_csv(path, source=os.path.basename(path), grid=g)
                snap.ships = [s for s in snap.ships if g.inside(s.lat, s.lon)]
                snap.label = f"Real AIS - {os.path.basename(path)}"
                _SNAPS[key] = snap
            else:
                raise KeyError(f"unknown snapshot {key!r}")
            _log(f"snapshot {key}: {len(_SNAPS[key])} stationary ships", quiet)
        return _SNAPS[key]


def add_uploaded_snapshot(name: str, text: str) -> str:
    g = grid(quiet=True)
    snap = O.load_snapshot_csv(text, source=name, is_text=True, grid=g)
    snap.ships = [s for s in snap.ships if g.inside(s.lat, s.lon)]
    snap.label = f"Uploaded - {name}"
    key = f"upload:{name}"
    with LOCK:
        _SNAPS[key] = snap
        for k in [k for k in _FIELDS if k[0] == key]:
            del _FIELDS[k]
        for k in [k for k in _NETS if k[2] == key]:
            del _NETS[k]
    return key


def field(key: str, swing: bool = True, quiet: bool = False) -> O.ObstacleField:
    with LOCK:
        fk = (key, swing)
        if fk not in _FIELDS:
            _FIELDS[fk] = O.obstacle_field(grid(quiet=quiet), snapshot(key, quiet), swing)
        return _FIELDS[fk]


def network(vessel: str, mode: str, snap_key: str = "synthetic", swing: bool = True,
            cpa_scale: float = 1.0, ship_types: Tuple[str, ...] = (),
            quiet: bool = False) -> NW.NavNetwork:
    key = (vessel, mode, snap_key, swing, round(cpa_scale, 3), tuple(sorted(ship_types)))
    with LOCK:
        if key in _NETS:
            _NETS.move_to_end(key)
            return _NETS[key]
        g = grid(quiet=quiet)
        if ship_types:
            snap = snapshot(snap_key, quiet).filtered(ship_types)
            obst = O.obstacle_field(g, snap, swing)
        else:
            obst = field(snap_key, swing, quiet)
        cfg = NW.config_for(mode, swing=swing, cpa_scale=cpa_scale)
        net = NW.build_network(g, vessel, cfg, obst)
        _log(f"network {vessel}/{mode}/{snap_key}: {net.n_nodes:,} nodes, "
             f"{net.n_edges:,} edges in {net.stats['build_s']:.1f} s", quiet)
        _NETS[key] = net
        while MAX_NETWORKS > 0 and len(_NETS) > MAX_NETWORKS:
            _NETS.popitem(last=False)
        return net


def clear():
    with LOCK:
        _GRID.clear(); _SNAPS.clear(); _FIELDS.clear(); _NETS.clear()
