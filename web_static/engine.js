/* Strait Safe Route - the routing engine, in the browser.
 *
 * A line-for-line port of the Python package (seagrid / obstacles / network /
 * algorithms / routing): same board, same hard limits, same penalties, same
 * TSS rules, same smoothing and the same safety metrics. The graph is implicit:
 * edge costs are computed as the search relaxes them, from per-cell arrays.
 */
(function (root) {
  "use strict";
  const R_NM = 6371008.8 / 1852;
  const M_PER_NM = 1852;
  const rad = d => d * Math.PI / 180;
  const deg = r => r * 180 / Math.PI;
  function hav(la1, lo1, la2, lo2) {
    const p1 = rad(la1), p2 = rad(la2), dp = p2 - p1, dl = rad(lo2 - lo1);
    const a = Math.sin(dp / 2) ** 2 + Math.cos(p1) * Math.cos(p2) * Math.sin(dl / 2) ** 2;
    return 2 * R_NM * Math.asin(Math.sqrt(Math.min(1, Math.max(0, a))));
  }
  function initialBearing(la1, lo1, la2, lo2) {
    const p1 = rad(la1), p2 = rad(la2), dl = rad(lo2 - lo1);
    return (deg(Math.atan2(Math.sin(dl) * Math.cos(p2),
      Math.cos(p1) * Math.sin(p2) - Math.sin(p1) * Math.cos(p2) * Math.cos(dl))) + 360) % 360;
  }
  const clip = (v, a, b) => Math.max(a, Math.min(b, v));

  function unb64(s, Type) {
    const bin = atob(s), buf = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) buf[i] = bin.charCodeAt(i);
    return new Type(buf.buffer);
  }

  // ---------------------------------------------------------------- board
  const FREE = 0, LAND = 1, SHALLOW = 2, SHORE = 3, SHIP_HARD = 4, SHIP_SOFT = 5;
  const N16 = [[-1, -1], [-1, 0], [-1, 1], [0, -1], [0, 1], [1, -1], [1, 0], [1, 1],
               [-2, -1], [-2, 1], [-1, -2], [-1, 2], [1, -2], [1, 2], [2, -1], [2, 1]];
  const MODES = {
    shortest: {w_ship: 0, w_shore: 0, w_depth: 0, w_entry: 0},
    balanced: {w_ship: 1.5, w_shore: .30, w_depth: .35, w_entry: 1.0},
    safest:   {w_ship: 3.0, w_shore: .80, w_depth: 1.0, w_entry: 8.0},
  };
  const BASE_CFG = {w_tss: 1.6, ukc_tss_m: 3.5, ukc_fraction: .10, ukc_min_m: 1.0,
                    min_shore_nm: .08, shore_per_loa: .5, tss_wrong_way_deg: 112.5,
                    tss_cross_tolerance_deg: 35, inshore_transit_penalty: 1.4, wrong_side_penalty: 3.0};

  function makeGrid(data) {
    const G = data.grid, n = G.ny * G.nx, L = data.layers;
    const g = {ny: G.ny, nx: G.nx, n, lat0: G.lat0, lon0: G.lon0, res: G.res,
               lats: new Float64Array(G.ny), lons: new Float64Array(G.nx)};
    for (let i = 0; i < G.ny; i++) g.lats[i] = G.lat0 + i * G.res;
    for (let j = 0; j < G.nx; j++) g.lons[j] = G.lon0 + j * G.res;
    g.land = unb64(L.land, Uint8Array);
    const dmm = unb64(L.dist_mm, Uint16Array), dcm = unb64(L.depth_cm, Uint16Array);
    const hd = unb64(L.tss_heading_d10, Uint16Array);
    g.dist = new Float64Array(n); g.depth = new Float64Array(n); g.head = new Float64Array(n);
    for (let k = 0; k < n; k++) {
      g.dist[k] = dmm[k] / 1000; g.depth[k] = dcm[k] / 100;
      g.head[k] = hd[k] === 65535 ? NaN : hd[k] / 10;
    }
    g.zone = unb64(L.tss_zone, Uint8Array);
    g.cpaZone = unb64(L.cpa_zone, Uint8Array);
    const midLat = G.lat0 + (G.ny - 1) * G.res / 2;
    g.dyNm = G.res * 60; g.dxNm = G.res * 60 * Math.cos(rad(midLat));
    g.toIndex = (la, lo) => [clip(Math.round((la - g.lat0) / g.res), 0, g.ny - 1),
                             clip(Math.round((lo - g.lon0) / g.res), 0, g.nx - 1)];
    g.inside = (la, lo) => la >= g.lats[0] - 1e-9 && la <= g.lats[g.ny - 1] + 1e-9 &&
                           lo >= g.lons[0] - 1e-9 && lo <= g.lons[g.nx - 1] + 1e-9;
    return g;
  }

  function nearestNavigable(g, la, lo, nav, maxR = 60) {
    const [iy0, ix0] = g.toIndex(la, lo);
    if (nav[iy0 * g.nx + ix0]) return [iy0, ix0];
    for (let r = 1; r <= maxR; r++) {
      let best = null, bd = Infinity;
      for (let y = Math.max(iy0 - r, 0); y <= Math.min(iy0 + r, g.ny - 1); y++)
        for (let x = Math.max(ix0 - r, 0); x <= Math.min(ix0 + r, g.nx - 1); x++) {
          if (!nav[y * g.nx + x]) continue;
          const d = hav(la, lo, g.lats[y], g.lons[x]);
          if (d < bd) { bd = d; best = [y, x]; }
        }
      if (best) return best;
    }
    return null;
  }

  // ------------------------------------------------------------ obstacles
  function envelope(s, swing) {
    if (s.env_swing && s.env_hull) return swing ? s.env_swing : s.env_hull;
    const depth = Math.max(s.depth_hint_m || 20, 5);
    const chain = 1.5 * Math.sqrt(depth) * 27.5;
    const scope = Math.sqrt(Math.max(chain * chain - (s.depth_hint_m || 20) ** 2, 0));
    if (s.status === "anchored" && swing) {
      const ahead = .8 * s.loa_m + scope, h = rad(s.heading_deg || 0);
      return [s.lat + ahead * Math.cos(h) / 111320,
              s.lon + ahead * Math.sin(h) / (111320 * Math.cos(rad(s.lat))),
              (s.loa_m + scope) / M_PER_NM];
    }
    const r = s.status === "moored" ? s.loa_m / 2 : s.loa_m / 2 + 100;
    return [s.lat, s.lon, r / M_PER_NM];
  }

  function obstacleField(g, ships, swing, maxRange = 2.0) {
    const clear = new Float64Array(g.n).fill(maxRange), near = new Int32Array(g.n).fill(-1);
    const env = ships.map(s => envelope(s, swing));
    env.forEach(([la, lo, r], i) => {
      const reach = (r + maxRange) / 60, rx = reach / Math.cos(rad(la));
      const y0 = Math.max(Math.trunc((la - reach - g.lat0) / g.res), 0);
      const y1 = Math.min(Math.trunc((la + reach - g.lat0) / g.res) + 2, g.ny);
      const x0 = Math.max(Math.trunc((lo - rx - g.lon0) / g.res), 0);
      const x1 = Math.min(Math.trunc((lo + rx - g.lon0) / g.res) + 2, g.nx);
      for (let y = y0; y < y1; y++) for (let x = x0; x < x1; x++) {
        const c = hav(g.lats[y], g.lons[x], la, lo) - r, k = y * g.nx + x;
        if (c < clear[k]) { clear[k] = c; near[k] = i; }
      }
    });
    return {clear, near, env};
  }

  // ----------------------------------------------------------- network
  function buildNet(g, data, vessel, mode, field, opts = {}) {
    const cfg = Object.assign({}, BASE_CFG, MODES[mode], {mode, cpa_scale: opts.cpaScale || 1});
    const n = g.n, nx = g.nx;
    const halfBeam = .5 * vessel.beam_m / M_PER_NM;
    const reqS = Math.max(cfg.min_shore_nm, cfg.shore_per_loa * vessel.loa_m / M_PER_NM);
    const ukcElse = Math.max(cfg.ukc_min_m, cfg.ukc_fraction * vessel.draught_m);
    const rules = data.meta.cpa_rules, zkeys = ["approach", "tss", "basin"];
    const hard = new Float64Array(n), target = new Float64Array(n), reqD = new Float64Array(n);
    for (let k = 0; k < n; k++) {
      const r = rules[zkeys[g.cpaZone[k]]];
      hard[k] = r.hard_nm * cfg.cpa_scale; target[k] = r.target_nm * cfg.cpa_scale;
      const inTss = g.zone[k] >= 1 && g.zone[k] <= 3;
      reqD[k] = vessel.draught_m + (inTss ? cfg.ukc_tss_m : ukcElse);
    }
    const margin = .5 * Math.hypot(g.dyNm, g.dxNm);
    // 3x3 minimum of depth ("nearest" edge mode)
    const dmin = new Float64Array(n);
    for (let y = 0; y < g.ny; y++) for (let x = 0; x < nx; x++) {
      let m = Infinity;
      for (let dy = -1; dy <= 1; dy++) for (let dx = -1; dx <= 1; dx++) {
        const yy = clip(y + dy, 0, g.ny - 1), xx = clip(x + dx, 0, nx - 1);
        const v = g.depth[yy * nx + xx]; if (v < m) m = v;
      }
      dmin[y * nx + x] = m;
    }
    const board = new Uint8Array(n);
    for (let k = 0; k < n; k++) {
      let b = FREE;
      if (field) {
        const c = field.clear[k] - halfBeam;
        if (c < target[k]) b = SHIP_SOFT;
        if (c < hard[k] + margin) b = SHIP_HARD;
      }
      if (g.dist[k] - margin < reqS) b = SHORE;
      if (dmin[k] < reqD[k]) b = SHALLOW;
      if (g.land[k]) b = LAND;
      board[k] = b;
    }
    let nav = new Uint8Array(n);
    for (let k = 0; k < n; k++) nav[k] = board[k] === FREE || board[k] === SHIP_SOFT ? 1 : 0;
    // keep the water bodies that contain a port approach (8-connectivity)
    const lab = new Int32Array(n).fill(0), keep = new Uint8Array(n);
    const q = new Int32Array(n);
    data.meta.ports.forEach(p => {
      const s = nearestNavigable(g, p.lat, p.lon, nav);
      if (!s) return;
      const k0 = s[0] * nx + s[1];
      if (keep[k0]) return;
      let h = 0, t = 0; q[t++] = k0; keep[k0] = 1;
      while (h < t) {
        const k = q[h++], y = (k / nx) | 0, x = k - y * nx;
        for (let dy = -1; dy <= 1; dy++) for (let dx = -1; dx <= 1; dx++) {
          const yy = y + dy, xx = x + dx;
          if (yy < 0 || yy >= g.ny || xx < 0 || xx >= nx) continue;
          const kk = yy * nx + xx;
          if (nav[kk] && !keep[kk]) { keep[kk] = 1; q[t++] = kk; }
        }
      }
    });
    nav = keep;
    // per-cell penalty terms
    const wNode = new Float64Array(n), inside = new Uint8Array(n);
    for (let k = 0; k < n; k++) {
      if (!nav[k]) continue;
      let ps = 0;
      if (field) {
        const c = field.clear[k] - halfBeam, h0 = hard[k], t0 = target[k];
        ps = clip((t0 - c) / Math.max(t0 - h0, 1e-6), 0, 1) ** 2;
        inside[k] = c < t0 ? 1 : 0;
      }
      const pl = clip(Math.exp(-(g.dist[k] - reqS) / .30), 0, 1);
      const mg = (g.depth[k] - reqD[k]) / Math.max(reqD[k], 1);
      const pd = clip(Math.exp(-mg / .20), 0, 1);
      wNode[k] = cfg.w_ship * ps + cfg.w_shore * pl + cfg.w_depth * pd;
    }
    let nodes = 0; for (let k = 0; k < n; k++) nodes += nav[k];
    return {g, data, vessel, cfg, field, board, nav, hard, target, reqD, reqS, halfBeam,
            wNode, inside, nodes};
  }

  function tssPenalty(zu, zv, hu, hv, bearing, cfg) {
    let pen = 0, allowed = true;
    const laneU = zu === 1 || zu === 3, laneV = zv === 1 || zv === 3;
    const same = laneU && laneV && zu === zv;
    const band = zu === 4 || zv === 4;
    const ref = isNaN(hu) ? hv : hu;
    if (same) {
      const dev = Math.abs(((bearing - ref + 180) % 360 + 360) % 360 - 180);
      if (dev > cfg.tss_wrong_way_deg) allowed = false;
      else pen = (dev / cfg.tss_wrong_way_deg) ** 2;
    }
    if (band && !isNaN(ref)) {
      const d = rad(((bearing - ref + 180) % 360 + 360) % 360 - 180);
      pen = Math.max(pen, cfg.inshore_transit_penalty * Math.abs(Math.cos(d)));
      if (Math.abs(deg(d)) > cfg.tss_wrong_way_deg) pen = Math.max(pen, cfg.wrong_side_penalty * Math.abs(Math.cos(d)));
    }
    const crossing = (laneU || laneV || zu === 2 || zv === 2) && !same;
    if (crossing && !isNaN(ref)) {
      const d = Math.abs(((bearing - ref + 180) % 360 + 360) % 360 - 180);
      const slack = Math.max(Math.abs(d - 90) - cfg.tss_cross_tolerance_deg, 0);
      pen = Math.max(pen, .40 + 1.60 * (slack / 55) ** 2);
    }
    if (zu === 2 || zv === 2) {
      let base = 1.20;
      if (!isNaN(ref)) {
        const d = Math.abs(((bearing - ref + 180) % 360 + 360) % 360 - 180);
        base = .80 + 3.00 * (Math.abs(d - 90) / 90) ** 2;
      }
      pen = Math.max(pen, base);
    }
    return [pen, allowed];
  }

  function knightClear(nav, nx, ny, y, x, dy, dx) {
    const ok = (yy, xx) => yy >= 0 && yy < ny && xx >= 0 && xx < nx && nav[yy * nx + xx];
    if (Math.abs(dy) <= 1 && Math.abs(dx) <= 1) {
      if (dy !== 0 && dx !== 0) return ok(y + dy, x) && ok(y, x + dx);
      return true;
    }
    const sy = Math.sign(dy), sx = Math.sign(dx);
    return ok(y + sy, x) && ok(y, x + sx) && ok(y + sy, x + sx) && ok(y + dy - sy, x + dx - sx);
  }

  function edgeCost(net, u, v, y, x, yy, xx) {
    const g = net.g, cfg = net.cfg;
    const la = g.lats[y], lo = g.lons[x], la2 = g.lats[yy], lo2 = g.lons[xx];
    const d = hav(la, lo, la2, lo2);
    const bearing = (deg(Math.atan2(rad(lo2 - lo) * Math.cos(rad(la)), rad(la2 - la))) + 360) % 360;
    const [pt, allowed] = tssPenalty(g.zone[u], g.zone[v], g.head[u], g.head[v], bearing, cfg);
    if (!allowed) return [Infinity, d];
    const w = 1 + .5 * (net.wNode[u] + net.wNode[v]) + cfg.w_tss * pt;
    let entry = 0;
    if (net.field && net.inside[v] && (!net.inside[u] || net.field.near[u] !== net.field.near[v])) entry = 1;
    return [d * w + cfg.w_entry * entry, d];
  }

  // --------------------------------------------------------------- search
  function search(net, src, dst, useH = true) {
    const t0 = performance.now();
    const g = net.g, n = g.n, nx = g.nx;
    const gs = new Float64Array(n).fill(Infinity), par = new Int32Array(n).fill(-1);
    const closed = new Uint8Array(n);
    const dla = g.lats[(dst / nx) | 0], dlo = g.lons[dst % nx];
    const H = k => useH ? hav(g.lats[(k / nx) | 0], g.lons[k % nx], dla, dlo) : 0;
    // binary heap of (f, node)
    let hf = new Float64Array(1 << 16), hn = new Int32Array(1 << 16), hs = 0, maxF = 0;
    const push = (f, k) => {
      if (hs >= hf.length) { const a = new Float64Array(hf.length * 2); a.set(hf); hf = a;
                             const b = new Int32Array(hn.length * 2); b.set(hn); hn = b; }
      let i = hs++;
      while (i > 0) { const p = (i - 1) >> 1; if (hf[p] < f || (hf[p] === f && hn[p] <= k)) break;
                      hf[i] = hf[p]; hn[i] = hn[p]; i = p; }
      hf[i] = f; hn[i] = k;
    };
    const pop = () => {
      const k = hn[0], lf = hf[--hs], ln = hn[hs];
      let i = 0;
      while (true) { let c = 2 * i + 1; if (c >= hs) break;
        if (c + 1 < hs && (hf[c + 1] < hf[c] || (hf[c + 1] === hf[c] && hn[c + 1] < hn[c]))) c++;
        if (lf < hf[c] || (lf === hf[c] && ln <= hn[c])) break;
        hf[i] = hf[c]; hn[i] = hn[c]; i = c; }
      hf[i] = lf; hn[i] = ln; return k;
    };
    gs[src] = 0; push(H(src), src);
    let expanded = 0, relaxed = 0;
    while (hs > 0) {
      maxF = Math.max(maxF, hs);
      const u = pop();
      if (closed[u]) continue;
      closed[u] = 1; expanded++;
      if (u === dst) break;
      const y = (u / nx) | 0, x = u - y * nx;
      for (const [dy, dx] of N16) {
        const yy = y + dy, xx = x + dx;
        if (yy < 0 || yy >= g.ny || xx < 0 || xx >= nx) continue;
        const v = yy * nx + xx;
        if (!net.nav[v] || !knightClear(net.knightNav || net.nav, nx, g.ny, y, x, dy, dx)) continue;
        const [c] = edgeCost(net, u, v, y, x, yy, xx);
        if (!isFinite(c)) continue;
        relaxed++;
        const nd = gs[u] + c;
        if (nd < gs[v]) { gs[v] = nd; par[v] = u; push(nd + H(v), v); }
      }
    }
    const found = !!closed[dst];
    const path = [];
    if (found) { let c = dst; while (c !== -1) { path.push(c); if (c === src) break; c = par[c]; } path.reverse(); }
    return {algorithm: useH ? "astar" : "dijkstra", found, path, cost: found ? gs[dst] : Infinity,
            nodes_expanded: expanded, edges_relaxed: relaxed, max_frontier: maxF,
            runtime_ms: performance.now() - t0};
  }

  // ------------------------------------------------------------ smoothing
  function segmentCost(net, a, b) {
    const g = net.g, cfg = net.cfg;
    const step = .4 * Math.min(g.dyNm, g.dxNm);
    const d = hav(a[0], a[1], b[0], b[1]);
    if (d <= 1e-9) return [0, 0, true];
    let n = Math.max(2, Math.ceil(d / step) + 1);
    const short = d <= 1.6 * Math.hypot(g.dyNm, g.dxNm);
    const ks = [];
    for (let i = 0; i < n; i++) {
      const t = i / (n - 1);
      const [iy, ix] = g.toIndex(a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t);
      ks.push(iy * g.nx + ix);
    }
    let S = ks;
    if (short) S = [ks[0], ks[ks.length - 1]];
    else if (!ks.every(k => net.nav[k])) return [Infinity, d, false];
    const bearing = (deg(Math.atan2(rad(b[1] - a[1]) * Math.cos(rad(a[0])), rad(b[0] - a[0]))) + 360) % 360;
    const w = [];
    for (const k of S) {
      let ps = 0;
      if (net.field) {
        const c = net.field.clear[k] - net.halfBeam, h0 = net.hard[k], t0 = net.target[k];
        ps = clip((t0 - c) / Math.max(t0 - h0, 1e-6), 0, 1) ** 2;
      }
      const pl = clip(Math.exp(-(g.dist[k] - net.reqS) / .30), 0, 1);
      const mg = (g.depth[k] - net.reqD[k]) / Math.max(net.reqD[k], 1);
      const pd = clip(Math.exp(-mg / .20), 0, 1);
      const [pt, ok] = tssPenalty(g.zone[k], g.zone[k], g.head[k], g.head[k], bearing, cfg);
      if (!short && !ok) return [Infinity, d, false];
      w.push(1 + cfg.w_ship * ps + cfg.w_shore * pl + cfg.w_depth * pd + cfg.w_tss * pt);
    }
    let m = 0; for (let i = 0; i < w.length - 1; i++) m += .5 * (w[i] + w[i + 1]); m /= (w.length - 1);
    let entries = 0;
    if (net.field && cfg.w_entry > 0 && S.length > 1) {
      const ins = S.map(k => (net.field.clear[k] - net.halfBeam) < net.target[k]);
      for (let i = 1; i < S.length; i++)
        if (ins[i] && (!ins[i - 1] || net.field.near[S[i]] !== net.field.near[S[i - 1]])) entries++;
    }
    return [m * d + cfg.w_entry * entries, d, true];
  }

  function smooth(net, coords, tol = 1.002, maxLook = 256) {
    if (coords.length < 3) return coords.slice();
    const prefix = [0];
    for (let i = 1; i < coords.length; i++) {
      const [c, , ok] = segmentCost(net, coords[i - 1], coords[i]);
      prefix.push(prefix[i - 1] + (ok ? c : Infinity));
    }
    const okCut = (i, j) => { const [c, , ok] = segmentCost(net, coords[i], coords[j]);
                              return ok && c <= (prefix[j] - prefix[i]) * tol; };
    const out = [coords[0]], n = coords.length;
    let i = 0;
    while (i < n - 1) {
      let lo = i + 1, step = 1;
      while (i + 2 * step < n && step <= maxLook && okCut(i, i + 2 * step)) { lo = i + 2 * step; step *= 2; }
      let hi = Math.min(n - 1, i + 2 * step);
      while (lo < hi) { const mid = (lo + hi + 1) >> 1; if (okCut(i, mid)) lo = mid; else hi = mid - 1; }
      const j = Math.max(lo, i + 1);
      out.push(coords[j]); i = j;
    }
    return out;
  }

  // -------------------------------------------------------------- sailing
  // Port of sailing.py: the planned legs sailed as a ship turns - straight
  // legs joined by circular arcs, one turn radius per waypoint (2.5 L where
  // the water allows; at least 1.5 L at sea, 1.0 L under pilotage with tugs
  // in the 3 NM next to a terminal). All metrics are taken on this track.
  const TURN = {radius_loa: 2.5, min_radius_loa: 1.5, tug_radius_loa: 1.0, pilotage_nm: 3.0, shrink: .85,
                straighten_m: 150, drift_deg: 5, drift_max_deg: 8, lag_loa: .4, step_m: 10,
                end_slack_nm: .25, gateway_free_nm: 2.0};
  const KYM = 110574;
  function frame(lat0, lon0) {
    const kx = 111320 * Math.cos(rad(lat0));
    return {lat0, lon0, kx, xy: pts => pts.map(([la, lo]) => [(lo - lon0) * kx, (la - lat0) * KYM]),
            ll: (x, y) => [lat0 + y / KYM, lon0 + x / kx]};
  }
  function offSeg(p, a, b) {
    const vx = b[0] - a[0], vy = b[1] - a[1], l2 = vx * vx + vy * vy || 1e-9;
    const t = clip(((p[0] - a[0]) * vx + (p[1] - a[1]) * vy) / l2, 0, 1);
    return Math.hypot(a[0] + vx * t - p[0], a[1] + vy * t - p[1]);
  }
  function straighten(net, legs) {
    if (legs.length < 3) return legs.slice();
    const fr = frame(legs[0][0], legs[0][1]), P = fr.xy(legs);
    const keep = [0];
    for (let i = 1; i < legs.length - 1; i++) {
      const a = keep[keep.length - 1], j = i + 1;
      let near = true;
      for (let m = a + 1; m < j; m++) if (offSeg(P[m], P[a], P[j]) >= TURN.straighten_m) { near = false; break; }
      if (near && segmentCost(net, legs[a], legs[j])[2]) continue;      // legs[i] dropped
      keep.push(i);
    }
    keep.push(legs.length - 1);
    return keep.map(k => legs[k]);
  }
  function legGeometry(P) {
    const u = [], L = [], th = [0];
    for (let i = 1; i < P.length; i++) {
      const dx = P[i][0] - P[i - 1][0], dy = P[i][1] - P[i - 1][1], d = Math.hypot(dx, dy) || 1e-9;
      u.push([dx / d, dy / d]); L.push(d);
    }
    for (let i = 1; i < P.length - 1; i++) {
      const a1 = Math.atan2(u[i - 1][0], u[i - 1][1]), a2 = Math.atan2(u[i][0], u[i][1]);
      th.push(Math.atan2(Math.sin(a2 - a1), Math.cos(a2 - a1)));        // + = to starboard
    }
    th.push(0);
    return {u, L, th};
  }
  function fitRadii(L, th, want) {
    const n = th.length, tan = th.map(t => Math.tan(Math.abs(t) / 2));
    const t = want.map((w, i) => w * tan[i]);
    for (let r = 0; r < 3; r++)
      for (let j = 0; j < n - 1; j++) {
        const need = t[j] + t[j + 1];
        if (need > L[j]) { const f = L[j] / need; t[j] *= f; t[j + 1] *= f; }
      }
    return t.map((x, i) => tan[i] > 1e-9 ? x / tan[i] : want[i]);
  }
  function arcPts(P, u, th, i, R, ds) {
    const t = R * Math.tan(Math.abs(th[i]) / 2);
    const ax = P[i][0] - u[i - 1][0] * t, ay = P[i][1] - u[i - 1][1] * t, sgn = th[i] > 0 ? 1 : -1;
    const cx = ax + u[i - 1][1] * sgn * R, cy = ay - u[i - 1][0] * sgn * R;
    const a0 = Math.atan2(ax - cx, ay - cy), c0 = Math.atan2(u[i - 1][0], u[i - 1][1]);
    const n = Math.max(2, Math.ceil(Math.abs(th[i]) * R / ds) + 1), out = [];
    for (let k = 0; k < n; k++) {
      const f = k / (n - 1), a = a0 + th[i] * f;
      out.push([cx + R * Math.sin(a), cy + R * Math.cos(a), c0 + th[i] * f]);
    }
    return out;
  }
  function toleranceMask(net, legs) {
    const g = net.g, m = new Uint8Array(g.n);
    densify(legs, .25 * Math.min(g.dyNm, g.dxNm)).forEach(p => { const [y, x] = g.toIndex(p[0], p[1]); m[y * g.nx + x] = 1; });
    return m;
  }
  function arcOk(net, fr, pts, tol) {
    const g = net.g, cIn = pts[0][2], cOut = pts[pts.length - 1][2];
    const rule = (k, c) => tssPenalty(g.zone[k], g.zone[k], g.head[k], g.head[k], (deg(c) + 360) % 360, net.cfg)[1];
    for (const [x, y, c] of pts) {
      const [la, lo] = fr.ll(x, y), [iy, ix] = g.toIndex(la, lo), k = iy * g.nx + ix;
      if (!net.nav[k] && !tol[k]) return false;
      // Rule 10 only where the planned legs themselves obey it there
      if (!rule(k, c) && rule(k, cIn) && rule(k, cOut)) return false;
    }
    return true;
  }
  function sailLegs(net, legsIn, pilotage = [0, 0], quay = [null, null]) {
    // pilotage: pilotage-zone length (NM) at each end, 0 = strait gateway
    const Ls = net.vessel.loa_m;
    const legs = straighten(net, legsIn);
    const fr = frame(legs[0][0], legs[0][1]), P = fr.xy(legs);
    const {u, L, th} = legGeometry(P), n = P.length;
    const cum = [0]; L.forEach(d => cum.push(cum[cum.length - 1] + d));
    const total = cum[n - 1], tol = toleranceMask(net, legs);
    const pz = TURN.pilotage_nm * M_PER_NM, slack = TURN.end_slack_nm * M_PER_NM, gate = TURN.gateway_free_nm * M_PER_NM;
    const floor = cum.map(c => ((pilotage[0] && c <= pilotage[0] * M_PER_NM) || (pilotage[1] && total - c <= pilotage[1] * M_PER_NM) ? TURN.tug_radius_loa : TURN.min_radius_loa) * Ls);
    const Rp = TURN.radius_loa * Ls;
    const R = fitRadii(L, th, new Array(n).fill(Rp));
    const tooTight = [];
    for (let i = 1; i < n - 1; i++) {
      if (Math.abs(th[i]) < 1e-4) continue;
      let r = R[i];
      const exempt = cum[i] < slack || total - cum[i] < slack;
      while (!exempt && r >= 5 && !arcOk(net, fr, arcPts(P, u, th, i, r, TURN.step_m), tol)) {
        const nxt = r * TURN.shrink;
        r = nxt < floor[i] && floor[i] < r ? floor[i] : nxt;      // step onto the limit before going under it
      }
      R[i] = r;
      const atGate = (!pilotage[0] && cum[i] < gate) || (!pilotage[1] && total - cum[i] < gate);
      if (r < floor[i] - 1e-6 && !atGate) tooTight.push(i);
    }
    // straight runs joined by arcs, every step_m
    const ds = TURN.step_m;
    const tIn = th.map((t, i) => i > 0 && i < n - 1 && Math.abs(t) >= 1e-4 ? R[i] * Math.tan(Math.abs(t) / 2) : 0);
    const xs = [], ys = [], chi = [], kap = [];
    for (let j = 0; j < n - 1; j++) {
      const s0 = tIn[j], s1 = L[j] - tIn[j + 1], c = Math.atan2(u[j][0], u[j][1]);
      const m = Math.max(1, Math.ceil(Math.max(s1 - s0, 0) / ds));
      for (let k = 0; k < m; k++) {
        const s = s0 + (s1 - s0) * k / m;
        xs.push(P[j][0] + u[j][0] * s); ys.push(P[j][1] + u[j][1] * s); chi.push(c); kap.push(0);
      }
      const i = j + 1;
      if (i < n - 1 && tIn[i] > 0) {
        const kk = (th[i] > 0 ? 1 : -1) / R[i], pts = arcPts(P, u, th, i, R[i], ds);
        for (let q = 0; q < pts.length - 1; q++) { xs.push(pts[q][0]); ys.push(pts[q][1]); chi.push(pts[q][2]); kap.push(kk); }
      }
    }
    xs.push(P[n - 1][0]); ys.push(P[n - 1][1]); chi.push(Math.atan2(u[n - 2][0], u[n - 2][1])); kap.push(0);
    const N = xs.length, d = new Float64Array(N), kf = new Float64Array(N), psi = new Float64Array(N);
    const lat = new Float64Array(N), lon = new Float64Array(N);
    const lag = TURN.lag_loa * Ls, dmax = rad(TURN.drift_max_deg);
    for (let k = 0; k < N; k++) {
      if (k) {
        d[k] = d[k - 1] + Math.hypot(xs[k] - xs[k - 1], ys[k] - ys[k - 1]);
        kf[k] = kf[k - 1] + (kap[k] - kf[k - 1]) * Math.min(1, (d[k] - d[k - 1]) / lag);
      }
      // the ship's head: course plus drift (bow inside the turn), rudder lag on the rate of turn
      psi[k] = chi[k] + clip(rad(TURN.drift_deg) * Rp * kf[k], -dmax, dmax);
      [lat[k], lon[k]] = fr.ll(xs[k], ys[k]);
    }
    // alongside: the tugs swing the ship parallel to the quay over the last
    // (first) few hundred metres, bow the way she was heading
    const swingM = Math.min(TURN.end_slack_nm * M_PER_NM, 2 * Ls);
    [0, 1].forEach(end => {
      const q = quay[end];
      if (q == null || !isFinite(q)) return;
      const cEnd = psi[end ? N - 1 : 0], qa = rad(q);
      const wrap = a => Math.atan2(Math.sin(a), Math.cos(a));
      const tgt = [qa, qa + Math.PI].reduce((b, a) => Math.abs(wrap(a - cEnd)) < Math.abs(wrap(b - cEnd)) ? a : b);
      for (let k = 0; k < N; k++) {
        const dEnd = end ? d[N - 1] - d[k] : d[k];
        const w = Math.max(0, Math.min(1, 1 - dEnd / swingM)) ** 2;
        if (w > 0) psi[k] += w * wrap(tgt - psi[k]);
      }
    });
    // turning figures and the largest offset from the planned legs
    let xt = 0;
    for (let k = 0; k < N; k++) {
      let b = Infinity;
      for (let i = 1; i < n; i++) b = Math.min(b, offSeg([xs[k], ys[k]], P[i - 1], P[i]));
      if (b > xt) xt = b;
    }
    let rmin = Infinity;
    for (let i = 1; i < n - 1; i++) if (Math.abs(th[i]) >= 1e-4) rmin = Math.min(rmin, R[i]);
    const mPerMin = 12 * M_PER_NM / 60;
    return {legs, radius: R, turn: th, tooTight, d, lat, lon, psi, kf, length_m: d[N - 1],
            summary: {waypoints: n, min_turn_radius_m: isFinite(rmin) ? rmin : null,
                      max_rot_deg_min: isFinite(rmin) ? deg(1 / rmin) * mPerMin : 0,
                      max_xtrack_m: xt, tight_turns: tooTight.length}};
  }

  // -------------------------------------------------------------- metrics
  function densify(coords, step) {
    const out = [];
    for (let i = 0; i < coords.length - 1; i++) {
      const a = coords[i], b = coords[i + 1];
      const n = Math.max(1, Math.ceil(hav(a[0], a[1], b[0], b[1]) / step));
      for (let k = 0; k < n; k++) out.push([a[0] + (b[0] - a[0]) * k / n, a[1] + (b[1] - a[1]) * k / n]);
    }
    out.push(coords[coords.length - 1]);
    return out;
  }
  function turnAngles(c) {
    const out = [];
    for (let i = 1; i < c.length - 1; i++) {
      const b1 = initialBearing(c[i - 1][0], c[i - 1][1], c[i][0], c[i][1]);
      const b2 = initialBearing(c[i][0], c[i][1], c[i + 1][0], c[i + 1][1]);
      out.push(Math.abs(((b1 - b2 + 180) % 360 + 360) % 360 - 180));
    }
    return out;
  }

  function trackMetrics(net, coords, ships, swing, maxList = 12, berthing = [0, 0]) {
    const g = net.g, v = net.vessel;
    let dist = 0;
    for (let i = 1; i < coords.length; i++) dist += hav(coords[i - 1][0], coords[i - 1][1], coords[i][0], coords[i][1]);
    const pts = coords.length > 1 ? densify(coords, .02) : coords;
    const ks = pts.map(p => { const [y, x] = g.toIndex(p[0], p[1]); return y * g.nx + x; });
    const seg = [], cum = [0];
    for (let i = 1; i < pts.length; i++) { const s = hav(pts[i - 1][0], pts[i - 1][1], pts[i][0], pts[i][1]); seg.push(s); cum.push(cum[i - 1] + s); }
    let minShore = Infinity, minDepth = Infinity, shallow = 0, onLand = 0;
    // the pilotage legs to the berths are checked against the real coastline
    // when built; the board's land / depth / shore checks cover the passage
    const total = cum[cum.length - 1];
    ks.forEach((k, i) => {
      if (cum[i] < berthing[0] || cum[i] > total - berthing[1]) return;
      minShore = Math.min(minShore, g.dist[k]); minDepth = Math.min(minDepth, g.depth[k]);
      if (g.land[k]) onLand++;
      if (i < seg.length && g.depth[k] < net.reqD[k]) shallow += seg[i];
    });
    const sl = hav(coords[0][0], coords[0][1], coords[coords.length - 1][0], coords[coords.length - 1][1]);
    const ta = turnAngles(coords);
    const m = {distance_nm: dist, passage_h: dist / 12, legs: coords.length - 1,
               max_turn_deg: ta.length ? Math.max(...ta) : 0, min_shore_nm: minShore,
               min_depth_m: minDepth, min_ukc_m: minDepth - v.draught_m, shallow_nm: shallow,
               on_land: onLand, straight_line_nm: sl, detour_pct: sl > 0 ? 100 * (dist / sl - 1) : 0};
    const enc = [];
    if (ships && ships.length) {
      const hb = net.halfBeam, N = ships.length;
      const env = ships.map(s => envelope(s, swing));
      const cpaEnv = new Float64Array(N).fill(Infinity), cpaHull = new Float64Array(N).fill(Infinity);
      const kMin = new Int32Array(N);
      const insideAny = new Uint8Array(pts.length);
      pts.forEach((p, i) => {
        const k = ks[i];
        for (let j = 0; j < N; j++) {
          const e = env[j];
          if (Math.abs(p[0] - e[0]) > .06 && cpaEnv[j] < 3) continue;   // far: cannot be the minimum
          const de = hav(p[0], p[1], e[0], e[1]) - e[2] - hb;
          if (de < cpaEnv[j]) { cpaEnv[j] = de; kMin[j] = i; }
          if (de < net.target[k]) insideAny[i] = 1;
          const dh = hav(p[0], p[1], ships[j].lat, ships[j].lon) - ships[j].loa_m / 2 / M_PER_NM - hb;
          if (dh < cpaHull[j]) cpaHull[j] = dh;
        }
      });
      let met = 0, breaches = 0, inside = 0;
      const list = [];
      for (let j = 0; j < N; j++) {
        const k = ks[kMin[j]], t = net.target[k], h = net.hard[k];
        if (cpaEnv[j] < h) breaches++;
        if (cpaEnv[j] < t) {
          met++;
          const s = ships[j];
          list.push({id: s.id, name: s.name, ship_type: s.ship_type, status: s.status, loa_m: s.loa_m,
                     area: s.area || "", cpa_nm: cpaEnv[j], hull_nm: cpaHull[j], target_nm: t, hard_nm: h,
                     at: pts[kMin[j]], along_nm: cum[kMin[j]], ship: [s.lat, s.lon]});
        }
      }
      for (let i = 0; i < seg.length; i++) if (insideAny[i]) inside += seg[i];
      list.sort((a, b) => a.cpa_nm - b.cpa_nm);
      enc.push(...list.slice(0, maxList));
      Object.assign(m, {obstacles_met: met, hard_breaches: breaches,
        min_ship_clear_nm: Math.min(...cpaEnv), min_hull_dist_nm: Math.min(...cpaHull),
        ships_considered: N, nm_inside_cpa: inside});
    } else Object.assign(m, {obstacles_met: 0, hard_breaches: 0, min_ship_clear_nm: NaN,
                              min_hull_dist_nm: NaN, ships_considered: 0, nm_inside_cpa: 0});
    m.feasible = m.on_land === 0 && m.shallow_nm < 1e-6 && m.hard_breaches === 0;
    return [m, enc];
  }

  // ------------------------------------------------------------- planning
  function pointOf(data, x) {
    if (typeof x === "string") { const p = data.meta.ports.find(q => q.code === x); return [p.lat, p.lon]; }
    return x;
  }
  function labelOf(data, x) {
    if (typeof x === "string") return data.meta.ports.find(q => q.code === x).name;
    return `${x[0].toFixed(4)} N, ${x[1].toFixed(4)} E`;
  }
  const BOARD_LABELS = {0: "free water", 1: "land", 2: "too shallow", 3: "too close to shore",
                        4: "ship exclusion", 5: "inside a ship's target CPA"};
  function endpointCheck(net, x) {
    const g = net.g, [la, lo] = pointOf(net.data, x);
    if (!g.inside(la, lo)) return `${labelOf(net.data, x)} is outside the study area`;
    const s = nearestNavigable(g, la, lo, net.nav);
    if (!s) return `no navigable water near ${labelOf(net.data, x)}`;
    const snap = hav(la, lo, g.lats[s[0]], g.lons[s[1]]);
    if (snap > .6) {
      const [cy, cx] = g.toIndex(la, lo), k = cy * g.nx + cx;
      const why = BOARD_LABELS[net.board[k]] || "blocked";
      const extra = why === "too shallow" ? ` (charted ${g.depth[k].toFixed(1)} m, ship needs ${net.reqD[k].toFixed(1)} m incl. UKC)` : "";
      return `${labelOf(net.data, x)} is not reachable for this ship (${net.vessel.name}): ${why}${extra}; nearest usable water is ${snap.toFixed(1)} NM away`;
    }
    return null;
  }
  function nodeAt(net, la, lo) { const s = nearestNavigable(net.g, la, lo, net.nav); return s[0] * net.g.nx + s[1]; }
  function coordsOf(net, path) { const nx = net.g.nx; return path.map(k => [net.g.lats[(k / nx) | 0], net.g.lons[k % nx]]); }

  function planRoute(net, origin, dest, ships, swing, algorithm = "astar", maxPushes = 5) {
    // search -> string-pull -> sail (turn radii) -> measure the sailed track.
    // A waypoint the ship cannot turn at pushes the route off the obstacle:
    // the water round it is closed and the search runs again.
    const g = net.g, a = pointOf(net.data, origin), b = pointOf(net.data, dest);
    const src = nodeAt(net, a[0], a[1]), dst = nodeAt(net, b[0], b[1]);
    const terminals = (net.data.meta.terminals || ["SGTUA", "SGJUR", "SGPPT", "SGKEP"]);
    const portOf = x => typeof x === "string" ? net.data.meta.ports.find(q => q.code === x) : null;
    const pilotNm = x => { const p = portOf(x); if (!p || !p.pilot || p.pilot.length < 2) return 0;
      let s = 0; for (let i = 1; i < p.pilot.length; i++) s += hav(p.pilot[i - 1][0], p.pilot[i - 1][1], p.pilot[i][0], p.pilot[i][1]); return s; };
    // join the route to the berths: the search ends where the board can reach
    // (the start of the pilotage track); the track takes the ship to the quay
    const withBerths = legs => {
      let L = legs.map(c => c.slice());
      const po = portOf(origin), pd = portOf(dest);
      if (po && po.pilot && po.pilot.length > 1) L = po.pilot.slice().reverse().map(c => c.slice()).concat(L.slice(1));
      if (pd && pd.pilot && pd.pilot.length > 1) L = L.slice(0, -1).concat(pd.pilot.map(c => c.slice()));
      return L;
    };
    // pilotage zone (NM) at each terminal end: from the MPA boarding ground the
    // route passes within 3 NM of, and at least the pilotage track + 3 NM
    const pilotZone = legs => {
      const pts = densify(legs, .05), cum = [0];
      for (let i = 1; i < pts.length; i++) cum.push(cum[i - 1] + hav(pts[i - 1][0], pts[i - 1][1], pts[i][0], pts[i][1]));
      const bg = net.data.meta.pilot_boarding || [];
      const near = pts.map(p => bg.some(b => hav(p[0], p[1], b.lat, b.lon) <= 3));
      const tot = cum[cum.length - 1];
      return [origin, dest].map((x, end) => {
        if (!(typeof x === "string" && terminals.includes(x))) return 0;
        let base = pilotNm(x) + 3;
        const idx = near.map((v, i) => v ? i : -1).filter(i => i >= 0);
        if (idx.length) {
          const k = end ? Math.max(...idx) : Math.min(...idx);
          const reach = end ? tot - cum[k] : cum[k];
          if (reach <= tot / 2) base = Math.max(base, reach);
        }
        return base;
      });
    };
    const quays = [origin, dest].map(x => { const p = portOf(x); return p && p.kind === "terminal" ? p.quay_hdg : null; });
    const berthing = [origin, dest].map(x => pilotNm(x) ? pilotNm(x) + .1 : 0);
    const blocked = new Uint8Array(g.n), keep = new Uint8Array(g.n);
    for (const k of [src, dst]) {
      const y = (k / g.nx) | 0, x = k % g.nx;
      for (let dy = -2; dy <= 2; dy++) for (let dx = -2; dx <= 2; dx++) {
        const yy = y + dy, xx = x + dx;
        if (yy >= 0 && yy < g.ny && xx >= 0 && xx < g.nx) keep[yy * g.nx + xx] = 1;
      }
    }
    let best = null, anyBlocked = false, rad = 1, lastBlock = null, lastTr = null;
    const closeCells = (tr, rr) => {
      for (const i of tr.tooTight) {
        const [y, x] = g.toIndex(tr.legs[i][0], tr.legs[i][1]);
        for (let dy = -rr; dy <= rr; dy++) for (let dx = -rr; dx <= rr; dx++) {
          const yy = y + dy, xx = x + dx;
          if (yy >= 0 && yy < g.ny && xx >= 0 && xx < g.nx && !keep[yy * g.nx + xx]) { blocked[yy * g.nx + xx] = 1; anyBlocked = true; }
        }
      }
    };
    for (let push = 0; push <= maxPushes; push++) {
      let netA = net;
      if (anyBlocked) {
        const nav = new Uint8Array(g.n);
        for (let k = 0; k < g.n; k++) nav[k] = net.nav[k] && !blocked[k] ? 1 : 0;
        // as in routing.py: closed cells cannot be entered; the lattice's
        // corner-cutting rules still use the unmodified board
        netA = Object.assign({}, net, {nav, knightNav: net.nav});
      }
      const r = search(netA, src, dst, algorithm !== "dijkstra");
      if (!r.found) {
        if (!lastBlock || rad === 0) break;            // closed too much water: keep the best so far
        // as in routing.py: the 3x3 block cut the way through - reopen it and
        // close only the waypoint's own cell
        blocked.set(lastBlock); rad = 0; closeCells(lastTr, 0); lastBlock = null;
        continue;
      }
      const raw = coordsOf(net, r.path), sm = withBerths(smooth(netA, raw));
      const tr = sailLegs(net, sm, pilotZone(sm), quays);
      if (!best || tr.tooTight.length < best.tr.tooTight.length) best = {r, raw, tr, push};
      if (!tr.tooTight.length) break;
      lastBlock = blocked.slice(); lastTr = tr;
      closeCells(tr, rad ? rad + push : 0);      // wider each time: a narrow block can just slide the kink
    }
    if (!best) return null;
    const {r, raw, tr, push} = best;
    const coords = [];
    for (let k = 0; k < tr.lat.length; k += 3) coords.push([tr.lat[k], tr.lon[k]]);
    const last = tr.lat.length - 1;
    if (last % 3) coords.push([tr.lat[last], tr.lon[last]]);
    const [m, enc] = trackMetrics(net, coords, ships, swing, 12, berthing);
    Object.assign(m, tr.summary);
    m.berthing_nm = berthing[0] + berthing[1];
    const ta = turnAngles(tr.legs);
    m.max_turn_deg = ta.length ? Math.max(...ta) : 0;
    m.legs = tr.legs.length - 1;
    m.turn_ok = tr.tooTight.length === 0;
    m.route_pushes = push;
    m.feasible = m.feasible && m.turn_ok;
    let gd = 0; for (let i = 1; i < raw.length; i++) gd += hav(raw[i - 1][0], raw[i - 1][1], raw[i][0], raw[i][1]);
    let pd = 0; for (let i = 1; i < tr.legs.length; i++) pd += hav(tr.legs[i - 1][0], tr.legs[i - 1][1], tr.legs[i][0], tr.legs[i][1]);
    m.grid_distance_nm = gd; m.planned_distance_nm = pd; m.cost_effective_nm = r.cost;
    const radii = tr.legs.map((_, i) => i > 0 && i < tr.legs.length - 1 && Math.abs(tr.turn[i]) >= 1e-4 ? tr.radius[i] : null);
    return {label: net.cfg.mode, mode: net.cfg.mode, metrics: m, coords, encounters: enc,
            waypoints: tr.legs, turn_radii: radii,
            track: {d: tr.d, lat: tr.lat, lon: tr.lon, psi: tr.psi, kf: tr.kf, length_m: tr.length_m},
            search: Object.assign({}, r, {path: undefined})};
  }

  // --------------------------------------------------------- CSV snapshots
  const AIS_TYPE = c => { c = +c; if (c >= 70 && c <= 79) return "cargo"; if (c >= 80 && c <= 89) return "tanker";
    if (c >= 60 && c <= 69) return "passenger"; if (c >= 30 && c <= 59) return "service"; return "other"; };
  const DIMS = {container: [260, 32, 12], tanker: [183, 32, 11], bulker: [190, 32, 11], cargo: [160, 25, 9],
                gas: [230, 36, 11], passenger: [120, 20, 5], service: [40, 12, 4], other: [100, 18, 6]};
  const ALIAS = {mmsi: ["mmsi"], time: ["receivetime", "timestamp", "time", "basedatetime", "time_utc", "timestamp_utc", "datetime"],
    lat: ["lat", "latitude"], lon: ["lon", "lng", "long", "longitude"], sog: ["sog", "sog_kn", "speed"],
    cog: ["cog", "cog_deg"], heading: ["heading", "heading_deg", "trueheading"],
    nav: ["navstatus", "nav_status", "navigationalstatus"], status: ["status"], type: ["ship_type", "shiptype", "vesseltype", "type"],
    aistype: ["ais_type"], length: ["loa_m", "length", "loa"], width: ["beam_m", "width", "beam"],
    draught: ["draught_m", "draught", "draft"], name: ["name", "vesselname", "shipname"], id: ["id"], area: ["area"]};
  function parseCsv(text) {
    const rows = [], lines = text.replace(/\r/g, "").split("\n").filter(l => l.trim());
    const split = l => { const out = []; let cur = "", q = false;
      for (const ch of l) { if (ch === '"') q = !q; else if (ch === "," && !q) { out.push(cur); cur = ""; } else cur += ch; }
      out.push(cur); return out; };
    const head = split(lines[0]).map(h => h.trim().toLowerCase().replace(/\s/g, ""));
    for (let i = 1; i < lines.length; i++) { const v = split(lines[i]); const o = {}; head.forEach((h, j) => o[h] = v[j]); rows.push(o); }
    return {head, rows};
  }
  function shipsFromCsv(g, text) {
    const {head, rows} = parseCsv(text);
    const col = k => ALIAS[k].find(a => head.includes(a));
    const c = {}; for (const k in ALIAS) c[k] = col(k);
    if (!c.lat || !c.lon) throw new Error("the file needs latitude and longitude columns");
    // last report per vessel
    let recs = rows;
    if (c.mmsi) {
      if (c.time) recs = recs.slice().sort((a, b) => String(a[c.time]).localeCompare(String(b[c.time])));
      const last = new Map(); recs.forEach(r => last.set(r[c.mmsi] || Math.random(), r)); recs = [...last.values()];
    }
    const out = []; let moving = 0;
    recs.forEach((r, i) => {
      const lat = +r[c.lat], lon = +r[c.lon];
      if (!isFinite(lat) || !isFinite(lon) || !g.inside(lat, lon)) return;
      const sog = c.sog ? +r[c.sog] || 0 : 0, ns = c.nav && r[c.nav] !== "" ? +r[c.nav] : null;
      let status = c.status && r[c.status] ? r[c.status].toLowerCase() : null;
      if (!status) status = ns === 1 ? "anchored" : ns === 5 ? "moored" : ns === 6 ? "aground" : "stationary";
      if (!(ns === 1 || ns === 5 || ns === 6 || sog <= .5 || (c.status && r[c.status]))) { moving++; return; }
      let type = c.type && r[c.type] ? String(r[c.type]).toLowerCase() : "";
      if (/^\d+$/.test(type)) type = AIS_TYPE(type);
      if (!type && c.aistype) type = AIS_TYPE(r[c.aistype]);
      if (!DIMS[type]) type = "other";
      const [dl, db, dd] = DIMS[type];
      let hdg = c.heading ? +r[c.heading] : NaN;
      if (!isFinite(hdg) || hdg >= 360) hdg = c.cog ? +r[c.cog] || 0 : 0;
      if (hdg >= 360) hdg = 0;
      const [iy, ix] = g.toIndex(lat, lon);
      out.push({id: (c.id && r[c.id]) || (c.mmsi && r[c.mmsi]) || `ship-${i}`, mmsi: c.mmsi ? +r[c.mmsi] || null : null,
        name: c.name ? (r[c.name] || "").trim() : "", ship_type: type, status, lat, lon, sog_kn: sog,
        heading_deg: hdg, loa_m: (c.length && +r[c.length]) || dl, beam_m: (c.width && +r[c.width]) || db,
        draught_m: (c.draught && +r[c.draught]) || dd, area: c.area ? r[c.area] || "" : "",
        depth_hint_m: clip(g.depth[iy * g.nx + ix], 8, 40), source: "ais"});
    });
    return {ships: out, moving};
  }

  const api = {edgeCost, makeGrid, obstacleField, buildNet, search, planRoute, trackMetrics, smooth, sailLegs, TURN, envelope,
               endpointCheck, nodeAt, shipsFromCsv, hav, initialBearing, MODES, BOARD_LABELS};
  root.SafeRouteEngine = api;
  if (typeof module !== "undefined") module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);
