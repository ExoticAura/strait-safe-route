"""Web app:  python -m saferoute.serve

Local chart server (standard-library http.server). Plans the safest and the
shortest route between a Singapore Strait entrance and a Singapore terminal,
around every stationary ship in the chosen snapshot.

    python -m saferoute.serve                 # http://127.0.0.1:8765, opens a browser
    python -m saferoute.serve --port 8080 --no-browser

API
    GET  /api/meta
    GET  /api/ships?snapshot=synthetic
    GET  /api/board?vessel=PANAMAX&snapshot=synthetic&swing=1
    GET  /api/route?origin=EAST&destination=SGPPT&vessel=PANAMAX&snapshot=synthetic
                   &modes=shortest,balanced,safest&swing=1&cpa_scale=1&types=
                   &alternatives=1&benchmark=0
         origin/destination may also be "lat,lon"
    GET  /api/route.csv?...same...&mode=safest          waypoints + turn radii as CSV
    POST /api/snapshot?name=my.csv   (body: CSV text)   add an AIS snapshot
    GET  /api/health
"""
from __future__ import annotations

import argparse
import io
import json
import os
import socketserver
import sys
import threading
import time
import traceback
import webbrowser
from http.server import SimpleHTTPRequestHandler
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

import numpy as np

from . import cache, network as NW, obstacles as O, routing as R, study_area as SA, viz
from .geo import M_PER_NM, haversine_nm, initial_bearing_deg

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
# The in-browser app (JS engine, 3D world, play/pause) is the one served at "/".
# The original server-backed chart stays available at /classic/.
STATIC_DIR = os.environ.get("SAFEROUTE_STATIC", os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "web_static"))
PAGE_HEAD = ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
             '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">'
             '</head><body>')
STATE: Dict[str, Any] = {"quiet": False}


def _json(obj) -> bytes:
    def default(o):
        if isinstance(o, np.integer):
            return int(o)
        if isinstance(o, np.floating):
            f = float(o)
            return None if (f != f or f in (float("inf"), float("-inf"))) else f
        if isinstance(o, np.bool_):
            return bool(o)
        if isinstance(o, np.ndarray):
            return o.tolist()
        raise TypeError(type(o))

    def clean(x):
        if isinstance(x, float):
            return None if (x != x or x in (float("inf"), float("-inf"))) else x
        if isinstance(x, dict):
            return {k: clean(v) for k, v in x.items()}
        if isinstance(x, (list, tuple)):
            return [clean(v) for v in x]
        return x
    return json.dumps(clean(obj), default=default, separators=(",", ":")).encode()


# --------------------------------------------------------------------------
def meta_payload() -> Dict[str, Any]:
    g = cache.grid(quiet=STATE["quiet"])
    return dict(
        bbox=dict(lat_min=float(g.lats[0]), lat_max=float(g.lats[-1]),
                  lon_min=float(g.lons[0]), lon_max=float(g.lons[-1])),
        resolution_deg=g.resolution_deg, grid=[g.ny, g.nx],
        cell_nm=list(g.cell_size_nm()),
        depth_model=g.depth_model, depth_source=g.depth_source,
        land_source=g.land_source,
        land=viz.land_geojson(), tss=viz.tss_geojson(),
        depth=viz.depth_payload(g, stride=2),
        ports=[dict(code=c, name=p.name, lat=p.app_lat, lon=p.app_lon,
                    berth_lat=p.lat, berth_lon=p.lon, kind=p.kind,
                    max_draught_m=p.max_draught_m, berth_name=p.berth_name,
                    quay_hdg=None if p.kind != "terminal" else p.quay_hdg,
                    pilot=[list(x) for x in p.pilot],
                    berths=SA.BERTH_OPTIONS.get(c, [])) for c, p in SA.PORTS.items()],
        berth_source=SA.BERTH_SOURCE,
        anchorages=[dict(code=c, name=n, charted_m=d, lat=la, lon=lo,
                         use=SA.anchorage_use(c)) for c, n, d, la, lo in SA.ANCHORAGE_POINTS],
        pilot_boarding=[dict(code=c, name=n, lat=la, lon=lo)
                        for c, n, la, lo in SA.PILOT_BOARDING],
        vessels=[v.as_dict() for v in SA.VESSEL_CLASSES.values()],
        default_vessel=SA.DEFAULT_VESSEL,
        modes=NW.MODES,
        cpa_rules={k: dict(zone=r.zone, context=r.context, hard_nm=r.hard_nm,
                           target_nm=r.target_nm) for k, r in SA.CPA_RULES.items()},
        board_labels=NW.BOARD_LABELS,
        snapshots=cache.available_snapshots(),
        default_snapshot=cache.default_snapshot_key(),
        od_pairs=[list(x) for x in SA.OD_PAIRS],
        planning_speed_kn=SA.PLANNING_SPEED_KN,
        ship_types=O.SHIP_TYPES,
    )


def ships_payload(key: str, swing: bool = True) -> Dict[str, Any]:
    snap = cache.snapshot(key, quiet=STATE["quiet"])
    ships = []
    for s in snap.ships:
        la, lo, r = s.envelope(swing)
        d = s.as_dict()
        d.update(env=[round(la, 6), round(lo, 6), round(r, 4)],
                 lat=round(s.lat, 6), lon=round(s.lon, 6))
        ships.append(d)
    return dict(key=key, label=snap.label, source=snap.source,
                captured_utc=snap.captured_utc, notes=snap.notes,
                counts=snap.counts(), n=len(snap), ships=ships)


def board_payload(vessel: str, key: str, swing: bool, cpa_scale: float,
                  types: tuple) -> Dict[str, Any]:
    net = cache.network(vessel, "shortest", key, swing, cpa_scale, types,
                        quiet=STATE["quiet"])
    p = viz.pack_int8(net.board, net.grid)
    p["labels"] = NW.BOARD_LABELS
    p["counts"] = {NW.BOARD_LABELS[k]: int((net.board == k).sum()) for k in NW.BOARD_LABELS}
    return p


def _parse_point(x: str):
    if x in SA.PORTS:
        return x
    try:
        la, lo = (float(t) for t in x.split(","))
        return (la, lo)
    except ValueError:
        raise KeyError(f"unknown origin/destination {x!r}")


def _point_label(x):
    if isinstance(x, str):
        return SA.PORTS[x].name
    return f"{x[0]:.4f} N, {x[1]:.4f} E"


def _endpoint_check(net: NW.NavNetwork, x) -> Optional[str]:
    """Why an endpoint cannot be used by this ship, or None."""
    la, lo = SA.port(x).approach if isinstance(x, str) else x
    g = net.grid
    if isinstance(x, str) and SA.PORTS[x].kind == "terminal":
        port, ship = SA.PORTS[x], net.vessel
        if ship.draught_m + max(1.0, 0.1 * ship.draught_m) > port.max_draught_m:
            return (f"{port.name} is not reachable for this ship ({ship.name}): its maintained depth "
                    f"is {port.max_draught_m:.1f} m, the ship needs {ship.draught_m + max(1.0, 0.1 * ship.draught_m):.1f} m incl. UKC")
    if not g.inside(la, lo):
        return f"{_point_label(x)} is outside the study area"
    try:
        iy, ix = g.nearest_navigable(la, lo, net.navigable)
    except ValueError:
        return f"no navigable water near {_point_label(x)}"
    snap_nm = float(haversine_nm(la, lo, g.lats[iy], g.lons[ix]))
    if snap_nm > 0.6:
        cy, cx = g.to_index(la, lo)
        why = NW.BOARD_LABELS.get(int(net.board[cy, cx]), "blocked")
        need = float(net.req_depth[cy, cx])
        extra = (f" (charted {g.depth_m[cy, cx]:.1f} m, ship needs {need:.1f} m incl. UKC)"
                 if why == "too shallow" else "")
        return (f"{_point_label(x)} is not reachable for this ship ({net.vessel.name}): "
                f"{why}{extra}; nearest usable water is {snap_nm:.1f} NM away")
    return None


def route_payload(q: Dict[str, str]) -> Dict[str, Any]:
    origin = _parse_point(q.get("origin", "EAST"))
    dest = _parse_point(q.get("destination", "SGPPT"))
    vessel = q.get("vessel", SA.DEFAULT_VESSEL)
    key = q.get("snapshot") or cache.default_snapshot_key()
    swing = q.get("swing", "1") not in ("0", "false")
    cpa_scale = float(q.get("cpa_scale", 1.0))
    types = tuple(t for t in (q.get("types") or "").split(",") if t)
    modes = [m for m in (q.get("modes") or "shortest,balanced,safest").split(",") if m in NW.MODES]
    alternatives = int(q.get("alternatives", 1))
    snap = cache.snapshot(key, quiet=STATE["quiet"])
    if types:
        snap = snap.filtered(types)

    out_plans, notes = [], []
    net0 = None
    for mode in modes:
        net = cache.network(vessel, mode, key, swing, cpa_scale, types, quiet=STATE["quiet"])
        net0 = net0 or net
        problems = [p for p in (_endpoint_check(net, origin), _endpoint_check(net, dest)) if p]
        if problems:
            return dict(found=False, reason="; ".join(problems), origin=q.get("origin"),
                        destination=q.get("destination"))
        plans = R.plan_route(net, origin, dest, snap,
                             alternatives=alternatives if mode == modes[-1] else 1)
        for p in plans:
            row = dict(label=p.label, mode=p.mode, metrics=p.metrics,
                       coords=viz.round_coords(p.coords, 6),
                       waypoints=viz.round_coords(p.waypoints, 6), turn_radii=p.turn_radii,
                       encounters=p.encounters, search=p.search)
            out_plans.append(row)
    if not out_plans:
        return dict(found=False, reason="no route satisfies every hard limit for this ship "
                    "(land, depth + UKC, shore clearance, ship exclusion zones)",
                    origin=q.get("origin"), destination=q.get("destination"))

    bench = None
    if q.get("benchmark", "0") in ("1", "true"):
        bench = R.compare_search(net0, origin, dest, reps=2)

    return dict(found=True, origin=q.get("origin"), destination=q.get("destination"),
                origin_label=_point_label(origin), destination_label=_point_label(dest),
                vessel=vessel, snapshot=key, snapshot_label=snap.label, swing=swing,
                cpa_scale=cpa_scale, types=list(types),
                network=dict(nodes=net0.n_nodes, edges=net0.n_edges,
                             build_s=round(net0.stats["build_s"], 2),
                             resolution_deg=net0.grid.resolution_deg),
                plans=out_plans, benchmark=bench, notes=notes)


def route_csv(q: Dict[str, str]) -> bytes:
    r = route_payload(dict(q, modes=q.get("mode", "safest")))
    if not r.get("found"):
        raise ValueError(r.get("reason"))
    p = r["plans"][0]
    buf = io.StringIO()
    # the passage plan: waypoints, and the turn radius the ship uses at each
    buf.write("waypoint,lat,lon,leg_nm,course_deg,cumulative_nm,turn_radius_m\n")
    cum = 0.0
    cs, radii = p["waypoints"], p["turn_radii"]
    for i, (la, lo) in enumerate(cs):
        rad = "" if radii[i] is None else f"{radii[i]:.0f}"
        if i:
            leg = float(haversine_nm(cs[i - 1][0], cs[i - 1][1], la, lo))
            crs = initial_bearing_deg(cs[i - 1][0], cs[i - 1][1], la, lo)
            cum += leg
            buf.write(f"{i},{la:.6f},{lo:.6f},{leg:.3f},{crs:.1f},{cum:.3f},{rad}\n")
        else:
            buf.write(f"{i},{la:.6f},{lo:.6f},,,0.000,{rad}\n")
    return buf.getvalue().encode()


# --------------------------------------------------------------------------
class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=WEB_DIR, **kw)

    def translate_path(self, path):
        # /classic/... -> the server-backed chart; everything else -> web_static
        p = urlparse(path).path
        if p.startswith("/classic"):
            self.directory = WEB_DIR
            path = path[len("/classic"):] or "/"
        elif os.path.isdir(STATIC_DIR):
            self.directory = STATIC_DIR
        return super().translate_path(path)

    def end_headers(self):
        p = urlparse(self.path).path
        if "/world/b/" in p or "/vendor/" in p:
            self.send_header("Cache-Control", "public, max-age=86400")
        super().end_headers()

    def log_message(self, fmt, *args):
        if not STATE["quiet"]:
            sys.stderr.write("  %s\n" % (fmt % args))

    def _send(self, payload: bytes, ctype="application/json", code=200, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if u.path == "/api/meta":
                return self._send(_json(meta_payload()))
            if u.path == "/api/ships":
                return self._send(_json(ships_payload(
                    q.get("snapshot") or cache.default_snapshot_key(),
                    q.get("swing", "1") not in ("0", "false"))))
            if u.path == "/api/board":
                return self._send(_json(board_payload(
                    q.get("vessel", SA.DEFAULT_VESSEL),
                    q.get("snapshot") or cache.default_snapshot_key(),
                    q.get("swing", "1") not in ("0", "false"),
                    float(q.get("cpa_scale", 1.0)),
                    tuple(t for t in (q.get("types") or "").split(",") if t))))
            if u.path == "/api/route":
                return self._send(_json(route_payload(q)))
            if u.path == "/api/route.csv":
                name = f"route_{q.get('origin', 'A')}_{q.get('destination', 'B')}_{q.get('mode', 'safest')}.csv"
                return self._send(route_csv(q), "text/csv",
                                  extra={"Content-Disposition": f'attachment; filename="{name}"'})
            if u.path == "/api/health":
                return self._send(_json(dict(ok=True)))
            if u.path in ("/", "/index.html") and os.path.isfile(os.path.join(STATIC_DIR, "index.html")):
                # web_static/index.html is a page body (it is also published as
                # a claude.ai artifact, which adds the skeleton); wrap it here.
                with open(os.path.join(STATIC_DIR, "index.html"), encoding="utf-8") as fh:
                    page = (PAGE_HEAD + fh.read() + "</body></html>").encode()
                return self._send(page, "text/html; charset=utf-8")
        except BrokenPipeError:
            return
        except Exception as exc:                            # noqa: BLE001
            traceback.print_exc()
            return self._send(_json(dict(error=str(exc), type=type(exc).__name__)), code=500)
        return super().do_GET()

    def do_POST(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if u.path == "/api/snapshot":
                n = int(self.headers.get("Content-Length", 0))
                if n <= 0 or n > 50_000_000:
                    raise ValueError("send the CSV as the request body (max 50 MB)")
                text = self.rfile.read(n).decode("utf-8", errors="replace")
                name = os.path.basename(q.get("name") or "upload.csv")[:80]
                key = cache.add_uploaded_snapshot(name, text)
                return self._send(_json(ships_payload(key)))
        except Exception as exc:                            # noqa: BLE001
            traceback.print_exc()
            return self._send(_json(dict(error=str(exc), type=type(exc).__name__)), code=400)
        self.send_error(404)


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main(argv=None) -> None:
    hosted = bool(os.environ.get("PORT"))
    ap = argparse.ArgumentParser(prog="python -m saferoute.serve")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8765)))
    ap.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0" if hosted else "127.0.0.1"))
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args(argv)
    STATE["quiet"] = a.quiet
    print("Singapore Strait safe-route planner - building the board", flush=True)
    cache.grid(quiet=a.quiet)

    def _warm():
        try:
            key = cache.default_snapshot_key()
            for m in NW.MODES:
                cache.network(SA.DEFAULT_VESSEL, m, key, quiet=a.quiet)
        except Exception:                                   # noqa: BLE001
            traceback.print_exc()
    threading.Thread(target=_warm, daemon=True).start()

    url = f"http://{'127.0.0.1' if a.host == '0.0.0.0' else a.host}:{a.port}/"
    with Server((a.host, a.port), Handler) as httpd:
        print(f"\n  Chart ready at {url}\n  Ctrl-C to stop\n", flush=True)
        if not (a.no_browser or hosted):
            threading.Timer(0.6, lambda: webbrowser.open(url)).start()
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n  stopped")


if __name__ == "__main__":
    main()
