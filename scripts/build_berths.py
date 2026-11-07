"""Place each terminal's berth on its quay and draw the pilotage track to it.

    python scripts/build_berths.py      -> src/saferoute/data/berths.json

For each terminal:

1. Berth. A reference position next to the terminal's quay (chosen from the
   terminal layout, e.g. the Pasir Panjang inner-basin quay) is snapped to the
   nearest edge of the modelled coastline (OpenStreetMap). The quay line gives
   the berth heading; the berth itself is 40 m off the quay line, seaward
   (the ship's centreline alongside, fenders and half a beam).
2. Pilotage track. The route-planning board is ~220 m square and keeps ships
   a shore distance off every coast, so it cannot reach into a basin or up to
   a quay. The last stretch - from open water to the berth - is the pilotage
   leg: a shortest path on a 22 m raster of the same coastline, from the berth
   to the terminal's pilotage start in open water (where the route network
   connects), preferring mid-channel (cost rises as clearance falls). It is
   then simplified to a few waypoints that keep at least 12 m off the land.

The dredged pocket of each terminal (seagrid.apply_dredged_pockets) covers
the berth, the pilotage track and its start, so the board and the track
agree about depth.

With MPA's chart layers in data/gss (mpa.py) the berths are MPA's own named
berths instead (PortsAndServicesP: every berth of Tuas F2, Jurong Terminal,
Pasir Panjang, Keppel and Brani), on the chart-corrected coastline, each with
its charted depth (DepthsA, usually the berth's dredged area) and the least
charted depth along its pilotage track. One shortest-path search per terminal
from the pilotage start reaches every berth. Output: data/berths_mpa.json
(git-ignored: derived from MPA data); the terminal's default berth is the one
nearest the reference position below.
"""
from __future__ import annotations

import json
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

import numpy as np                                               # noqa: E402
from matplotlib.path import Path                                 # noqa: E402
from scipy import ndimage                                        # noqa: E402
from scipy.sparse import coo_matrix                              # noqa: E402
from scipy.sparse.csgraph import dijkstra                        # noqa: E402
from shapely.geometry import LineString, Point, shape            # noqa: E402

OUT = os.path.join(ROOT, "src", "saferoute", "data", "berths.json")
OUT_MPA = os.path.join(ROOT, "src", "saferoute", "data", "berths_mpa.json")

# Per terminal: a reference position next to its quay (lat, lon), a label,
# and the pilotage start - open water outside the terminal that the route
# network reaches (the terminal's previous approach position).
REFS = {
    "SGTUA": ((1.2440, 103.6350), "Tuas Port - middle finger pier, south berth", (1.2400, 103.6600)),
    "SGJUR": ((1.2980, 103.7190), "Jurong Port - main wharf", (1.2630, 103.7400)),
    "SGPPT": ((1.2640, 103.7800), "Pasir Panjang - inner basin, east quay", (1.2380, 103.7790)),
    "SGKEP": ((1.2615, 103.8405), "Brani Terminal - north-east wharf", (1.2460, 103.8620)),
}
# With the MPA chart the pilotage starts sit where the charted water is
# >= 15 m over a 3 x 3-cell (~660 m) block or more, on the line of the
# approach channel, so the route's last turn onto
# the pilotage track is not made over a shallow band edge.
MPA_STARTS = {"SGTUA": (1.2400, 103.6580), "SGJUR": (1.2640, 103.7400),
              "SGPPT": (1.2480, 103.7820), "SGKEP": (1.2500, 103.8580)}
BERTH_OFF_M = 40.0
FINE_DEG = 0.0002
WINDOW_DEG = 0.05
MIN_CLEAR_M = 25.0
KY = 110574.0


def main():
    from saferoute import mpa
    if mpa.enabled() and "--legacy" not in sys.argv:
        return main_mpa()
    from saferoute import seagrid as SG, study_area as SA
    land = shape(json.load(open(SG.LAND_GEOJSON))["features"][0]["geometry"])
    out = {}
    for code, ((rla, rlo), label, (sla, slo)) in REFS.items():
        port = SA.PORTS[code]
        kx = 111320.0 * math.cos(math.radians(rla))
        # ---- 1. berth on the quay
        p = Point(rlo, rla)
        ext = min((gm.exterior for gm in land.geoms), key=lambda e: e.distance(p))
        s = ext.project(p)
        q = ext.interpolate(s)
        a, b = ext.interpolate(max(0.0, s - 0.0004)), ext.interpolate(s + 0.0004)
        dx, dy = (b.x - a.x) * kx, (b.y - a.y) * KY
        n = math.hypot(dx, dy)
        dx, dy = dx / n, dy / n
        for sgn in (1, -1):
            nx, ny = sgn * dy, -sgn * dx
            if not land.contains(Point(q.x + nx * 60 / kx, q.y + ny * 60 / KY)):
                break
        berth = (q.y + ny * BERTH_OFF_M / KY, q.x + nx * BERTH_OFF_M / kx)
        quay_hdg = (math.degrees(math.atan2(dx, dy)) + 360) % 360

        # ---- 2. largest ship the terminal takes (reported with the track)
        cls = [v for v in SA.VESSEL_CLASSES.values()
               if v.draught_m + max(1.0, 0.1 * v.draught_m) <= port.max_draught_m]
        big = max(cls, key=lambda v: v.loa_m)

        # ---- 3. fine raster round the berth
        half = max(WINDOW_DEG, abs(sla - berth[0]) + .015, abs(slo - berth[1]) + .015)
        la0, lo0 = berth[0] - half, berth[1] - half
        nf = int(2 * half / FINE_DEG) + 1
        lats = la0 + np.arange(nf) * FINE_DEG
        lons = lo0 + np.arange(nf) * FINE_DEG
        LA, LO = np.meshgrid(lats, lons, indexing="ij")
        pts = np.column_stack([LO.ravel(), LA.ravel()])
        water = np.ones(nf * nf, bool)
        for gm in land.geoms:
            if not gm.intersects(Point(berth[1], berth[0]).buffer(half * 1.5)):
                continue
            inside = Path(np.asarray(gm.exterior.coords)).contains_points(pts)
            for h in gm.interiors:
                inside &= ~Path(np.asarray(h.coords)).contains_points(pts)
            water &= ~inside
        water = water.reshape(nf, nf)
        clear = ndimage.distance_transform_edt(water) * FINE_DEG * KY
        near_berth = np.hypot((LA - berth[0]) * KY, (LO - berth[1]) * kx) < 80
        ok = water & ((clear >= MIN_CLEAR_M) | near_berth)
        # target: the pilotage start (within 150 m of it)
        target = ok & (np.hypot((LA - sla) * KY, (LO - slo) * kx) < 150)

        idx = -np.ones((nf, nf), int)
        idx[ok] = np.arange(ok.sum())
        rows, cols, w = [], [], []
        cm = 1.0 + 150.0 / (clear + 10.0)
        for ddy, ddx in ((0, 1), (1, 0), (1, 1), (1, -1)):
            sa = (slice(0, nf - ddy), slice(max(0, -ddx), nf - max(0, ddx)))
            sb = (slice(ddy, nf), slice(max(0, ddx), nf - max(0, -ddx)))
            A, B = idx[sa], idx[sb]
            m = (A >= 0) & (B >= 0)
            step = math.hypot(ddy * KY, ddx * kx) * FINE_DEG
            rows += list(A[m]); cols += list(B[m]); w += list(step * .5 * (cm[sa][m] + cm[sb][m]))
        G = coo_matrix((w, (rows, cols)), shape=(ok.sum(),) * 2).tocsr()
        by, bx = int(round((berth[0] - la0) / FINE_DEG)), int(round((berth[1] - lo0) / FINE_DEG))
        start = idx[by, bx]
        assert start >= 0, code
        dist, pred = dijkstra(G, directed=False, indices=start, return_predecessors=True)
        tgt_ids = idx[target]
        tgt_ids = tgt_ids[np.isfinite(dist[tgt_ids])]
        goal = int(tgt_ids[np.argmin(dist[tgt_ids])])
        inv = np.argwhere(ok)
        path, c = [], goal
        while c != -9999 and c >= 0:
            y, x = inv[c]
            path.append((float(lats[y]), float(lons[x])))
            if c == start:
                break
            c = pred[c]
        path[-1] = berth                                  # start of path = board side
        # ---- 4. simplify, keeping MIN_CLEAR_M off land
        track = simplify(path, land, kx)
        out[code] = dict(name=label, vessel_class=big.code,
                         quay=[round(q.y, 6), round(q.x, 6)], quay_hdg=round(quay_hdg, 1),
                         berth=[round(berth[0], 6), round(berth[1], 6)],
                         pilot=[[round(a, 6), round(b, 6)] for a, b in track],
                         pilot_nm=round(sum(math.hypot((b[0] - a[0]) * KY, (b[1] - a[1]) * kx)
                                            for a, b in zip(track[:-1], track[1:])) / 1852, 2))
        print(code, label, "berth", out[code]["berth"], "quay", out[code]["quay_hdg"],
              "pilot", len(track), "pts", out[code]["pilot_nm"], "NM for", big.code)
    with open(OUT, "w") as fh:
        json.dump(out, fh, indent=1)
    print("->", OUT)


def quay_berth(land_boundary, land, lat, lon):
    """Snap a berth point to the quay line; return (quay, heading, berth 40 m off)."""
    from shapely.geometry import Point
    kx = 111320.0 * math.cos(math.radians(lat))
    p = Point(lon, lat)
    best = min((ln for ln in land_boundary), key=lambda e: e.distance(p))
    s = best.project(p)
    q = best.interpolate(s)
    a, b = best.interpolate(max(0.0, s - 0.0003)), best.interpolate(min(best.length, s + 0.0003))
    dx, dy = (b.x - a.x) * kx, (b.y - a.y) * KY
    n = math.hypot(dx, dy) or 1.0
    dx, dy = dx / n, dy / n
    nx = ny = None
    for sgn in (1, -1):
        cx, cy = sgn * dy, -sgn * dx
        if not land.contains(Point(q.x + cx * 60 / kx, q.y + cy * 60 / KY)):
            nx, ny = cx, cy
            break
    if nx is None:                     # both sides land (a narrow slip): stay put
        nx, ny = 0.0, 0.0
    berth = (q.y + ny * BERTH_OFF_M / KY, q.x + nx * BERTH_OFF_M / kx)
    hdg = (math.degrees(math.atan2(dx, dy)) + 360) % 360
    return (q.y, q.x), hdg, berth


def in_pocket(quay, berth, pocket):
    """Middle of the berth's dredged area on the line from the quay out
    through the 40 m berth point (None if that line misses the area)."""
    from shapely.geometry import LineString
    qla, qlo = quay
    dla, dlo = berth[0] - qla, berth[1] - qlo
    n = math.hypot(dla, dlo) or 1e-9
    line = LineString([(qlo, qla), (qlo + dlo / n * 0.003, qla + dla / n * 0.003)])  # ~330 m out
    x = line.intersection(pocket)
    parts = [g for g in getattr(x, "geoms", [x]) if g.length > 0]
    if not parts:
        return None
    seg = min(parts, key=lambda g: g.distance(line.interpolate(0)))
    c = seg.interpolate(0.5, normalized=True)
    return (c.y, c.x)


def main_mpa():
    """MPA berths (see the module notes) -> data/berths_mpa.json."""
    from shapely import STRtree, contains_xy
    from shapely.geometry import Point, box, shape
    from shapely.prepared import prep
    from saferoute import mpa, seagrid as SG, study_area as SA
    land_all = shape(json.load(open(mpa.land_geojson(SG.LAND_GEOJSON)))["features"][0]["geometry"])
    dfe = mpa.features("depthsa")
    dpolys = [shape(f["geometry"]) for f in dfe]
    ddep = np.array([mpa.area_depth(f["properties"]) for f in dfe])
    dtree = STRtree(dpolys)

    def depth_at(la, lo, r_m=0.0):
        g = Point(lo, la) if not r_m else Point(lo, la).buffer(r_m / KY)
        idx = [i for i in dtree.query(g) if dpolys[i].intersects(g)]
        return float(ddep[idx].min()) if idx else None

    out = {}
    for code, ((rla, rlo), _label, (sla, slo)) in REFS.items():
        sla, slo = MPA_STARTS.get(code, (sla, slo))
        pts = mpa.berth_points(code)
        if not pts:
            print(code, "no MPA berths - skipped")
            continue
        lat_all = [p[1] for p in pts] + [sla]
        lon_all = [p[2] for p in pts] + [slo]
        la0, la1 = min(lat_all) - 0.012, max(lat_all) + 0.012
        lo0, lo1 = min(lon_all) - 0.012, max(lon_all) + 0.012
        land = land_all.intersection(box(lo0 - .01, la0 - .01, lo1 + .01, la1 + .01))
        boundary = [ln for g in getattr(land, "geoms", [land])
                    for ln in [g.exterior, *g.interiors]]
        kx = 111320.0 * math.cos(math.radians((la0 + la1) / 2))
        lats = np.arange(la0, la1 + 1e-12, FINE_DEG)
        lons = np.arange(lo0, lo1 + 1e-12, FINE_DEG)
        LA, LO = np.meshgrid(lats, lons, indexing="ij")
        water = ~contains_xy(land, LO, LA)
        clear = ndimage.distance_transform_edt(water) * FINE_DEG * KY
        ny_, nx_ = water.shape

        # each MPA berth usually has its own dredged area (DRGARE named after it)
        pockets = {f["properties"].get("OBJNAM"): (shape(f["geometry"]), mpa.area_depth(f["properties"]))
                   for f in dfe if f["properties"].get("FCSubtype") == mpa.DRGARE
                   and f["properties"].get("OBJNAM")}
        berths = []
        for short, bla, blo in pts:
            full = mpa.berth_full_name(code, short)
            quay, hdg, berth = quay_berth(boundary, land, bla, blo)
            pocket = pockets.get(full)
            if pocket is not None:
                berth = in_pocket(quay, berth, pocket[0]) or berth
            berths.append(dict(id=short, quay=quay, hdg=hdg, berth=berth, mpa=(bla, blo),
                               pocket_m=pocket[1] if pocket else None))
        near = np.zeros_like(water)
        for b in berths:
            near |= np.hypot((LA - b["berth"][0]) * KY, (LO - b["berth"][1]) * kx) < 80
        # charted depth on the fine raster (uncharted water counts as 0 m)
        _, fdep = mpa.depth_raster(lats, lons, supersample=1)
        base_ok = water & ((clear >= MIN_CLEAR_M) | near)
        cm = 1.0 + 150.0 / (clear + 10.0)

        def search(T):
            """Shortest mid-channel path tree from the start over water >= T m."""
            ok = base_ok & ((fdep >= T) | near)
            idx = -np.ones(water.shape, int)
            idx[ok] = np.arange(ok.sum())
            rows, cols, w = [], [], []
            for ddy, ddx in ((0, 1), (1, 0), (1, 1), (1, -1)):
                sa = (slice(0, ny_ - ddy), slice(max(0, -ddx), nx_ - max(0, ddx)))
                sb = (slice(ddy, ny_), slice(max(0, ddx), nx_ - max(0, -ddx)))
                A, B = idx[sa], idx[sb]
                m = (A >= 0) & (B >= 0)
                step = math.hypot(ddy * KY, ddx * kx) * FINE_DEG
                rows += list(A[m]); cols += list(B[m]); w += list(step * .5 * (cm[sa][m] + cm[sb][m]))
            G = coo_matrix((w, (rows, cols)), shape=(ok.sum(),) * 2).tocsr()
            inv = np.argwhere(ok)

            def nearest(la, lo):
                d = np.hypot((inv[:, 0] * FINE_DEG + la0 - la) * KY, (inv[:, 1] * FINE_DEG + lo0 - lo) * kx)
                k = int(np.argmin(d))
                return k, float(d[k])
            st, off = nearest(sla, slo)
            if off > 300:
                return None
            dist, pred = dijkstra(G, directed=False, indices=st, return_predecessors=True)
            return dict(T=T, inv=inv, nearest=nearest, start=st, dist=dist, pred=pred)

        levels = sorted({0.0, 5.0, 8.0, 10.0, 11.0, 12.0, 13.0, 14.0, 15.0, 16.5, 18.0, 20.0, 23.0}, reverse=True)
        trees = [t for t in (search(T) for T in levels) if t is not None]
        buf = prep(land.buffer(MIN_CLEAR_M * .5 / KY))

        def shallow(seg, T):
            n = max(2, int(seg.length * KY / 10) + 1)
            for t in np.linspace(0, 1, n)[1:-1]:
                p_ = seg.interpolate(t, normalized=True)
                iy, ix = int(round((p_.y - la0) / FINE_DEG)), int(round((p_.x - lo0) / FINE_DEG))
                if 0 <= iy < ny_ and 0 <= ix < nx_ and fdep[iy, ix] < T and not near[iy, ix]:
                    return True
            return False

        def seg_min_depth(seg):
            n = max(2, int(seg.length * KY / 10) + 1)
            m = np.inf
            for t in np.linspace(0, 1, n):
                p_ = seg.interpolate(t, normalized=True)
                iy, ix = int(round((p_.y - la0) / FINE_DEG)), int(round((p_.x - lo0) / FINE_DEG))
                if 0 <= iy < ny_ and 0 <= ix < nx_ and not near[iy, ix]:
                    m = min(m, fdep[iy, ix])
            return m

        options = []
        for b in berths:
            cap = b["pocket_m"] if b["pocket_m"] is not None else 99.0
            tree = None
            for t in trees:                      # deepest water that still reaches the berth
                if t["T"] > cap:
                    continue
                k, off = t["nearest"](*b["berth"])
                if off < 60 and np.isfinite(t["dist"][k]):
                    tree = t
                    break
            if tree is None:
                print(f"  {code} {b['id']}: no water path - skipped")
                continue
            path, c = [], k
            while c >= 0:
                y, x = tree["inv"][c]
                path.append((float(lats[y]), float(lons[x])))
                if c == tree["start"]:
                    break
                c = tree["pred"][c]
            path = path[::-1]                      # start -> berth
            path[0] = (sla, slo)
            path[-1] = b["berth"]
            T = tree["T"]
            track = simplify(path[::-1], buf, kx, prepared=True,
                             extra=lambda seg, T=T: shallow(seg, T))[::-1]
            def track_depths(tk):
                samples = []
                for (a1, b1), (a2, b2) in zip(tk[:-1], tk[1:]):
                    n = max(2, int(math.hypot((a2 - a1) * KY, (b2 - b1) * kx) / 25) + 1)
                    samples += [(a1 + (a2 - a1) * t, b1 + (b2 - b1) * t) for t in np.linspace(0, 1, n)]
                return [d for d in (depth_at(a, b2) for a, b2 in samples) if d is not None]
            track = drop_short_legs(track, buf, kx, seg_min_depth, floor_m=T,
                                    reported=lambda tk: min(track_depths(tk), default=np.inf))
            dd = track_depths(track)
            bd = b["pocket_m"] if b["pocket_m"] is not None else depth_at(*b["berth"])
            track_min = min(dd) if dd else None
            known = [x for x in (bd, track_min) if x is not None]
            control = min(known) if known else None
            fits = [v.code for v in SA.VESSEL_CLASSES.values()
                    if control is not None and v.draught_m + max(1.0, 0.1 * v.draught_m) <= control]
            big = max((SA.VESSEL_CLASSES[c_] for c_ in fits), key=lambda v: v.loa_m) if fits else None
            nm = sum(math.hypot((q2[0] - q1[0]) * KY, (q2[1] - q1[1]) * kx)
                     for q1, q2 in zip(track[:-1], track[1:])) / 1852
            options.append(dict(
                id=b["id"], name=f"{SA.PORTS[code].name} - berth {b['id']}",
                mpa_point=[round(b["mpa"][0], 6), round(b["mpa"][1], 6)],
                quay=[round(b["quay"][0], 6), round(b["quay"][1], 6)], quay_hdg=round(b["hdg"], 1),
                berth=[round(b["berth"][0], 6), round(b["berth"][1], 6)],
                pilot=[[round(a, 6), round(c2, 6)] for a, c2 in track], pilot_nm=round(nm, 2),
                berth_depth_m=bd, track_min_depth_m=track_min, controlling_depth_m=control,
                vessel_class=big.code if big else None, fits=fits))
        if not options:
            continue
        # default: the berth with the deepest controlling depth (berth and
        # track), then the one nearest the old reference position
        default = min(options, key=lambda o: (-(o["controlling_depth_m"] or 0.0),
                                              math.hypot((o["mpa_point"][0] - rla) * KY,
                                                         (o["mpa_point"][1] - rlo) * kx)))
        out[code] = dict(default, berths=options, source=f"MPA GeoSpace-Sea {mpa.source_date()}")
        print(f"{code}: {len(options)} berths, default {default['id']} at {default['berth']} "
              f"depth {default['berth_depth_m']} m (track min {default['track_min_depth_m']}), "
              f"pilot {default['pilot_nm']} NM, largest {default['vessel_class']}")
        for o in options:
            if o["vessel_class"] is None:
                print(f"   ! {o['id']}: controlling depth {o['berth_depth_m']}/{o['track_min_depth_m']} m - no class fits")
    with open(OUT_MPA, "w") as fh:
        json.dump(out, fh, indent=1)
    print("->", OUT_MPA)


def drop_short_legs(track, buf, kx, min_depth, min_leg_m=150.0, floor_m=0.0, reported=None):
    """Remove waypoints that leave a leg shorter than `min_leg_m` (a ship
    cannot steer a 30 m zigzag) when the leg that replaces them stays clear of
    the land buffer and is no shallower than the two legs it replaces
    (`min_depth(segment)`) or than `floor_m`, the depth the track was
    searched at - or at least no shallower than the whole track already is,
    so the track's least depth never gets worse. Ends are kept."""
    from shapely.geometry import LineString
    pts = list(track)
    seg = lambda a, c: LineString([(a[1], a[0]), (c[1], c[0])])
    changed = True
    while changed and len(pts) > 2:
        changed = False
        for i in range(1, len(pts) - 1):
            a, b, c = pts[i - 1], pts[i], pts[i + 1]
            la = math.hypot((b[0] - a[0]) * KY, (b[1] - a[1]) * kx)
            lb = math.hypot((c[0] - b[0]) * KY, (c[1] - b[1]) * kx)
            if min(la, lb) >= min_leg_m:
                continue
            cut = seg(a, c)
            if i + 1 == len(pts) - 1:      # the last metres run up to the quay
                cut_l = seg(a, (c[0] + (a[0] - c[0]) * .15, c[1] + (a[1] - c[1]) * .15))
            else:
                cut_l = cut
            if buf.intersects(cut_l):
                continue
            whole = min(min_depth(seg(p, q)) for p, q in zip(pts[:-1], pts[1:]))
            if reported is not None:
                whole = min(whole, reported(pts))
            if min_depth(cut) + 1e-9 < min(min(min_depth(seg(a, b)), min_depth(seg(b, c)), floor_m), whole) \
                    and min_depth(cut) + 1e-9 < whole:
                continue
            del pts[i]
            changed = True
            break
    return pts


def simplify(path, land, kx, prepared=False, extra=None):
    """Fewest waypoints whose legs stay >= MIN_CLEAR_M/2 off land (greedy).
    `path` runs berth -> start; `land` is the land (or, with `prepared`, the
    already buffered and prepared land)."""
    buf = land if prepared else land.buffer(MIN_CLEAR_M * .5 / KY)
    out, i = [path[0]], 0
    while i < len(path) - 1:
        j = len(path) - 1
        while j > i + 1:
            seg = LineString([(path[i][1], path[i][0]), (path[j][1], path[j][0])])
            # the last metres to the berth run up to the quay itself
            if j == len(path) - 1:
                seg = LineString([(path[i][1], path[i][0]),
                                  (path[j][1] + (path[i][1] - path[j][1]) * .15,
                                   path[j][0] + (path[i][0] - path[j][0]) * .15)])
            if not buf.intersects(seg) and not (extra and extra(seg)):
                break
            j -= 1
        out.append(path[j])
        i = j
    return out


if __name__ == "__main__":
    main()
