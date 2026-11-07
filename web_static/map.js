/* ChartMap - the Singapore Strait chart, drawn with Leaflet.
 *
 * Layers, bottom to top: basemap tiles (or the model's own coastline when
 * offline), depth shading, the router's board, TSS, anchorages, stationary
 * ships (true-scale hulls when zoomed in, swing circles, CPA rings), routes,
 * close-pass markers, ports, and the own-ship marker with distance lines to
 * the nearest ships.
 *
 * Coordinates are [lat, lon] (Leaflet order).
 */
(function (root) {
  "use strict";
  const L = root.L;
  const css = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
  const ESRI = "https://server.arcgisonline.com/ArcGIS/rest/services";
  const ESRI_CANVAS = "Tiles &copy; Esri &mdash; Esri, HERE, Garmin, &copy; OpenStreetMap contributors";
  const ESRI_OCEAN = "Tiles &copy; Esri &mdash; Sources: GEBCO, NOAA, CHS, OSU, UNH, CSUMB, National Geographic, DeLorme, NAVTEQ, and Esri";
  const SEAMARK = "https://tiles.openseamap.org/seamark/{z}/{x}/{y}.png";
  const BASEMAPS = {
    simple: dark => ({
      url: `${ESRI}/Canvas/${dark ? "World_Dark_Gray_Base" : "World_Light_Gray_Base"}/MapServer/tile/{z}/{y}/{x}`,
      labels: `${ESRI}/Canvas/${dark ? "World_Dark_Gray_Reference" : "World_Light_Gray_Reference"}/MapServer/tile/{z}/{y}/{x}`,
      attribution: ESRI_CANVAS, maxNativeZoom: 16 }),
    satellite: () => ({
      url: `${ESRI}/World_Imagery/MapServer/tile/{z}/{y}/{x}`,
      attribution: "Imagery &copy; Esri, Maxar, Earthstar Geographics", maxNativeZoom: 18 }),
    nautical: dark => dark ? {
      url: `${ESRI}/Canvas/World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}`,
      attribution: `${ESRI_CANVAS} &middot; seamarks &copy; <a href="https://www.openseamap.org">OpenSeaMap</a>`,
      maxNativeZoom: 16, overlay: SEAMARK } : {
      url: `${ESRI}/Ocean/World_Ocean_Base/MapServer/tile/{z}/{y}/{x}`,
      labels: `${ESRI}/Ocean/World_Ocean_Reference/MapServer/tile/{z}/{y}/{x}`,
      attribution: `${ESRI_OCEAN} &middot; seamarks &copy; <a href="https://www.openseamap.org">OpenSeaMap</a>`,
      maxNativeZoom: 13, overlay: SEAMARK },
    hybrid: () => ({
      url: `${ESRI}/World_Imagery/MapServer/tile/{z}/{y}/{x}`,
      labels: `${ESRI}/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}`,
      attribution: "Imagery &copy; Esri, Maxar, Earthstar Geographics", maxNativeZoom: 18 }),
    streets: () => ({
      url: "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
      attribution: "&copy; <a href=\"https://www.openstreetmap.org/copyright\">OpenStreetMap</a> contributors",
      maxNativeZoom: 19 }),
    terrain: () => ({
      url: `${ESRI}/World_Topo_Map/MapServer/tile/{z}/{y}/{x}`,
      attribution: "Tiles &copy; Esri &mdash; Esri, HERE, Garmin, FAO, NOAA, USGS, &copy; OpenStreetMap contributors",
      maxNativeZoom: 18 }),
    offline: null,
  };
  const MODE_VARS = {shortest: "--rt-c", balanced: "--rt-b", safest: "--rt-a"};
  const TYPE_COLOURS = {container: "#2E6DB4", tanker: "#B03A2E", bulker: "#8A6D3B", cargo: "#5C7A3A",
                        gas: "#C67A21", passenger: "#7E60A6", service: "#5F737F", other: "#7C8A93"};
  const BOARD_COLOURS = {1: null, 2: [70, 130, 200, .45], 3: [200, 140, 60, .45],
                         4: [200, 40, 30, .55], 5: [240, 150, 40, .30]};
  const NM = 1852;

  function unpack(p) {
    if (p.data) return p.data;
    const bin = atob(p.b64), out = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
    return out;
  }
  /* Raster -> image overlay in Web Mercator. At 1.2 deg N the Mercator stretch
     over the study area is 0.03 %, far below a cell, so rows map linearly. */
  function rasterImage(p, colourOf) {
    const v = unpack(p), c = document.createElement("canvas");
    c.width = p.nx; c.height = p.ny;
    const x = c.getContext("2d"), img = x.createImageData(p.nx, p.ny);
    for (let r = 0; r < p.ny; r++) for (let q = 0; q < p.nx; q++) {
      const col = colourOf(v[r * p.nx + q]); if (!col) continue;
      const o = ((p.ny - 1 - r) * p.nx + q) * 4;
      img.data[o] = col[0]; img.data[o + 1] = col[1]; img.data[o + 2] = col[2]; img.data[o + 3] = col[3] * 255;
    }
    x.putImageData(img, 0, 0);
    return {url: c.toDataURL("image/png"),
            bounds: [[p.lat0 - p.dlat / 2, p.lon0 - p.dlon / 2],
                     [p.lat0 + p.dlat * (p.ny - .5), p.lon0 + p.dlon * (p.nx - .5)]]};
  }
  function depthColour(dark) {
    return b => {
      if (b === 255) return null;
      const d = b * .25, t = Math.min(d / 30, 1);
      if (d < 10) return dark ? [120, 60, 50, .55] : [214, 120, 90, .55];
      return dark ? [20 + 10 * t, 50 + 40 * t, 80 + 70 * t, .55] : [200 - 150 * t, 225 - 120 * t, 240 - 60 * t, .55];
    };
  }

  /* offset (m east, m north) -> lat/lon */
  function offsetLL(lat, lon, e, n) {
    return [lat + n / 111320, lon + e / (111320 * Math.cos(lat * Math.PI / 180))];
  }
  function hullPolygon(s) {
    // bow-pointed hull outline at true size around the AIS position
    const h = s.heading_deg * Math.PI / 180, L2 = s.loa_m / 2, B2 = s.beam_m / 2;
    const pts = [[L2, 0], [L2 * .78, B2], [-L2, B2], [-L2, -B2], [L2 * .78, -B2]];
    return pts.map(([f, st]) => offsetLL(s.lat, s.lon, f * Math.sin(h) + st * Math.cos(h),
                                                     f * Math.cos(h) - st * Math.sin(h)));
  }
  function hav(a, b) {
    const R = 3440.065, r = d => d * Math.PI / 180;
    const s = Math.sin(r(b[0] - a[0]) / 2) ** 2 + Math.cos(r(a[0])) * Math.cos(r(b[0])) * Math.sin(r(b[1] - a[1]) / 2) ** 2;
    return 2 * R * Math.asin(Math.min(1, Math.sqrt(s)));
  }
  function bearing(a, b) {
    const r = d => d * Math.PI / 180, p1 = r(a[0]), p2 = r(b[0]), dl = r(b[1] - a[1]);
    return (Math.atan2(Math.sin(dl) * Math.cos(p2), Math.cos(p1) * Math.sin(p2) - Math.sin(p1) * Math.cos(p2) * Math.cos(dl)) * 180 / Math.PI + 360) % 360;
  }

  function create(el, opts) {
    opts = opts || {};
    const renderer = L.canvas({padding: .3});
    const map = L.map(el, {zoomControl: false, preferCanvas: true, renderer,
                           attributionControl: true, minZoom: 10, maxZoom: 18,
                           zoomSnap: .25, wheelPxPerZoomLevel: 90});
    map.attributionControl.setPrefix('<a href="https://leafletjs.com">Leaflet</a> &middot; coast &copy; OpenStreetMap');
    map.createPane("labels").style.zIndex = 350;
    map.getPane("labels").style.pointerEvents = "none";
    const G = {};
    ["depth", "mdepth", "land", "board", "tss", "mtrk", "anch", "mhaz", "mberth", "env", "ships", "routes", "enc", "ports", "own"]
      .forEach(k => { G[k] = L.layerGroup().addTo(map); });
    const st = {meta: null, ships: null, result: null, sel: 0, dark: false, basemap: opts.basemap || "simple",
                layers: {}, tiles: null, labels: null, seamark: null, board: null, boardImg: null,
                depthImg: null, own: null, highlight: null, mpa: null};

    /* ---------------- basemap ---------------- */
    function clearTiles() {
      ["tiles", "labels", "seamark"].forEach(k => { if (st[k]) { map.removeLayer(st[k]); st[k] = null; } });
    }
    function watchTiles(layer, name) {
      let ok = 0, bad = 0;
      layer.on("tileload", () => { ok++; });
      layer.on("tileerror", () => {
        bad++;
        if (bad >= 6 && ok === 0 && st.basemap === name) {
          st.basemap = "offline"; applyBasemap();
          if (opts.onBasemapFail) opts.onBasemapFail(name);
        }
      });
    }
    function applyBasemap() {
      clearTiles();
      el.classList.toggle("offline", st.basemap === "offline");
      const make = BASEMAPS[st.basemap];
      if (make) {
        const b = make(st.dark);
        st.tiles = L.tileLayer(b.url, {attribution: b.attribution, maxZoom: 19,
                                       maxNativeZoom: b.maxNativeZoom || 18, keepBuffer: 3, crossOrigin: true}).addTo(map);
        st.tiles.bringToBack(); watchTiles(st.tiles, st.basemap);
        if (b.labels) st.labels = L.tileLayer(b.labels, {maxZoom: 19, maxNativeZoom: b.maxNativeZoom || 16,
                                                         pane: "labels", opacity: .9}).addTo(map);
        if (b.overlay) st.seamark = L.tileLayer(b.overlay, {maxZoom: 18, opacity: .95}).addTo(map);
      }
      drawLand();
    }
    function drawLand() {
      G.land.clearLayers();
      if (!st.meta || st.basemap !== "offline") return;
      st.meta.land.features.forEach(f => {
        L.polygon(f.geometry.coordinates.map(r => r.map(c => [c[1], c[0]])),
                  {fillColor: css("--land"), fillOpacity: 1, color: css("--land-line"), weight: .7,
                   interactive: false}).addTo(G.land);
      });
    }

    /* ---------------- rasters ---------------- */
    function drawDepth() {
      G.depth.clearLayers();
      if (!st.meta || !st.layers.depth) return;
      if (!st.depthImg) st.depthImg = rasterImage(st.meta.depth, depthColour(st.dark));
      L.imageOverlay(st.depthImg.url, st.depthImg.bounds, {interactive: false, opacity: 1}).addTo(G.depth);
    }
    function drawBoard() {
      G.board.clearLayers();
      if (!st.board || !st.layers.board) return;
      if (!st.boardImg) st.boardImg = rasterImage(st.board, b => BOARD_COLOURS[b] ||
        (b === 1 && st.basemap === "offline" ? null : null));
      L.imageOverlay(st.boardImg.url, st.boardImg.bounds, {interactive: false, opacity: 1,
                                                           className: "board-img"}).addTo(G.board);
    }
    function drawTss() {
      G.tss.clearLayers();
      if (!st.meta || !st.layers.tss) return;
      st.meta.tss.features.forEach(f => {
        const cs = f.geometry.coordinates.map(c => [c[1], c[0]]);
        const lane = f.properties.kind === "tss_lane";
        const l = (lane ? L.polygon : L.polyline)(cs, {color: css("--canal"), weight: 1, fill: lane,
          fillOpacity: .06, dashArray: lane ? null : "2 5", interactive: lane});
        if (lane) l.bindTooltip(`TSS ${f.properties.direction} lane (schematic)`, {sticky: true});
        l.addTo(G.tss);
      });
    }
    function drawAnch() {
      G.anch.clearLayers();
      if (!st.meta || !st.layers.anch) return;
      const seen = new Set();
      st.meta.anchorages.forEach(a => {
        const m = L.circleMarker([a.lat, a.lon], {radius: 2.5, color: css("--anch"), weight: 1,
                                                  fillOpacity: .6, fillColor: css("--anch")});
        m.bindTooltip(`<b>${a.code}</b> ${a.name}<br>${a.use} anchorage &middot; charted ${a.charted_m} m`, {sticky: true});
        m.addTo(G.anch);
        if (!seen.has(a.code) && map.getZoom() >= 12) {
          seen.add(a.code);
          L.tooltip({permanent: true, direction: "right", offset: [4, 0], className: "anch-label"})
            .setLatLng([a.lat, a.lon]).setContent(a.code).addTo(G.anch);
        }
      });
      st.meta.pilot_boarding.forEach(p => {
        L.circleMarker([p.lat, p.lon], {radius: 3.5, color: css("--anch"), weight: 1.5, fillOpacity: 0})
          .bindTooltip(`<b>${p.code}</b> ${p.name}`, {sticky: true}).addTo(G.anch);
      });
    }

    /* ---------------- MPA chart layers (mpa.json) ---------------- */
    const MPA_BANDS = [[0, "#c9a86a", "drying / < 0 m"], [2, "#e7f0f7", "0 – 5 m"], [5, "#cfe3f1", "5 – 10 m"],
                       [10, "#a9cde8", "10 – 15 m"], [15, "#7fb3dc", "15 – 20 m"], [20, "#5596cb", "20 – 30 m"],
                       [30, "#2f73b0", "30 m +"]];
    function bandColour(d) {
      if (d == null || d <= 0) return MPA_BANDS[0][1];
      let c = MPA_BANDS[1][1];
      MPA_BANDS.forEach(([lo, col]) => { if (d >= lo && lo > 0) c = col; });
      return d < 5 ? MPA_BANDS[1][1] : c;
    }
    const ll = cs => cs.map(c => [c[1], c[0]]);
    function eachPart(g, fn) {
      const t = g.type, c = g.coordinates;
      if (t === "Point") fn("pt", c);
      else if (t === "MultiPoint") c.forEach(x => fn("pt", x));
      else if (t === "LineString") fn("ln", c);
      else if (t === "MultiLineString") c.forEach(x => fn("ln", x));
      else if (t === "Polygon") fn("pg", c);
      else if (t === "MultiPolygon") c.forEach(x => fn("pg", x));
    }
    function drawMpa() {
      ["mdepth", "mtrk", "mhaz", "mberth"].forEach(k => G[k].clearLayers());
      const M = st.mpa;
      if (!M) return;
      if (st.layers.mdepth) M.depth.features.forEach(f => {
        const p = f.properties, col = bandColour(p.d1);
        const tip = p.dredged ? `<b>Dredged area</b> &middot; maintained ${p.d1} m${p.name ? "<br>" + p.name : ""}`
                              : `<b>Charted depth</b> ${p.d1} – ${p.d2 ?? "?"} m<br><span style="opacity:.75">the router counts ${Math.max(p.d1, 0)} m (shallow end)</span>`;
        eachPart(f.geometry, (k, c) => {
          if (k !== "pg") return;
          L.polygon(c.map(ll), {stroke: !!p.dredged, color: "#1f5f99", weight: .6, dashArray: p.dredged ? "3 3" : null,
                                fillColor: col, fillOpacity: st.dark ? .55 : .65})
            .bindTooltip(tip, {sticky: true}).addTo(G.mdepth);
        });
      });
      if (st.layers.mtrk) M.tracks.features.forEach(f => {
        const p = f.properties;
        const tip = `<b>${p.kind}</b>${p.orient != null ? ` &middot; ${p.orient}°` : ""}${p.traffic ? ` &middot; ${p.traffic}` : ""}` +
                    `${p.depth != null ? `<br>depth ${p.depth} m` : ""}${p.name ? "<br>" + p.name : ""}${p.inform ? `<br><span style="opacity:.75">${p.inform}</span>` : ""}`;
        eachPart(f.geometry, (k, c) => {
          if (k === "pg") L.polygon(c.map(ll), {color: "#8a3ffc", weight: 1.2, fillColor: "#8a3ffc", fillOpacity: .08})
            .bindTooltip(tip, {sticky: true}).addTo(G.mtrk);
          else if (k === "ln") L.polyline(ll(c), {color: "#8a3ffc", weight: 1.6, dashArray: "8 5"})
            .bindTooltip(tip, {sticky: true}).addTo(G.mtrk);
          else L.circleMarker([c[1], c[0]], {radius: 3, color: "#8a3ffc", weight: 1.5, fillOpacity: 0})
            .bindTooltip(tip).addTo(G.mtrk);
        });
      });
      if (st.layers.mhaz) M.hazards.features.forEach(f => {
        const p = f.properties, col = p.routed ? css("--bad") : "#8c8c8c";
        const tip = `<b>${p.kind}</b>${p.depth != null ? ` &middot; least depth ${p.depth} m` : " &middot; depth unknown"}` +
                    `<br>${p.routed ? "the router keeps out of water shallower than the ship needs here" : "shown only (not routed round)"}` +
                    `${p.name ? "<br>" + p.name : ""}${p.inform ? `<br><span style="opacity:.75">${p.inform}</span>` : ""}`;
        eachPart(f.geometry, (k, c) => {
          if (k === "pt") L.circleMarker([c[1], c[0]], {radius: map.getZoom() >= 14 ? 4.5 : 3, color: "#fff", weight: 1,
                                                       fillColor: col, fillOpacity: .95}).bindTooltip(tip).addTo(G.mhaz);
          else if (k === "ln") L.polyline(ll(c), {color: col, weight: 2}).bindTooltip(tip, {sticky: true}).addTo(G.mhaz);
          else L.polygon(c.map(ll), {color: col, weight: 1, fillColor: col, fillOpacity: .18, dashArray: "2 3"})
            .bindTooltip(tip, {sticky: true}).addTo(G.mhaz);
        });
      });
      if (st.layers.mberth) {
        const z = map.getZoom();
        M.berths.features.forEach(f => {
          const p = f.properties, c = f.geometry.coordinates;
          const pick = opts.berthLookup && opts.berthLookup(p.name);
          const m = L.circleMarker([c[1], c[0]], {radius: pick ? (z >= 14 ? 5 : 3.5) : 2.5, weight: 1.2, color: "#fff",
            fillColor: pick ? "#0b8f6a" : "#6b7a86", fillOpacity: 1});
          m.bindTooltip(`<b>${p.name}</b>${pick ? `<br>${pick.depthText}<br><i>click to sail here</i>` : ""}`,
                        {direction: "top"});
          if (pick && opts.onBerthClick) m.on("click", () => opts.onBerthClick(pick.code, pick.id));
          m.addTo(G.mberth);
          if (pick && z >= 15) L.tooltip({permanent: true, direction: "right", offset: [5, 0], className: "anch-label"})
            .setLatLng([c[1], c[0]]).setContent(pick.id).addTo(G.mberth);
        });
      }
    }

    /* ---------------- ships ---------------- */
    function encounterIds() {
      const r = st.result, p = r && r.found ? r.plans[st.sel] : null;
      return new Map(((p && p.encounters) || []).map(e => [e.id, e]));
    }
    function drawShips() {
      G.ships.clearLayers(); G.env.clearLayers();
      if (!st.ships || !st.layers.ships) return;
      const z = map.getZoom(), enc = encounterIds(), big = z >= 13.5;
      const ignored = new Set(opts.ignoredTypes ? opts.ignoredTypes() : []);
      st.ships.ships.forEach(s => {
        const col = TYPE_COLOURS[s.ship_type] || TYPE_COLOURS.other, hit = enc.get(s.id);
        const off = ignored.has(s.ship_type);
        const tip = `<b>${s.name || s.id}</b>${s.mmsi ? " &middot; MMSI " + s.mmsi : ""}<br>`
          + `${s.ship_type} &middot; ${s.status} &middot; ${Math.round(s.loa_m)} &times; ${Math.round(s.beam_m)} m`
          + `${s.area ? " &middot; " + s.area : ""}<br>${s.lat.toFixed(5)}, ${s.lon.toFixed(5)} &middot; hdg ${Math.round(s.heading_deg)}&deg;`
          + (hit ? `<br><b style="color:var(--bad)">passed at ${hit.cpa_nm.toFixed(2)} NM (target ${hit.target_nm} NM)</b>` : "")
          + (off ? "<br><i>ignored by the router (type unticked)</i>" : "");
        if (st.layers.env && !off) {
          // An anchored ship swings round its ANCHOR, not round itself: the
          // circle is centred on the anchor (ahead of the ship, where the
          // chain leads) and its radius is ship length + cable scope.
          L.circle([s.env[0], s.env[1]], {radius: s.env[2] * NM, weight: hit ? 1.4 : .7,
            color: hit ? css("--bad") : col, opacity: hit ? .9 : .45, fillOpacity: hit ? .10 : .04,
            dashArray: s.status === "anchored" ? "3 3" : null, interactive: false}).addTo(G.env);
          if (s.status === "anchored" && z >= 12.5 && hav([s.lat, s.lon], [s.env[0], s.env[1]]) > .01) {
            const h = s.heading_deg * Math.PI / 180, L2 = s.loa_m / 2;
            const bow = offsetLL(s.lat, s.lon, L2 * Math.sin(h), L2 * Math.cos(h));
            L.polyline([bow, [s.env[0], s.env[1]]], {color: col, weight: 1, opacity: .7,
              dashArray: "2 3", interactive: false}).addTo(G.env);
            L.circleMarker([s.env[0], s.env[1]], {radius: 2.5, weight: 1.2, color: col, fillColor: "#fff",
              fillOpacity: 1, interactive: true})
              .bindTooltip(`anchor of <b>${s.name || s.id}</b> &middot; swing circle ${(s.env[2] * NM).toFixed(0)} m`, {sticky: true})
              .addTo(G.env);
          }
        }
        const m = big
          ? L.polygon(hullPolygon(s), {color: hit ? css("--bad") : "#fff", weight: 1, fillColor: col,
                                       fillOpacity: off ? .35 : .95})
          : L.circleMarker([s.lat, s.lon], {radius: Math.max(2.5, Math.min(6, s.loa_m / 60)),
              color: hit ? css("--bad") : "#fff", weight: hit ? 2 : 1, fillColor: col,
              fillOpacity: off ? .35 : .95});
        m.bindTooltip(tip, {sticky: true});
        if (opts.onShipClick) m.on("click", () => opts.onShipClick(s));
        m.addTo(G.ships);
      });
    }

    /* ---------------- routes & close passes ---------------- */
    function drawRoutes() {
      G.routes.clearLayers(); G.enc.clearLayers();
      const r = st.result;
      if (!r || !r.found) return;
      const order = r.plans.map((_, i) => i).filter(i => i !== st.sel).concat([st.sel]);
      order.forEach(i => {
        const sel = i === st.sel, p = r.plans[i];
        if (!sel && !st.layers.alt) return;
        const col = css(MODE_VARS[p.mode] || "--rt-b");
        if (sel) L.polyline(p.coords, {color: "#ffffff", weight: 7, opacity: .85, interactive: false}).addTo(G.routes);
        const pl = L.polyline(p.coords, {color: col, weight: sel ? 3.4 : 2, opacity: sel ? 1 : .65,
                                          dashArray: sel ? null : "6 5"}).addTo(G.routes);
        pl.bindTooltip(`${p.label}: ${p.metrics.distance_nm.toFixed(2)} NM, ${p.metrics.obstacles_met} ships met`, {sticky: true});
        if (!sel && opts.onRouteClick) pl.on("click", () => opts.onRouteClick(i));
        if (sel) p.coords.slice(1, -1).forEach(c => L.circleMarker(c, {radius: 2.4, color: col, weight: 1,
          fillColor: "#fff", fillOpacity: 1, interactive: false}).addTo(G.routes));
      });
      const p = r.plans[st.sel];
      (p.encounters || []).forEach(e => {
        const bad = e.cpa_nm < e.hard_nm;
        L.polyline([e.at, e.ship], {color: css("--bad"), weight: 1, dashArray: "2 3", interactive: false}).addTo(G.enc);
        const m = L.circleMarker(e.at, {radius: 3.5, color: css("--bad"), weight: 1.5, fillColor: "#fff", fillOpacity: 1});
        m.bindTooltip(`${e.cpa_nm.toFixed(2)} NM`, {permanent: map.getZoom() >= 13, direction: "top",
                                                    className: "cpa-label", offset: [0, -4]});
        m.on("click", () => opts.onEncounterClick && opts.onEncounterClick(e));
        m.addTo(G.enc);
      });
    }
    function drawPorts() {
      G.ports.clearLayers();
      if (!st.meta) return;
      const r = st.result, pts = [];
      st.meta.ports.forEach(p => {
        const on = r && (p.code === r.origin || p.code === r.destination);
        const at = p.kind === "terminal" ? [p.berth_lat, p.berth_lon] : [p.lat, p.lon];
        if (p.pilot && p.pilot.length > 1)                // pilotage track to the berth
          L.polyline(p.pilot, {color: css("--ink-2"), weight: 1.5, dashArray: "4 4", opacity: .8,
                               interactive: false}).addTo(G.ports);
        const m = L.circleMarker(at, {radius: on ? 6 : 4, weight: 2, color: "#ffffff",
          fillColor: on ? css("--ink") : css("--ink-2"), fillOpacity: 1});
        const nm = p.berth_name || p.name;
        const lab = on ? (p.code === r.origin ? "FROM " : "TO ") + nm : nm;
        m.bindTooltip(lab, {permanent: true, direction: "right", offset: [8, 0],
                            className: on ? "port-label on" : "port-label"});
        if (opts.onPortClick) m.on("click", () => opts.onPortClick(p.code));
        m.addTo(G.ports);
      });
      if (r && r.found) {
        const p = r.plans[st.sel];
        [p.coords[0], p.coords[p.coords.length - 1]].forEach((c, i) => {
          const code = i ? r.destination : r.origin;
          if (!st.meta.ports.find(q => q.code === code))
            L.circleMarker(c, {radius: 6, weight: 2, color: "#fff", fillColor: css("--ink"), fillOpacity: 1})
              .bindTooltip((i ? "TO " : "FROM ") + (i ? r.destination_label : r.origin_label),
                           {permanent: true, direction: "right", offset: [8, 0], className: "port-label on"})
              .addTo(G.ports);
        });
      }
    }

    /* ---------------- own ship ---------------- */
    function setOwn(own) {
      G.own.clearLayers(); st.own = own;
      if (!own) return;
      const nearest = (own.nearest || []).slice(0, 3);
      nearest.forEach(n => {
        L.polyline([[own.lat, own.lon], [n.lat, n.lon]], {color: css(n.cls === "bad" ? "--bad" : n.cls === "warn" ? "--warn" : "--ink-2"),
          weight: 1.2, dashArray: "4 3", interactive: false}).addTo(G.own);
        L.tooltip({permanent: true, direction: "center", className: "port-label"})
          .setLatLng([(own.lat + n.lat) / 2, (own.lon + n.lon) / 2])
          .setContent(`${n.hull_nm.toFixed(2)} NM`).addTo(G.own);
      });
      if (map.getZoom() >= 13.5 && own.vessel) {
        L.polygon(hullPolygon({lat: own.lat, lon: own.lon, heading_deg: own.heading,
                               loa_m: own.vessel.loa_m, beam_m: own.vessel.beam_m}),
                  {color: "#fff", weight: 1.5, fillColor: css("--rt-a"), fillOpacity: 1}).addTo(G.own);
      }
      const icon = L.divIcon({className: "ship-icon", iconSize: [30, 30], iconAnchor: [15, 15],
        html: `<div style="transform:rotate(${own.heading}deg)"><svg viewBox="0 0 30 30" width="30" height="30">
          <path d="M15 3 L21 22 L15 18.5 L9 22 Z" fill="${css("--rt-a")}" stroke="#fff" stroke-width="1.6"
          stroke-linejoin="round"/></svg></div>`});
      const m = L.marker([own.lat, own.lon], {icon, zIndexOffset: 1000, title: "Open 3D ship view"});
      m.bindTooltip(own.label || "own ship", {direction: "top", offset: [0, -12]});
      if (opts.onOwnClick) m.on("click", opts.onOwnClick);
      m.addTo(G.own);
    }

    /* ---------------- status ---------------- */
    function dm(v, hemi) {
      const h = v < 0 ? hemi[1] : hemi[0], a = Math.abs(v), d = Math.floor(a), mm = (a - d) * 60;
      return `${d}°${mm.toFixed(2).padStart(5, "0")}'${h}`;
    }
    map.on("mousemove", e => {
      if (!opts.cursorEl) return;
      let t = dm(e.latlng.lat, "NS") + "  " + dm(e.latlng.lng, "EW");
      if (st.meta && st.meta.depth) {
        const p = st.meta.depth, r = Math.round((e.latlng.lat - p.lat0) / p.dlat), q = Math.round((e.latlng.lng - p.lon0) / p.dlon);
        if (r >= 0 && r < p.ny && q >= 0 && q < p.nx) {
          if (!st.depthVals) st.depthVals = unpack(p);
          const b = st.depthVals[r * p.nx + q];
          t += b === 255 ? "  · land" : `  · ${(b * .25).toFixed(1)} m`;
        }
      }
      opts.cursorEl.textContent = t;
    });
    map.on("click", e => { if (opts.onMapClick) opts.onMapClick(e.latlng); });
    function updateScale() {
      if (!opts.scaleLine) return;
      const c = map.getCenter(), z = map.getZoom();
      const nmPerPx = 40075016.686 * Math.cos(c.lat * Math.PI / 180) / Math.pow(2, z + 8) / NM;
      const steps = [0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 20];
      const nm = steps.find(t => t / nmPerPx > 70) || 20;
      opts.scaleLine.style.width = Math.round(nm / nmPerPx) + "px";
      opts.scaleText.textContent = nm + " NM";
    }
    let lastBig = null;
    map.on("zoomend", () => {
      const big = map.getZoom() >= 13.5;
      if (big !== lastBig) { lastBig = big; drawShips(); drawAnch(); drawRoutes(); if (st.own) setOwn(st.own); }
      if (st.mpa && (st.layers.mberth || st.layers.mhaz)) { G.mberth.clearLayers(); drawMpa(); }
    });
    map.on("zoomend moveend", updateScale);

    function redrawAll() { drawLand(); drawDepth(); drawMpa(); drawBoard(); drawTss(); drawAnch(); drawShips(); drawRoutes(); drawPorts(); }
    const api = {
      map,
      setMeta(meta) {
        st.meta = meta; st.depthImg = null; st.depthVals = null;
        const b = meta.bbox, p = .12;
        map.setMaxBounds([[b.lat_min - p, b.lon_min - p], [b.lat_max + p, b.lon_max + p]]);
        api.fitAll(); applyBasemap(); redrawAll();
      },
      setShips(ships) { st.ships = ships; drawShips(); },
      setMpa(m) { st.mpa = m; drawMpa(); },
      redrawMpa() { drawMpa(); },
      MPA_BANDS,
      setBoard(board) { st.board = board; st.boardImg = null; drawBoard(); },
      setResult(result, sel) { st.result = result; st.sel = sel || 0; drawRoutes(); drawPorts(); drawShips(); },
      setSelected(sel) { st.sel = sel; drawRoutes(); drawShips(); drawPorts(); },
      setLayers(layers) { st.layers = Object.assign({}, layers); redrawAll(); },
      setBasemap(name) { st.basemap = BASEMAPS.hasOwnProperty(name) ? name : "simple"; applyBasemap(); },
      get basemap() { return st.basemap; },
      setTheme(dark) { st.dark = !!dark; st.depthImg = null; applyBasemap(); redrawAll(); },
      setOwn,
      fitResult() {
        const r = st.result;
        if (!r || !r.found) return api.fitAll();
        const pts = []; r.plans.forEach(p => p.coords.forEach(c => pts.push(c)));
        map.fitBounds(L.latLngBounds(pts), {padding: [40, 40], maxZoom: 14});
      },
      fitAll() { const b = st.meta.bbox; map.fitBounds([[b.lat_min, b.lon_min], [b.lat_max, b.lon_max]]); },
      focus(lat, lon, z) { map.setView([lat, lon], z || Math.max(map.getZoom(), 14.5)); },
      zoomIn() { map.zoomIn(); }, zoomOut() { map.zoomOut(); },
      invalidate() { map.invalidateSize(); },
      pointAlong(coords, f) {
        const cum = [0];
        for (let i = 1; i < coords.length; i++) cum.push(cum[i - 1] + hav(coords[i - 1], coords[i]));
        const tgt = Math.max(0, Math.min(1, f)) * cum[cum.length - 1];
        let i = 1; while (i < coords.length - 1 && cum[i] < tgt) i++;
        const a = coords[i - 1], b = coords[i], t = Math.max(0, Math.min(1, (tgt - cum[i - 1]) / ((cum[i] - cum[i - 1]) || 1e-9)));
        return {lat: a[0] + (b[0] - a[0]) * t, lon: a[1] + (b[1] - a[1]) * t, heading: bearing(a, b),
                nm: tgt, total: cum[cum.length - 1]};
      },
      hav, bearing, TYPE_COLOURS, BOARD_COLOURS,
    };
    return api;
  }
  root.ChartMap = {create};
})(window);
