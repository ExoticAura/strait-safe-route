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

# Per terminal: a reference position next to its quay (lat, lon), a label,
# and the pilotage start - open water outside the terminal that the route
# network reaches (the terminal's previous approach position).
REFS = {
    "SGTUA": ((1.2440, 103.6350), "Tuas Port - middle finger pier, south berth", (1.2400, 103.6600)),
    "SGJUR": ((1.2980, 103.7190), "Jurong Port - main wharf", (1.2630, 103.7400)),
    "SGPPT": ((1.2640, 103.7800), "Pasir Panjang - inner basin, east quay", (1.2380, 103.7790)),
    "SGKEP": ((1.2615, 103.8405), "Brani Terminal - north-east wharf", (1.2460, 103.8620)),
}
BERTH_OFF_M = 40.0
FINE_DEG = 0.0002
WINDOW_DEG = 0.05
MIN_CLEAR_M = 25.0
KY = 110574.0


def main():
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


def simplify(path, land, kx):
    """Fewest waypoints whose legs stay >= MIN_CLEAR_M/2 off land (greedy)."""
    buf = land.buffer(MIN_CLEAR_M * .5 / KY)
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
            if not seg.intersects(buf):
                break
            j -= 1
        out.append(path[j])
        i = j
    return out


if __name__ == "__main__":
    main()
