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
| **2. Static pathfinding** | Dijkstra, A* (admissible great-circle heuristic), weighted A*, spatially distinct alternatives, Theta*-style smoothing, turn radii per waypoint (sailed track, re-search if a turn is too tight); three objectives: shortest / balanced / safest | `algorithms.py`, `network.py`, `sailing.py`, `routing.py` |
| **3. Baseline route validation** | Paired comparison with historical voyages on distance and reliability (ships met, closest approach, hard breaches, shallow water, land), t-test + Wilcoxon | `ais.py`, `evaluation.py`, `scripts/run_pipeline.py` |

## Headline results (Panamax, synthetic snapshot of 181 ships, GEBCO 2026 depths; `outputs/`)

All figures are measured on the **sailed track** (the legs joined by the
turns the ship can actually make, see *How the ship turns*), not on the
search polyline.

* **Safest vs shortest**, averaged over all 16 entrance↔berth routes: ships
  met **2.12 vs 6.19** (−66 %), NM sailed inside a target CPA ****1.11 vs 4.12****,
  for +0.07 NM (38.82 vs 38.75 NM). East entrance → Pasir Panjang berth:
  **5 ships met instead of 14** for +0.65 NM.
* **A\* vs Dijkstra**: identical optimal cost on every route (gap 0.0); A\*
  expands **48 % fewer nodes** than Dijkstra on the 16-neighbour graph.
* **Validation** against 320 baseline voyages (current-practice lane following
  that knows land and depth but not where ships are anchored, to the same
  berths): planned routes are **5.4 % shorter** and meet **75 % fewer ships**
  (safest mode; 7.6 → 1.9 per voyage, p < 0.0001); **100 %** of planned
  routes are free of hard CPA breaches against **43 %** of baseline voyages.
  (The baseline now also crosses water GEBCO shows too shallow for the ship.)
* **Re-planning as traffic moves** (`scripts/replan_benchmark.py`, 369
  re-plans, 181 anchored + 40 moving simulated ships, 2-core 2.1 GHz Xeon):
  a full re-plan (traffic, obstacle field, graph, search, sail, measure)
  takes **0.49 s median, 1.3 s 95th percentile, 1.9 s worst** - under a
  fifth of a 10 s cycle. **99 %** found a feasible route; the 3 that did not
  (East → Pasir Panjang, about 3650 s into the run) are a known gap.
* **Turning**: every route, for every ship class, is sailable: no turn tighter
  than 1.5 ship lengths at sea (1 L in pilotage waters). The sailed track is
  at most 200 m off the planned legs.
* **Every terminal route ends on a berth** (see *Berths*), not in open water.

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

## Coastline

`src/saferoute/data/sg_land.geojson` is built in three layers:

1. OpenStreetMap land polygons, 2017 extract (ODbL);
2. plus reclaimed land wherever 2026 OSM building footprints stand outside it;
3. plus **land reclaimed 2016-2025 detected from Sentinel-2** (persistent
   water → land, 10 m pixels) - 20.8 km² not in layers 1-2, of which 8.4 km²
   at **Tuas Port** (its new finger piers and yards) and 11.2 km² at Changi /
   the east coast. `python scripts/update_coastline.py reclamation.gpkg`;
   polygons from github.com/papersheep025/sg-reclamation-habitat (contains
   modified Copernicus Sentinel data).

Each terminal's **fairway** - from its approach to the nearest point of the
TSS - is treated as dredged (0.3 NM either side) and kept free of the
synthetic anchored ships, as port fairways are.

## Berths

Every route to or from a terminal starts or ends **on a berth**, 40 m off the
quay line (the ship alongside), reached by a pilotage track:

| Terminal | Berth | Pilotage track |
|---|---|---|
| Tuas Port | middle finger pier, south berth | 1.4 NM |
| Jurong Port | main wharf | 2.7 NM |
| Pasir Panjang | inner basin, east quay | 1.8 NM |
| Keppel / Brani | Brani north-east wharf | 1.5 NM |

`scripts/build_berths.py` snaps a reference position for each terminal onto
the modelled (OpenStreetMap) quay line, and draws the pilotage track as a
shortest mid-channel path on a 22 m raster of the same coastline from the
berth to open water the ~220 m route board can reach. The track and berth are
dredged to the terminal's depth; the board's land / shore / depth checks
apply to the passage outside the pilotage track (alongside, a ship is metres
from the quay by design), ship clearances apply everywhere. Pilotage waters
(tug-assisted turns down to 1 ship length) run from the MPA pilot boarding
ground the route passes to the berth. At the berth the tugs swing the ship
parallel to the quay. Berth positions are representative (one per terminal,
on the modelled coastline), not surveyed berth numbers; edit `REFS` in the
script to move or add them.

## How the ship turns

The search returns straight legs with sharp corners; a ship cannot turn on the
spot. `sailing.py` turns the legs into the track the ship would steer, the way
a passage plan is drawn on an ECDIS - straight legs joined by circular arcs,
one turn radius per waypoint - and **every metric is taken on that track**:

1. **Straighten** - small lattice jogs (< 150 m, where the straight line is
   legal) are steered straight through.
2. **Turn radius per waypoint** - 2.5 × LOA where the water allows (735 m
   for a Panamax, ~29°/min at 12 kn); smaller where the legs are too short or
   the arc would cut into land, shallow water, the shore band, a ship's hard
   CPA or the wrong way down a TSS lane.
3. **Limits** - never below 1.5 × LOA at sea (about hard-over), or 1.0 × LOA
   in the 3 NM next to a terminal (pilotage with tugs). A corner that needs
   less closes the water round it and the route is **searched again** until
   it can be sailed.

The ship's head in the 3D view adds the drift angle (bow inside the turn,
up to 5°) and rudder lag, so the ship eases into and out of each turn.
`/api/route.csv` and the app's waypoint download give the passage plan:
waypoints plus the turn radius used at each.

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
the target CPA of the zone. Metrics are measured on the sailed track (with its turns), not the
grid.

## The app

* **Chart** — base map: Chart (offline), Satellite, Satellite + labels,
  Streets, Terrain, Nautical or Light grey (tile maps need the internet; the
  published preview page blocks them, the Railway site loads them). Ships
  drawn at true size and heading when zoomed in, swing circles centred on the
  anchor with the chain to the bow, ships met highlighted with their closest-approach distance,
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
* **Sea** - a wave-displaced ocean (`web_static/ocean.js`): 16 Gerstner wave
  trains from a JONSWAP spectrum for the Strait's fetch-limited wind chop
  (calm / moderate / fresh: Hs 0.3 / 0.7 / 1.0 m, Tp 3.1 / 4.1 / 4.6 s),
  physically based shading (Fresnel for water's index of refraction, soft sky
  reflection, sun glint, turbid green-grey water, whitecaps where the surface
  folds). Every ship heaves, pitches and rolls on the same waves through a
  damped spring with periods that scale with its length, so a Panamax barely
  moves in chop while a tug bobs. Purely visual: routing does not use it.

API: see the docstring of `src/saferoute/serve.py`.

## Real bathymetry

GEBCO 2026 (Esri ASCII, N 1.45 / S 1.03 / W 103.40 / E 104.45) is in
`data/bathymetry/` and loaded automatically (the status bar names the file).
Without a file the board falls back to a **declared proxy** (depth rises with
distance from the coastline). Both were checked against the 36 charted depths
MPA publishes for its anchorages (`outputs/s1_proxy_vs_charted_depth.csv`):

| depth source | mean abs. error | bias | within 3 m |
|---|---|---|---|
| shore-distance proxy | 5.9 m | +2.0 m | 12 / 36 |
| GEBCO 2026 (+ dredged pockets) | 7.2 m | +2.2 m | 9 / 36 |

GEBCO is not better than the proxy at these points: its ~460 m cells are
smoothed and partly predicted in the shallow strait. What it adds is real
shoals and banks the proxy cannot know. Four consequences, all in the code:

1. **Dredged pockets stay on top.** GEBCO cannot see terminal basins or
   fairways (0-11 m at the Jurong and Pasir Panjang approaches), so each
   terminal's maintained depth is applied over it (`apply_dredged_pockets`).
2. **Fairways follow the water.** `seagrid.fairway_path()` runs the dredged,
   ship-free corridor from each approach to the TSS through water, not
   through Pulau Bukom as the straight line did.
3. **The west gateway moved** from 1.092 N 103.416 E to 1.110 N 103.440 E
   (2.7 NM): GEBCO shows 6-12 m on its own south-west edge.
4. **Pasir Panjang's pilotage start moved** to 1.238 N 103.779 E, south-east
   of the berth, so the ship arrives without a hairpin.

## Commands

| Command | What it does |
|---|---|
| `python -m saferoute.serve` | chart server on 127.0.0.1:8765 |
| `python scripts/capture_ais_snapshot.py` | live AIS snapshot → `data/ais/latest.csv` |
| `python scripts/run_pipeline.py --out outputs [--snapshot ais:latest.csv] [--voyages real.csv]` | all three scope items, every table |
| `python scripts/make_figures.py` | F1–F4 for the report |
| `python scripts/replan_benchmark.py [--ais day.csv]` | re-planning every 10 / 30 s as traffic moves → `outputs/s4_replan_*.csv` |
| `python scripts/update_coastline.py reclamation.gpkg` | add reclaimed land to the coastline |
| `python -m pytest tests -q` | 59 tests |
| `python scripts/export_web_data.py` | rebuild `web_static/data.json` for the in-browser version |

`python scripts/export_3d_world.py --osm <singapore-driving>/public/data` rebuilds
the 3D buildings (124,883 OSM footprints, ~70 % with estimated heights) and the
representative quay cranes / container yards for `web_static/world/`.
`python scripts/export_land_texture.py --osm <same>` then paints the land-use
texture draped over the land (urban ground, port yards, parks, reservoirs, roads,
building footprints) as `web_static/world/landuse.jpg`.
`python scripts/export_3d_detail.py --osm <same>` (run it between the two) adds
land height from the Copernicus DEM tile in `data/dem/` (buildings filtered out
by a 450 m morphological opening, so hills stay and towers do not: approximate,
Bukit Timah reads about 90 m, not 164 m), a sea-depth map from the route board
(shallow water is tinted), a white-water ribbon along the coast, and container
yard blocks packed into the free land behind each quay (about 100,000 stacks over
the four terminals; representative, not surveyed).

`web_static/` is the same app with the engine ported to JavaScript
(`engine.js`), so it runs with no server at all; its routes and costs match the
Python engine to within the rounding of the exported layers.

See **Deploying to Railway** below.

## Read this before quoting a number

* Ship snapshot and baseline voyages are **synthetic** unless you load real AIS.
  A real AIS voyage was sailed among the ships present at *its* time, not the
  snapshot's; validate against voyages from the same period as the snapshot.
* The coastline is an **OpenStreetMap land extract from 2017** (ODbL), topped
  up with newer reclaimed land from 2026 OSM buildings and from Sentinel-2
  (2016-2025, see *Coastline*). Reclamation after 2025 is missing.
* Depth is GEBCO 2026 at ~460 m (see *Real bathymetry*) with terminal pockets forced on top; terminal maintained depths
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
  sailing.py      turn radii per waypoint: the track the ship actually sails
  routing.py      plan a route, sail it, measure its safety
  ais.py          historical voyages (synthetic baseline / real loader)
  evaluation.py   paired baseline validation
  cache.py serve.py viz.py
  data/sg_land.geojson   OSM land polygons for the study area
  web/            index.html app.js map.js ship3d.js style.css vendor/
scripts/          capture_ais_snapshot.py run_pipeline.py make_figures.py
tests/            59 tests
data/ais/         real snapshots go here      data/bathymetry/   GEBCO goes here
```
