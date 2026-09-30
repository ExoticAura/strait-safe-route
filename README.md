# Strait Safe Route

The safest — or the shortest — route for a container ship between the east or
west entrance of the Singapore Strait and a Singapore terminal (or the other way
round), threaded between every **stationary ship** around the port, clear of
**land**, and clear of water that is **too shallow** for the ship's draught.

The strait is turned into a board: a 211 × 526 grid of ~220 m cells, each one
free water, land, too shallow, too close to shore, inside a ship's exclusion
zone, or inside a ship's target passing distance. Dijkstra and A* then solve
the board for the route that meets the fewest ships, or the shortest one that
still obeys every hard limit.

No costs of any kind are modelled: no port dues, no fuel, no charges.

```bash
pip install -r requirements.txt
python -m saferoute.serve            # opens the chart at http://127.0.0.1:8765
```

---

## Scope → where it lives

| Scope item | What was built | Where |
|---|---|---|
| **1. Navigational network construction** | Static board: OpenStreetMap coastline, depth (GEBCO file or declared proxy), shore clearance, schematic TSS lanes, MPA anchorages; stationary ships rasterised on top per snapshot; 16-neighbour **directed** graph | `seagrid.py`, `obstacles.py`, `network.py` |
| **2. Static pathfinding** | Dijkstra, A* (admissible great-circle heuristic), weighted A*, spatially distinct alternatives, Theta*-style smoothing; three objectives: shortest / balanced / safest | `algorithms.py`, `network.py`, `routing.py` |
| **3. Baseline route validation** | Paired comparison with historical voyages on distance and reliability (ships met, closest approach, hard breaches, shallow water, land), t-test + Wilcoxon | `ais.py`, `evaluation.py`, `scripts/run_pipeline.py` |

## Headline results (Panamax, synthetic snapshot of 217 ships; `outputs/`)

* **Safest vs shortest**, averaged over all 16 entrance↔terminal routes: ships
  met **2.12 vs 5.88** (−64 %), NM sailed inside a target CPA **1.47 vs 4.24**,
  for **+0.07 NM** of distance (38.85 vs 38.78 NM). East entrance → Pasir
  Panjang: **4 ships met instead of 16** for +0.45 NM.
* **A\* vs Dijkstra**: identical optimal cost on every route (gap 0.0), with
  **56 % fewer nodes expanded** on the 16-neighbour graph (49 % on 8-neighbour).
* **Validation** against 320 baseline voyages (current-practice lane following
  that knows land and depth but not where ships are anchored): planned routes
  are **2.1 % shorter** and meet **88 % fewer ships** (safest mode; 15.4 → 1.8
  per voyage, p < 0.0001); **100 %** of planned routes are free of hard CPA
  breaches against **36 %** of baseline voyages.

The baseline corpus and the ship snapshot are **synthetic** until real AIS is
dropped in (below) — these are results about the method.

---

## Deploying to Railway

The repo deploys as-is:

1. Railway → **New Project → Deploy from GitHub repo** → pick this repo.
   Railway reads `railway.json`, builds the `Dockerfile` (Python 3.12 slim) and
   health-checks `/api/health`.
2. **Settings → Networking → Generate Domain** for a public URL.

What gets served:

| Path | What |
|---|---|
| `/` | the full app from `web_static/`: routing engine in the browser, 3D world, play/pause |
| `/classic/` | the original chart that plans on the server |
| `/api/*` | the Python engine (route, ships, board, CSV export, snapshot upload) |

`PORT` is injected by Railway; the server then listens on `0.0.0.0:$PORT`.
Memory use is small (well under 512 MB): the browser app does its own
routing, and the server only builds graphs for `/classic/` and `/api/`.
`SAFEROUTE_MAX_NETWORKS` (default 8) caps how many graphs stay cached.

To show a real AIS snapshot on the deployed site, commit it as
`data/ais/latest.csv` (for `/classic/` and the API), or upload it in the
browser app's *Ship snapshot* card (kept in that browser only).

Local run of the same container:

```bash
docker build -t strait-safe-route .
docker run -p 8765:8765 -e PORT=8765 strait-safe-route    # http://localhost:8765
```

## The ships: where the obstacle positions come from

The router needs the lat/lon, size and heading of every ship that is **not
under way** — anchored, moored or drifting. Three sources, chosen in the
**Ships** drop-down:

1. **Real AIS, captured live** (recommended) — on your laptop:
   ```bash
   pip install websockets
   export AISSTREAM_API_KEY=...        # free: sign in at https://aisstream.io
   python scripts/capture_ais_snapshot.py --minutes 15
   ```
   Listens to AIS over the study area, keeps every vessel reporting NavStatus 1
   (at anchor) or 5 (moored), or never faster than 0.5 kn, and writes
   `data/ais/latest.csv`. The app picks it up automatically on next start.
2. **Any AIS extract** — upload a CSV on the *Ship snapshot* card (or drop it in
   `data/ais/`). Either the snapshot format below, or a raw table with columns
   like `MMSI, ReceiveTime, Lat, Lon, SoG, NavStatus, Heading` (MarineCadastre
   names work too). The last report of each vessel is taken and ships under way
   are dropped. The sibling group's cleaned AIS fits here directly.
3. **Synthetic** (default when there is no real file) — ships sampled inside
   MPA's real anchorages (reference positions from *Singapore Port Information
   2025*), with sizes by anchorage purpose and a common heading to the tidal
   stream. Labelled synthetic everywhere; no MMSIs are invented.

Snapshot CSV columns: `id, mmsi, name, ship_type, ais_type, lat, lon, sog_kn,
heading_deg, nav_status, status, loa_m, beam_m, draught_m, timestamp_utc, area, source`.

**"Container ship" in AIS:** AIS type codes 70–79 mean *cargo* and do not
separate container ships from bulk carriers. A real snapshot is therefore
typed by AIS category; every stationary ship is a physical obstacle regardless.
Types can be ignored with the tick-boxes on the *Ship snapshot* card.

### How much water a stationary ship blocks

* **Anchored** ships swing round their anchor with the tide, so the router
  plans round the **swing circle**: radius = LOA + horizontal scope of cable
  (1.5 × √depth shackles), centred on the anchor, which lies ahead of the AIS
  position. Untick *swing circle* to plan round the hull where it is now.
* **Moored** ships: the hull (LOA/2).
* Passing distances (the project's CPA table; × factor slider in the app):

| Zone | Hard limit (never closer) | Target (penalised inside) |
|---|---|---|
| Singapore Strait TSS lanes | 0.30 NM | 1.00 NM |
| Port approaches & fairways | 0.20 NM | 0.50 NM |
| Berthing basins & narrow water | 0.05 NM | 0.22 NM |

## The three routes

All three obey the same **hard limits** — land; depth ≥ draught + UKC (3.5 m in
the TSS per the IMO rules for the Straits, 10 % of draught elsewhere); shore
clearance; each ship's hard CPA; TSS direction (no wrong-way lane or inshore
band transit). They differ in how hard they avoid a ship's **target** CPA:

| Mode | Per NM inside a target CPA | Per ship met |
|---|---|---|
| Shortest | 0 | 0 |
| Balanced | 1.5 × (depth of incursion)² | 1 NM |
| Safest | 3 × (depth of incursion)² | 8 NM |

"Ships met" = distinct stationary ships whose envelope the route passes inside
the target CPA of the zone. Metrics are measured on the smoothed track, not the
grid.

## The app

* **Chart** — ships drawn at true size and heading when zoomed in, swing
  circles, ships met highlighted with their closest-approach distance,
  **Router's board** layer (the puzzle the search solves), depth, TSS, MPA
  anchorages and pilot boarding grounds. Basemaps Simple / Satellite / Nautical
  / Offline (vendored Leaflet + the model's own coastline — works with no
  internet). **Pick on chart…** in From/To starts or ends anywhere.
* **Shortest vs safest** table, list of ships met (click to jump there),
  waypoints download (`/api/route.csv`).
* **Ship view (3D)** — your ship at any point along the route (slider, or jump
  from close pass to close pass) with the surrounding stationary ships at their
  true relative positions, sizes and headings, range rings at 0.1 / 0.25 / 0.5
  / 1 NM and hull-to-hull distance labels. *Surroundings* frames the nearest
  ships; *Bridge* is the view ahead from the bridge wing. Light: day / dawn /
  dusk / night.

API: see the docstring of `src/saferoute/serve.py`.

## Real bathymetry

The default depth is a **declared proxy** (depth rises with distance from the
OSM coastline, plus dredged pockets at the terminals). Against the 36 charted
depths MPA publishes for its anchorages it is off by 6.1 m on average
(`outputs/s1_proxy_vs_charted_depth.csv`) — good enough to keep ships off the
shore, not good enough to find shoals. To use real depths:

1. Go to <https://download.gebco.net>, draw the box **N 1.45, S 1.03, W 103.40,
   E 104.45**, choose **2D netCDF**, **Esri ASCII** or **Data GeoTIFF**.
2. Put the file in `data/bathymetry/`. It is used automatically (the status bar
   says which file). Esri ASCII needs nothing extra; GeoTIFF needs `tifffile`,
   netCDF needs `netCDF4`.

## Commands

| Command | What it does |
|---|---|
| `python -m saferoute.serve` | chart server on 127.0.0.1:8765 |
| `python scripts/capture_ais_snapshot.py` | live AIS snapshot → `data/ais/latest.csv` |
| `python scripts/run_pipeline.py --out outputs [--snapshot ais:latest.csv] [--voyages real.csv]` | all three scope items, every table |
| `python scripts/make_figures.py` | F1–F4 for the report |
| `python -m pytest tests -q` | 31 tests |
| `python scripts/export_web_data.py` | rebuild `web_static/data.json` for the in-browser version |

`python scripts/export_3d_world.py --osm <singapore-driving>/public/data` rebuilds
the 3D buildings (124,883 OSM footprints, ~70 % with estimated heights) and the
representative quay cranes / container yards for `web_static/world/`.
`python scripts/export_land_texture.py --osm <same>` then paints the land-use
texture draped over the land (urban ground, port yards, parks, reservoirs, roads,
building footprints) as `web_static/world/landuse.jpg`.

`web_static/` is the same app with the engine ported to JavaScript
(`engine.js`), so it runs with no server at all; its routes and costs match the
Python engine to within the rounding of the exported layers.

See **Deploying to Railway** below.

## Read this before quoting a number

* Ship snapshot and baseline voyages are **synthetic** unless you load real AIS.
  A real AIS voyage was sailed among the ships present at *its* time, not the
  snapshot's; validate against voyages from the same period as the snapshot.
* The coastline is an **OpenStreetMap land extract from 2017** (ODbL), topped
  up with 12.9 km² of newer reclaimed land wherever 2026 OSM building
  footprints stand outside it (Tuas, Pulau Bukom and others). Reclaimed areas
  with no buildings yet are still missing.
* Depth is a **proxy** until GEBCO is supplied; terminal maintained depths
  (Tuas 23 m, Jurong 15.7 m, Pasir Panjang 16 m, Keppel 15 m) are planning
  assumptions in `study_area.py`.
* The TSS is **schematic** (~0.5 NM). Anchorage extents are approximated as
  discs round MPA's reference positions. Not for navigation.
* Tidal streams, moving traffic, pilotage and the last few hundred metres from
  the approach position to the berth are out of scope.

## Layout

```
src/saferoute/
  study_area.py   bbox, ports, MPA anchorages & pilot grounds, TSS, CPA table, own-ship classes
  seagrid.py      the static board: OSM land, depth (proxy or GEBCO), TSS, CPA zones
  obstacles.py    ship snapshots: AIS loaders, aisstream parsing, synthetic, obstacle field
  network.py      board + ships -> directed graph; smoothing
  algorithms.py   Dijkstra, A*, weighted A*, alternatives
  routing.py      plan a route, measure its safety
  ais.py          historical voyages (synthetic baseline / real loader)
  evaluation.py   paired baseline validation
  cache.py serve.py viz.py
  data/sg_land.geojson   OSM land polygons for the study area
  web/            index.html app.js map.js ship3d.js style.css vendor/
scripts/          capture_ais_snapshot.py run_pipeline.py make_figures.py
tests/            31 tests
data/ais/         real snapshots go here      data/bathymetry/   GEBCO goes here
```
