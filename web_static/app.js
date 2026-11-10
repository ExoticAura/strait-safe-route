/* Strait Safe Route - front end, running fully in the browser (engine.js).
 *
 * Asks the local server for the shortest, balanced and safest routes between
 * a strait entrance (or any point picked on the chart) and a Singapore
 * terminal, around every stationary ship in the chosen snapshot, and shows how
 * close each route comes to those ships - on the chart and in 3D.
 */
"use strict";

const $ = (s, r) => (r || document).querySelector(s);
const $$ = (s, r) => [...(r || document).querySelectorAll(s)];
const css = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const MODE_VARS = {shortest: "--rt-c", balanced: "--rt-b", safest: "--rt-a"};
const MODE_NAMES = {shortest: "Shortest", balanced: "Balanced", safest: "Safest"};
const NM = 1852;

const S = {
  meta: null, ships: null, result: null, sel: 0, board: null, busy: false, pending: false,
  frac: 0.35, ignored: new Set(), playing: false, speed: 60, swing: true, cpaScale: 1.0, picking: null,
  custom: {origin: null, dest: null}, wantMode: "safest", tod: "day", sea: "moderate",
};

const fmt = (v, d = 1) => (v == null || !isFinite(v)) ? "—"
  : Number(v).toLocaleString("en-SG", {minimumFractionDigits: d, maximumFractionDigits: d});
const esc = s => String(s == null ? "" : s).replace(/&/g, "&amp;").replace(/</g, "&lt;");
function dur(h) {
  if (!isFinite(h)) return "—";
  const hh = Math.floor(h), m = Math.round((h - hh) * 60);
  return `${hh}h ${String(m).padStart(2, "0")}m`;
}
function dm(v, hemi) {
  const h = v < 0 ? hemi[1] : hemi[0], a = Math.abs(v), d = Math.floor(a), m = (a - d) * 60;
  return `${d}°${m.toFixed(2).padStart(5, "0")}'${h}`;
}
const nmTxt = v => v == null || !isFinite(v) ? "—" : `${fmt(v, 2)} NM · ${fmt(v * NM, 0)} m`;

/* =====================================================================
   chart
   ===================================================================== */
const LAYER_IDS = {ships: "L-ships", env: "L-env", board: "L-board", depth: "L-depth",
                   tss: "L-tss", anch: "L-anch", alt: "L-alt",
                   mdepth: "L-mdepth", mhaz: "L-mhaz", mtrk: "L-mtrk", mberth: "L-mberth"};
const MPA_LAYERS = ["L-mdepth", "L-mhaz", "L-mtrk", "L-mberth"];
function readLayers() {
  const o = {};
  for (const [k, id] of Object.entries(LAYER_IDS)) o[k] = !!$("#" + id).checked;
  return o;
}
// the chosen base map is remembered in this browser; Chart (the model's own
// coastline) needs no internet, the others load map tiles
const BASEMAP_CHOICES = ["hybrid", "streets", "nautical", "simple"];
function savedBasemap() {
  let b = null;
  try { b = localStorage.getItem("sr-basemap"); } catch (e) {}
  return BASEMAP_CHOICES.includes(b) ? b : "streets";
}

const CM = ChartMap.create($("#chart"), {
  basemap: savedBasemap(),
  cursorEl: $("#cursor"), scaleLine: $("#scaleline"), scaleText: $("#scaletext"),
  ignoredTypes: () => [...S.ignored],
  onRouteClick: i => { S.sel = i; S.wantMode = S.result.plans[i].mode; renderSide(); CM.setSelected(i); updateOwn(); },
  onOwnClick: () => openShipView(true),
  onEncounterClick: e => goToEncounter(e),
  onShipClick: s => { if (S.picking) return; },
  onMapClick: ll => pickPoint(ll),
  berthLookup: name => BERTH_BY_NAME.get(name),
  onBerthClick: (code, id) => sailToBerth(code, id),
  onBasemapFail: name => {
    syncBasemapUI();
    $("#gridinfo").textContent = `${name} map tiles could not load here (this page may block outside map servers; they load on the Railway site) — showing the model's own coastline`;
    $("#gridinfo").classList.add("warn");
  },
});
const isDark = () => document.documentElement.getAttribute("data-theme") === "dark" ||
  (!document.documentElement.hasAttribute("data-theme") && matchMedia("(prefers-color-scheme: dark)").matches);
CM.setTheme(isDark());

function syncBasemapUI() { $$("#basemap input").forEach(i => { i.checked = i.value === CM.basemap; }); }
$$("#basemap input").forEach(i => i.onchange = () => {
  if (!i.checked) return;
  $("#gridinfo").classList.remove("warn");
  CM.setBasemap(i.value);
  try { localStorage.setItem("sr-basemap", i.value); } catch (e) {}
});
syncBasemapUI();

Object.values(LAYER_IDS).forEach(id => $("#" + id).onchange = () => {
  CM.setLayers(readLayers());
  if (id === "L-board") { $("#boardlegend").hidden = !$("#L-board").checked; if ($("#L-board").checked) loadBoard(); }
  if (MPA_LAYERS.includes(id)) { loadMpa(); depthKey(); }
});
CM.setLayers(readLayers());
CM.setBasemap(savedBasemap()); syncBasemapUI();
$("#zin").onclick = () => CM.zoomIn();
$("#zout").onclick = () => CM.zoomOut();
$("#zfit").onclick = () => CM.fitResult();

function boardLegend() {
  const labels = (S.meta && S.meta.board_labels) || {};
  $("#boardlegend").innerHTML = "<b style='font-size:10px'>Router&rsquo;s board</b>" + Object.entries(labels)
    .filter(([k]) => +k >= 2).map(([k, v]) => {
      const c = CM.BOARD_COLOURS[k];
      return `<span><i style="background:rgba(${c[0]},${c[1]},${c[2]},${Math.min(1, c[3] + .2)})"></i>${esc(v)}</span>`;
    }).join("") + "<span><i style='background:transparent'></i>clear: free water</span>";
}

/* =====================================================================
   picking a point on the chart
   ===================================================================== */
function pickPoint(ll) {
  if (!S.picking) return;
  const b = S.meta.bbox;
  if (ll.lat < b.lat_min || ll.lat > b.lat_max || ll.lng < b.lon_min || ll.lng > b.lon_max) return;
  const which = S.picking;
  S.custom[which] = [ll.lat, ll.lng];
  S.picking = null; $("#pickhint").hidden = true;
  fillPointSelects();
  plan();
}
function fillPointSelects() {
  for (const which of ["origin", "dest"]) {
    const sel = $("#" + which), cur = sel.value;
    const opts = S.meta.ports.map(p => `<option value="${p.code}">${esc(p.name)}</option>`);
    const c = S.custom[which];
    if (c) opts.push(`<option value="${c[0].toFixed(5)},${c[1].toFixed(5)}">Chart point ${c[0].toFixed(4)}, ${c[1].toFixed(4)}</option>`);
    opts.push(`<option value="__pick">Pick on chart…</option>`);
    sel.innerHTML = opts.join("");
    if (c && (cur === "__pick" || cur.includes(","))) sel.value = `${c[0].toFixed(5)},${c[1].toFixed(5)}`;
    else if ([...sel.options].some(o => o.value === cur)) sel.value = cur;
  }
}
["origin", "dest"].forEach(id => $("#" + id).addEventListener("change", e => {
  fillBerthSelect(id); berthInfo();
  if (e.target.value === "__pick") {
    S.picking = id;
    $("#pickhint").textContent = `Click the chart to set the ${id === "origin" ? "start" : "end"} point`;
    $("#pickhint").hidden = false;
    return;
  }
  plan();
}));
$("#swap").onclick = () => {
  const o = $("#origin").value, d = $("#dest").value;
  [S.custom.origin, S.custom.dest] = [S.custom.dest, S.custom.origin];
  fillPointSelects();
  $("#origin").value = d; $("#dest").value = o;
  fillBerthSelect("origin"); fillBerthSelect("dest"); berthInfo();
  plan();
};

/* =====================================================================
   own ship along the route, nearest ships, 3D view
   ===================================================================== */
let SHIP = null, shipLoading = null;
function selectedPlan() { const r = S.result; return r && r.found ? (r.plans[S.sel] || r.plans[0]) : null; }
function vesselObj() { return (S.meta.vessels || []).find(v => v.code === $("#vessel").value) || S.meta.vessels[0]; }

function nearestShips(lat, lon, n = 6) {
  if (!S.ships) return [];
  const v = vesselObj(), halfBeam = v.beam_m / 2 / NM;
  return S.ships.ships.map(s => {
    const c = CM.hav([lat, lon], ENG.hullCentre(s));
    const hull = c - s.loa_m / 2 / NM - halfBeam;
    // the router keeps clear of each ship's swing circle (centred on its anchor),
    // so the chart measures to the nearest point on that circle, not to the hull
    const ce = CM.hav([lat, lon], [s.env[0], s.env[1]]), env = ce - s.env[2] - halfBeam;
    const k = ce > 1e-9 ? Math.min(1, s.env[2] / ce) : 0;
    const edge = [s.env[0] + (lat - s.env[0]) * k, s.env[1] + (lon - s.env[1]) * k];
    return {s, lat: s.lat, lon: s.lon, hull_nm: Math.max(hull, 0), centre_nm: c,
            env_nm: Math.max(env, 0), edge,
            brg: CM.bearing([lat, lon], [s.lat, s.lon]),
            cls: env < 0.2 ? "bad" : env < 0.5 ? "warn" : ""};
  }).filter(o => !S.ignored.has(o.s.ship_type)).sort((a, b) => a.env_nm - b.env_nm).slice(0, n);
}
/* ---- the ship along the route ----
   The engine returns the SAILED track (engine.js sailLegs): the planned legs
   joined by turning arcs, sampled every 10 m with the ship's head (course plus
   drift angle) and its rate of turn after rudder lag. The 3D view and the
   chart marker both sail exactly that track, which is also the track every
   safety number was measured on. */
function trackPoint(tr, f) {
  const target = Math.max(0, Math.min(1, f)) * tr.length_m, d = tr.d;
  let lo = 0, hi = d.length - 1;
  while (hi - lo > 1) { const m = (lo + hi) >> 1; if (d[m] <= target) lo = m; else hi = m; }
  const t = hi > lo ? Math.max(0, Math.min(1, (target - d[lo]) / ((d[hi] - d[lo]) || 1e-9))) : 0;
  const lerp = (a, b) => a + (b - a) * t;
  let dp = tr.psi[hi] - tr.psi[lo]; dp = Math.atan2(Math.sin(dp), Math.cos(dp));
  return {lat: lerp(tr.lat[lo], tr.lat[hi]), lon: lerp(tr.lon[lo], tr.lon[hi]),
          heading: ((tr.psi[lo] + dp * t) * 180 / Math.PI + 360) % 360,
          turn: lerp(tr.kf[lo], tr.kf[hi]),        // rate of turn per metre, + = to starboard
          nm: target / NM, total: tr.length_m / NM};
}
function ownState() {
  const p = selectedPlan();
  if (!p) return null;
  const vessel = vesselObj();
  const pt = p.track ? trackPoint(p.track, S.frac) : CM.pointAlong(p.coords, S.frac);
  return {p, pt, near: nearestShips(pt.lat, pt.lon, 14), vessel};
}
let lastMapPaint = 0;
function updateOwn(fromPlay) {
  const st = ownState();
  if (!st) { CM.setOwn(null); return; }
  const now = performance.now();
  // while playing, the chart and the side panel refresh a few times a second;
  // the 3D view gets every frame
  const paint = !fromPlay || now - lastMapPaint > 250;
  if (paint) {
    lastMapPaint = now;
    CM.setOwn({lat: st.pt.lat, lon: st.pt.lon, heading: st.pt.heading, vessel: st.vessel,
               nearest: st.near.slice(0, 3), label: `${st.vessel.name} · ${fmt(st.pt.nm, 2)} NM along`});
  }
  if (SHIP) {
    const d = new Date();
    // Singapore local time (UTC+8) for the fixed presets
    const TOD = {day: 2, dawn: 23, dusk: 11.17, night: 15};
    const utcHour = S.tod === "now" ? d.getUTCHours() + d.getUTCMinutes() / 60 : TOD[S.tod];
    SHIP.setParams({vessel: st.vessel, sog: S.meta.planning_speed_kn, heading: st.pt.heading, turn: st.pt.turn, sea: S.sea,
      lat: st.pt.lat, lon: st.pt.lon, utcHour, playing: !!fromPlay,
      nearest: st.near.filter(o => o.centre_nm < 3).map(o => ({id: o.s.id, hull_nm: o.hull_nm, cls: o.cls,
        centre_m: o.centre_nm * NM, loa_m: o.s.loa_m, ship_type: o.s.ship_type}))});
  }
  if (paint) paintOwnCard(st);
}

/* ---- play / pause: sail the selected route in time-lapse ---- */
let playRaf = 0, playLast = 0;
function setPlaying(on) {
  S.playing = on;
  ["#playbtn", "#modalplay"].forEach(id => { const b = $(id);
    if (b) { b.textContent = on ? "❚❚ Pause" : "▶ Play"; b.setAttribute("aria-pressed", String(on)); } });
  cancelAnimationFrame(playRaf);
  if (!on) { updateOwn(); return; }
  const p = selectedPlan(); if (!p) return;
  if (S.frac >= .999) S.frac = 0;
  playLast = performance.now();
  const step = t => {
    const dt = Math.min((t - playLast) / 1000, .25); playLast = t;
    const nmPerSec = S.meta.planning_speed_kn / 3600 * S.speed;
    S.frac = Math.min(1, S.frac + dt * nmPerSec / p.metrics.distance_nm);
    const sl = $("#shipfrac"); if (sl) sl.value = Math.round(S.frac * 1000);
    updateOwn(true);
    if (S.frac >= 1) { setPlaying(false); return; }
    playRaf = requestAnimationFrame(step);
  };
  playRaf = requestAnimationFrame(step);
}
function wireWorld() {
  if (!SHIP || !S.meta) return;
  SHIP.setWorld({land: S.meta.land, meta: WORLD, basePath: "", ships: S.ships ? S.ships.ships : undefined});
}
let WORLD = null;
fetch("world/world.json").then(r => r.ok ? r.json() : null).then(w => { WORLD = w; wireWorld(); }).catch(() => {});

function paintOwnCard(st) {
  const side = $("#side"), f = k => $(`[data-f="${k}"]`, side);
  const val = $("#shipfracval", side);
  if (!val || !st) return;
  val.textContent = `${fmt(st.pt.nm, 2)} of ${fmt(st.pt.total, 2)} NM`;
  f("shippos").textContent = dm(st.pt.lat, "NS") + " " + dm(st.pt.lon, "EW");
  f("shiphdg").textContent = Math.round(st.pt.heading) + "°";
  f("nearest").innerHTML = st.near.slice(0, 6).map(o =>
    `<div><span>${esc(o.s.name || o.s.id)} · ${esc(o.s.ship_type)} ${Math.round(o.s.loa_m)} m · ${Math.round(o.brg)}°</span>
      <span class="${o.cls}" title="to the swing circle / to the hull">${fmt(o.env_nm, 2)} / ${fmt(o.hull_nm, 2)} NM</span></div>`).join("")
    || `<div><span>no ships in the snapshot</span></div>`;
  f("shipnote").textContent = "First figure: your ship's side to the edge of the other ship's swing circle, which is what the router "
    + "keeps clear of (dashed lines on the chart). Second: hull to hull at the snapshot positions.";
}
function loadShip(el) {
  if (SHIP) { SHIP.attach(el); updateOwn(); return Promise.resolve(SHIP); }
  if (!shipLoading) shipLoading = import("./ship3d.js").then(m => {
    SHIP = m.createShipView(el);
    const ph = $(".ship3d-placeholder"); if (ph) ph.remove();
    updateOwn(); wireWorld(); updateOwn(); return SHIP;
  }).catch(err => {
    const ph = $(".ship3d-placeholder"); if (ph) ph.textContent = "3D view unavailable: " + err.message;
  });
  return shipLoading;
}
function openShipView(modal) {
  if (!modal) { const c = $('[data-f="shipcard"]'); if (c) c.scrollIntoView({behavior: "smooth", block: "nearest"}); return; }
  const m = $("#shipmodal"), st = ownState();
  m.hidden = false;
  if (st) $("#shipmodaltitle").textContent = `${st.vessel.name} — ${fmt(st.pt.nm, 2)} NM along the ${st.p.mode} route`;
  loadShip($("#ship3d-modal")).then(() => SHIP && SHIP.view("surroundings"));
  $("#shipclose").focus();
}
function closeShipView() {
  $("#shipmodal").hidden = true;
  const inline = $("#ship3d-inline");
  if (SHIP && inline) { SHIP.attach(inline); SHIP.view("quarter"); }
}
$("#shipclose").onclick = closeShipView;
$("#modalplay").onclick = () => setPlaying(!S.playing);
$("#shipmodal").addEventListener("click", e => { if (e.target.id === "shipmodal") closeShipView(); });
document.addEventListener("keydown", e => {
  if (e.key === " " && !["INPUT", "SELECT", "TEXTAREA", "BUTTON"].includes(document.activeElement.tagName) && S.result && S.result.found) {
    e.preventDefault(); setPlaying(!S.playing);
  }
  if (e.key === "Escape") {
    if (!$("#shipmodal").hidden) closeShipView();
    if (S.picking) { S.picking = null; $("#pickhint").hidden = true; fillPointSelects(); }
  }
});
$$("#shipmodal .views button").forEach(b => b.onclick = () => SHIP && SHIP.view(b.dataset.view));

function setFrac(f) {
  S.frac = Math.max(0, Math.min(1, f));
  const sl = $("#shipfrac"); if (sl) sl.value = Math.round(S.frac * 1000);
  updateOwn();
}
function goToEncounter(e) {
  const p = selectedPlan(); if (!p) return;
  setFrac(e.along_nm / p.metrics.distance_nm);
  CM.focus(e.at[0], e.at[1], 15);
}
function stepEncounter(dir) {
  const p = selectedPlan(); if (!p || !p.encounters.length) return;
  const here = S.frac * p.metrics.distance_nm;
  const list = [...p.encounters].sort((a, b) => a.along_nm - b.along_nm);
  const e = dir > 0 ? list.find(x => x.along_nm > here + 0.01) || list[0]
                    : [...list].reverse().find(x => x.along_nm < here - 0.01) || list[list.length - 1];
  goToEncounter(e);
}

/* =====================================================================
   side panel
   ===================================================================== */
function renderSide() {
  const r = S.result, side = $("#side");
  if (!r) return;
  if (!r.found) {
    side.innerHTML = `<div class="card"><h3>No route</h3><p class="note">${esc(r.reason || "no feasible route")}</p>
      <p class="note">Try a smaller ship, another terminal, a smaller passing-distance factor, or
      untick ship types to ignore.</p></div>`;
    renderSnapshotCard(side, true);
    return;
  }
  side.innerHTML = "";
  side.appendChild($("#tpl-result").content.cloneNode(true));
  const f = k => $(`[data-f="${k}"]`, side);
  const p = r.plans[S.sel] || r.plans[0], m = p.metrics;

  f("odlabel").textContent = `${r.origin_label} → ${r.destination_label}`;
  f("modes").innerHTML = r.plans.map((q, i) =>
    `<button type="button" role="tab" data-i="${i}" aria-selected="${i === S.sel}"
      style="--sel:var(${MODE_VARS[q.mode]})"><span class="sw"></span>${esc(q.label.includes("alternative")
      ? q.label.replace(q.mode, MODE_NAMES[q.mode]) : MODE_NAMES[q.mode])}</button>`).join("");
  $$("button", f("modes")).forEach(b => b.onclick = () => {
    S.sel = +b.dataset.i; S.wantMode = r.plans[S.sel].mode; renderSide(); CM.setSelected(S.sel); updateOwn();
  });
  f("distance").textContent = fmt(m.distance_nm, 2);
  f("time").textContent = `${dur(m.passage_h)} at ${S.meta.planning_speed_kn} kn`;
  f("met").textContent = m.obstacles_met;
  f("metnote").textContent = `of ${m.ships_considered} stationary ships`;
  f("minenv").textContent = nmTxt(m.min_ship_clear_nm);
  f("minhull").textContent = nmTxt(m.min_hull_dist_nm);
  f("inside").textContent = fmt(m.nm_inside_cpa, 2) + " NM";
  f("minshore").textContent = nmTxt(m.min_shore_nm);
  f("minukc").textContent = fmt(m.min_ukc_m, 1) + " m";
  f("legs").textContent = `${m.legs} · ${Math.round(m.max_turn_deg)}°`;
  const L = vesselObj().loa_m;
  f("turn").textContent = m.min_turn_radius_m
    ? `${fmt(m.min_turn_radius_m, 0)} m (${fmt(m.min_turn_radius_m / L, 1)} L) · ${fmt(m.max_rot_deg_min, 0)}°/min`
    : "no turns";
  f("detour").textContent = "+" + fmt(m.detour_pct, 1) + "%";
  f("feasnote").innerHTML = m.feasible
    ? `Measured on the track the ship actually sails (turns at 2.5 ship lengths where the water allows, never under 1.5 L at sea or 1 L under pilotage). Clear of land, of water shallower than draught + UKC, and of every ship&rsquo;s hard exclusion zone.`
    : `<b style="color:var(--bad)">Check:</b> ${m.hard_breaches} hard CPA breaches, ${fmt(m.shallow_nm, 2)} NM shallow${m.turn_ok === false ? `, ${m.tight_turns} turn(s) tighter than the ship can make` : ""}.`;
  f("csv").onclick = async e => {
    e.preventDefault();
    let cum = 0;
    // the passage plan: waypoints and the turn radius used at each
    const lines = ["waypoint,lat,lon,leg_nm,course_deg,cumulative_nm,turn_radius_m"];
    const W = p.waypoints || p.coords, RR = p.turn_radii || [];
    W.forEach((c, i) => {
      const rr = RR[i] == null ? "" : Math.round(RR[i]);
      if (!i) { lines.push(`0,${c[0].toFixed(6)},${c[1].toFixed(6)},,,0.000,${rr}`); return; }
      const a = W[i - 1], leg = ENG.hav(a[0], a[1], c[0], c[1]); cum += leg;
      lines.push(`${i},${c[0].toFixed(6)},${c[1].toFixed(6)},${leg.toFixed(3)},${ENG.initialBearing(a[0], a[1], c[0], c[1]).toFixed(1)},${cum.toFixed(3)},${rr}`);
    });
    const txt = lines.join("\n");
    try { await navigator.clipboard.writeText(txt); f("csv").textContent = `Copied ${p.coords.length} waypoints as CSV`; }
    catch (err) { const ta = document.createElement("textarea"); ta.value = txt; ta.rows = 6; ta.style.width = "100%";
                  f("csv").replaceWith(ta); ta.select(); }
  };

  // comparison table: one column per mode (first plan of each mode)
  const cols = [];
  r.plans.forEach((q2, i) => { if (!q2.label.includes("alternative")) cols.push([i, q2]); });
  const rows = [
    ["Distance", q2 => q2.metrics.distance_nm, v => fmt(v, 2), -1],
    ["Ships met", q2 => q2.metrics.obstacles_met, v => v, -1],
    ["Closest hull", q2 => q2.metrics.min_hull_dist_nm, v => fmt(v, 2), 1],
    ["Closest envelope", q2 => q2.metrics.min_ship_clear_nm, v => fmt(v, 2), 1],
    ["NM inside CPA", q2 => q2.metrics.nm_inside_cpa, v => fmt(v, 2), -1],
    ["Least UKC (m)", q2 => q2.metrics.min_ukc_m, v => fmt(v, 1), 1],
    ["Closest land", q2 => q2.metrics.min_shore_nm, v => fmt(v, 2), 1],
  ];
  f("compare").innerHTML = `<table class="cmp"><tr><th></th>${cols.map(([, q2]) =>
    `<th style="color:var(${MODE_VARS[q2.mode]})">${MODE_NAMES[q2.mode]}</th>`).join("")}</tr>`
    + rows.map(([lab, get, show, dir]) => {
      const vals = cols.map(([, q2]) => get(q2));
      const best = dir < 0 ? Math.min(...vals) : Math.max(...vals);
      const allSame = vals.every(v => Math.abs(v - vals[0]) < 1e-6);
      return `<tr><td>${lab}</td>${vals.map(v => `<td class="${!allSame && Math.abs(v - best) < 1e-6 ? "best" : ""}">${show(v)}</td>`).join("")}</tr>`;
    }).join("") + `</table>`;

  // encounters
  f("metcount").textContent = p.encounters.length ? `${m.obstacles_met}` : "none";
  f("encounters").innerHTML = p.encounters.length ? p.encounters.map((e, i) =>
    `<button type="button" data-i="${i}"><span class="tag">${esc(e.ship_type)}</span>
      <span>${esc(e.name || e.id)} <span style="color:var(--ink-3)">${Math.round(e.loa_m)} m${e.area ? " · " + esc(e.area) : ""} · at ${fmt(e.along_nm, 1)} NM</span></span>
      <span class="d ${e.cpa_nm < e.hard_nm ? "bad" : "warn"}" title="envelope clearance / hull distance / target">
        ${fmt(e.cpa_nm, 2)} / ${fmt(e.hull_nm, 2)} <span style="color:var(--ink-3)">&lt; ${e.target_nm}</span></span></button>`).join("")
    : `<p class="note" style="margin:0">No stationary ship comes inside its target passing distance.</p>`;
  $$("button", f("encounters")).forEach(b => b.onclick = () => goToEncounter(p.encounters[+b.dataset.i]));

  // own-ship card
  const sl = $("#shipfrac", side);
  sl.value = Math.round(S.frac * 1000);
  sl.oninput = () => { if (S.playing) setPlaying(false); setFrac(+sl.value / 1000); };
  const pb = $("#playbtn", side), spd = $("#playspeed", side);
  pb.textContent = S.playing ? "❚❚ Pause" : "▶ Play";
  pb.onclick = () => setPlaying(!S.playing);
  spd.value = String(S.speed);
  spd.onchange = () => { S.speed = +spd.value; };
  const tod = $("#tod", side); tod.value = S.tod;
  tod.onchange = () => { S.tod = tod.value; updateOwn(); };
  const sea = $("#sea", side); sea.value = S.sea;
  sea.onchange = () => { S.sea = sea.value; updateOwn(); };
  $("#prevclose", side).onclick = () => stepEncounter(-1);
  $("#nextclose", side).onclick = () => stepEncounter(1);
  $("#shipexpand", side).onclick = () => openShipView(true);
  const host = $("#ship3d-inline", side);
  if (SHIP) {
    const ph = $(".ship3d-placeholder", host); if (ph) ph.remove();
    if ($("#shipmodal").hidden) SHIP.attach(host);
  } else {
    const io = new IntersectionObserver(es => {
      if (es.some(e => e.isIntersecting)) { io.disconnect(); loadShip(host); }
    });
    io.observe(host);
  }
  paintOwnCard(ownState());

  renderSnapshotCard(side, false);

  f("search").innerHTML =
    `<span class="lbl">Board</span><span></span><span>${S.meta.grid[0]}×${S.meta.grid[1]} cells @ ${S.meta.resolution_deg}°</span>
     <span class="lbl">Graph</span><span></span><span>${r.network.nodes.toLocaleString()} navigable cells · 16 moves each</span>
     <span class="lbl">A* (this route)</span><span></span><span>${Math.round(p.search.nodes_expanded).toLocaleString()} expanded · ${fmt(p.search.runtime_ms, 0)} ms</span>`;
  const bb = f("benchbtn");
  bb.onclick = () => { bb.disabled = true; bb.textContent = "running…"; plan(true); };
  if (r.benchmark) {
    const b = r.benchmark;
    f("bench").innerHTML = `<div class="bd" style="margin-top:9px">
      <span class="lbl">Dijkstra</span><span></span><span>${Math.round(b.dijkstra.nodes_expanded).toLocaleString()} exp · ${fmt(b.dijkstra.runtime_ms, 0)} ms</span>
      <span class="lbl">A*</span><span></span><span>${Math.round(b.astar.nodes_expanded).toLocaleString()} exp · ${fmt(b.astar.runtime_ms, 0)} ms</span>
      <span class="lbl">Cost gap</span><span></span><span>${b.cost_gap < 1e-9 ? "0 (identical)" : b.cost_gap.toExponential(1)}</span></div>
      <p class="note">A* expands ${fmt(100 * (1 - b.astar.nodes_expanded / b.dijkstra.nodes_expanded), 0)}% fewer nodes
      and returns the same optimal cost (on the ${r.plans[0].mode} graph).</p>`;
    bb.remove();
  }
}

function renderSnapshotCard(side, standalone) {
  if (standalone) {
    const card = document.createElement("div");
    card.innerHTML = $("#tpl-result").content.querySelectorAll(".card")[4].outerHTML;
    side.appendChild(card.firstElementChild);
  }
  const f = k => $(`[data-f="${k}"]`, side);
  const sh = S.ships;
  if (!sh) return;
  f("snaplabel").innerHTML = `<b>${esc(sh.label)}</b> — ${sh.n} stationary ships`
    + (sh.captured_utc ? ` · captured ${esc(sh.captured_utc)}` : "")
    + (sh.notes && sh.notes.length ? `<br>${sh.notes.map(esc).join("<br>")}` : "");
  f("snapcounts").innerHTML = Object.entries(sh.counts).map(([k, v]) => `<span>${esc(k)} ${v}</span>`).join("");
  f("types").innerHTML = Object.keys(sh.counts).map(t =>
    `<label><input type="checkbox" value="${t}" ${S.ignored.has(t) ? "" : "checked"}>
      <i style="background:${CM.TYPE_COLOURS[t] || "#888"}"></i>${esc(t)}</label>`).join("");
  $$("input", f("types")).forEach(i => i.onchange = () => {
    if (i.checked) S.ignored.delete(i.value); else S.ignored.add(i.value);
    CM.setShips(S.ships); plan();
  });
  const sw = $("#swing", side); sw.checked = S.swing;
  sw.onchange = () => { S.swing = sw.checked; loadShips().then(plan); };
  const cs = $("#cpascale", side); cs.value = Math.round(S.cpaScale * 100);
  f("cpascaleval").textContent = S.cpaScale.toFixed(2);
  cs.oninput = () => { f("cpascaleval").textContent = (+cs.value / 100).toFixed(2); };
  cs.onchange = () => { S.cpaScale = +cs.value / 100; plan(); };
  const rules = S.meta.cpa_rules;
  f("cparules").innerHTML = `<div class="cparules"><span class="h">zone</span><span class="h">hard</span><span class="h">target</span>`
    + Object.values(rules).map(r => `<span title="${esc(r.context)}">${esc(r.zone)}</span>
      <span>${fmt(r.hard_nm * S.cpaScale, 2)}</span><span>${fmt(r.target_nm * S.cpaScale, 2)} NM</span>`).join("") + `</div>`;
  const fi = $("#snapfile", side);
  fi.onchange = async () => {
    const file = fi.files[0]; if (!file) return;
    f("uploadnote").textContent = `Reading ${file.name}…`;
    await tick();
    try {
      const P = ENG.parseAis(GRID, await file.text());
      const key = "upload:" + file.name;
      AIS.set(key, P);
      await useAisMinute(key, P.timed ? null : undefined);
    } catch (e) { f("uploadnote").textContent = "Could not use that file: " + e.message; }
  };
  // one snapshot per minute: the time slider picks which minute is on the map
  const box = f("aistime"), P = AIS.get($("#snapshot").value);
  box.hidden = !(P && P.timed && P.minutes.length > 1);
  if (!box.hidden) {
    const sl = $("#aist", side), snap = SNAPS[$("#snapshot").value];
    sl.max = P.minutes.length - 1; sl.value = Math.max(0, P.minutes.indexOf(snap.at));
    f("aistval").textContent = `${snap.at} \u00b7 ${snap.ships.length} stationary`;
    sl.oninput = () => { f("aistval").textContent = P.minutes[+sl.value]; };
    const go = i => useAisMinute($("#snapshot").value, P.minutes[Math.max(0, Math.min(P.minutes.length - 1, i))]);
    sl.onchange = () => go(+sl.value);
    f("aisprev").onclick = () => go(+sl.value - 1);
    f("aisnext").onclick = () => go(+sl.value + 1);
  }
}
async function useAisMinute(key, at) {
  const P = AIS.get(key), name = key.replace(/^upload:/, "");
  const {ships, moving, reporting, at: t} = ENG.snapshotAt(GRID, P, at, AIS_HOLD_MIN);
  if (!ships.length) throw new Error(`no stationary ships inside the study area${t ? " at " + t : ""} (${moving} under way were dropped)`);
  SNAPS[key] = {key, label: "Uploaded - " + name, source: "ais", ships, at: t, captured_utc: t ? t + " (file time)" : "",
    notes: [t ? `One snapshot at ${t}: ${reporting} vessels reporting (latest report within ${AIS_HOLD_MIN} min), ` +
                `${ships.length} stationary kept, ${moving} under way dropped. ${P.minutes.length} minutes in the file; ` +
                `use the slider to pick another.`
              : `${ships.length} stationary kept, ${moving} under way dropped (SOG > 0.5 kn and not anchored/moored)`]};
  S.meta.snapshots[key] = SNAPS[key].label;
  NETS.clear(); FIELDS.clear();
  fillSnapshots(); $("#snapshot").value = key;
  S.ignored.clear();
  await loadShips(); await plan();
}

/* =====================================================================
   the engine (engine.js) stands in for the Python server
   ===================================================================== */
const ENG = window.SafeRouteEngine;
let DATA = null, GRID = null;
const SNAPS = {}, FIELDS = new Map(), NETS = new Map(), AIS = new Map();
const AIS_HOLD_MIN = 3;   // a vessel counts if it reported in the 3 minutes up to the chosen minute
const tick = () => new Promise(r => setTimeout(r, 30));

function routeQuery() {
  const types = S.ignored.size && S.ships ? Object.keys(S.ships.counts).filter(t => !S.ignored.has(t)) : null;
  return {origin: $("#origin").value, destination: $("#dest").value, vessel: $("#vessel").value,
          snapshot: $("#snapshot").value, swing: S.swing, cpaScale: S.cpaScale, types};
}
function fillSnapshots() {
  $("#snapshot").innerHTML = Object.entries(S.meta.snapshots).map(([k, v]) =>
    `<option value="${esc(k)}">${esc(v)}</option>`).join("");
}
function activeShips(q) {
  const all = SNAPS[q.snapshot].ships;
  return q.types ? all.filter(s => q.types.includes(s.ship_type)) : all;
}
function fieldFor(q) {
  const k = [q.snapshot, q.swing, (q.types || []).join(",")].join("|");
  if (!FIELDS.has(k)) FIELDS.set(k, ENG.obstacleField(GRID, activeShips(q), q.swing));
  return FIELDS.get(k);
}
function netFor(q, mode) {
  const k = [q.vessel, mode, q.snapshot, q.swing, q.cpaScale, (q.types || []).join(",")].join("|");
  if (!NETS.has(k)) {
    const v = S.meta.vessels.find(x => x.code === q.vessel);
    NETS.set(k, ENG.buildNet(GRID, DATA, v, mode, fieldFor(q), {cpaScale: q.cpaScale}));
    if (NETS.size > 9) NETS.delete(NETS.keys().next().value);
  }
  return NETS.get(k);
}
function counts(ships) {
  const c = {}; ships.forEach(s => { c[s.ship_type] = (c[s.ship_type] || 0) + 1; });
  return Object.fromEntries(Object.entries(c).sort((a, b) => b[1] - a[1]));
}
async function loadShips() {
  const snap = SNAPS[$("#snapshot").value];
  S.ships = {key: snap.key, label: snap.label, source: snap.source, captured_utc: snap.captured_utc || "",
             notes: snap.notes || [], counts: counts(snap.ships), n: snap.ships.length,
             ships: snap.ships.map(s => Object.assign({}, s, {env: ENG.envelope(s, S.swing)}))};
  CM.setShips(S.ships);
  wireWorld();
  S.board = null;
  if ($("#L-board").checked) loadBoard();
}
async function loadBoard() {
  const q = routeQuery(), net = netFor(q, "shortest");
  S.board = {lat0: GRID.lat0, lon0: GRID.lon0, dlat: GRID.res, dlon: GRID.res, ny: GRID.ny, nx: GRID.nx, data: net.board};
  CM.setBoard(S.board);
  boardLegend();
}

/* =====================================================================
   MPA chart: berths at each terminal, chart layers (data_mpa.json / mpa.json
   are built only where the MPA GeoSpace-Sea files are present)
   ===================================================================== */
let MPA = null, mpaLoading = null;
const BERTH_BY_NAME = new Map(), BERTH_DEFAULT = {};
const BERTH_PREFIX = {SGTUA: ["Tuas F2 Terminal Berth "], SGJUR: ["Jurong Terminal (Jurong Port Pte Ltd) Berth "],
  SGPPT: ["Pasir Panjang Terminal (PSA Singapore Terminals) Berth "], SGKEP: ["Keppel Terminal Berth ", "Brani Terminal Berth "]};
function needM(v) { return v.draught_m + Math.max(1.0, 0.1 * v.draught_m); }
function berthDepthText(b) {
  const bd = b.berth_depth_m != null ? `berth ${b.berth_depth_m} m` : "berth depth not charted";
  const tr = b.track_min_depth_m != null ? `approach ≥ ${b.track_min_depth_m} m` : "";
  return [bd, tr].filter(Boolean).join(" · ") + " (chart datum)";
}
function initBerths() {
  BERTH_BY_NAME.clear();
  S.meta.ports.forEach(p => {
    if (!p.berths || !p.berths.length) return;
    BERTH_DEFAULT[p.code] = {berth_lat: p.berth_lat, berth_lon: p.berth_lon, berth_name: p.berth_name,
                             quay_hdg: p.quay_hdg, pilot: p.pilot, id: p.berths.find(b => b.name === p.berth_name)?.id};
    p.berths.forEach(b => {
      const pre = (BERTH_PREFIX[p.code] || []).find(x => x.startsWith("Brani") === b.id.startsWith("Brani "));
      const full = pre ? pre + b.id.replace(/^Brani /, "") : b.id;
      BERTH_BY_NAME.set(full, {code: p.code, id: b.id, depthText: berthDepthText(b)});
    });
  });
}
function fillBerthSelect(which) {
  const sel = $(which === "origin" ? "#oberth" : "#dberth"), code = $("#" + which).value;
  const p = S.meta.ports.find(q => q.code === code);
  if (!p || !p.berths || !p.berths.length) { sel.hidden = true; sel.innerHTML = ""; return; }
  const v = vesselObj(), need = needM(v), cur = p.berths.find(b => b.name === p.berth_name);
  sel.innerHTML = p.berths.map(b => {
    const c = b.controlling_depth_m, fits = c != null && c >= need;
    return `<option value="${esc(b.id)}">${esc(b.id)} · ${c != null ? c + " m" : "?"}${fits ? "" : " ✕"}</option>`;
  }).join("");
  sel.value = cur ? cur.id : p.berths[0].id;
  sel.hidden = false;
}
function applyBerth(code, id) {
  const p = S.meta.ports.find(q => q.code === code), b = p && p.berths.find(x => x.id === id);
  if (!b) return;
  // the engine reads the berth, its quay heading and the pilotage track from the port
  Object.assign(p, {berth_lat: b.berth[0], berth_lon: b.berth[1], berth_name: b.name,
                    quay_hdg: b.quay_hdg, pilot: b.pilot});
}
function berthInfo() {
  const box = $("#berthinfo"), lines = [];
  const v = vesselObj(), need = needM(v);
  for (const which of ["origin", "dest"]) {
    const p = S.meta.ports.find(q => q.code === $("#" + which).value);
    if (!p || !p.berths || !p.berths.length) continue;
    const b = p.berths.find(x => x.name === p.berth_name);
    if (!b) continue;
    const c = b.controlling_depth_m, ok = c != null && c >= need;
    lines.push(`<div><b>${which === "origin" ? "FROM" : "TO"} ${esc(b.name)}</b><br>${esc(berthDepthText(b))}<br>` +
      `${esc(v.name)} needs ${need.toFixed(2)} m (draught ${v.draught_m} m + UKC) &rarr; ` +
      `<span class="${ok ? "ok" : "bad"}">${ok ? "fits" : "charted depth too shallow at low water"}</span>` +
      `${b.fits && b.fits.length ? `<br><span style="opacity:.75">fits: ${b.fits.join(", ")}</span>` : ""}</div>`);
  }
  const open = box.querySelector("details") ? box.querySelector("details").open : true;
  box.innerHTML = lines.length ? `<details${open ? " open" : ""}><summary>Berth depth check</summary>` +
    lines.join("<hr style='border:0;border-top:1px solid var(--rule);margin:5px 0'>") + "</details>" : "";
  box.hidden = !lines.length;
}
function sailToBerth(code, id) {
  if (S.picking) return;
  $("#dest").value = code; fillBerthSelect("dest");
  $("#dberth").value = id; applyBerth(code, id);
  berthInfo(); plan();
}
["origin", "dest"].forEach(which => {
  const sel = $(which === "origin" ? "#oberth" : "#dberth");
  sel.addEventListener("change", () => { applyBerth($("#" + which).value, sel.value); berthInfo(); plan(); });
});
async function loadMpa() {
  if (MPA || mpaLoading || !MPA_LAYERS.some(id => $("#" + id).checked) || $("#mpalayers").hidden) return;
  mpaLoading = fetch("mpa.json").then(r => r.ok ? r.json() : null).catch(() => null);
  MPA = await mpaLoading; mpaLoading = null;
  if (MPA) CM.setMpa(MPA);
}
function depthKey() {
  const on = $("#L-mdepth").checked && !$("#mpalayers").hidden;
  $("#depthkey").hidden = !on;
  if (on) $("#depthkey").innerHTML = "<b style='font-size:9.5px;letter-spacing:.08em'>CHARTED DEPTH</b>" +
    CM.MPA_BANDS.map(([, c, t]) => `<span><i class="sw" style="background:${c}"></i>${t}</span>`).join("") +
    "<span><i class='sw' style='border:1px dashed #1f5f99'></i>dredged area</span>";
}

/* MPA GeoSpace-Sea (charted depths, hazards, named berths) is kept for the
   final project but switched off on the site for now. To reinstate it set
   MPA_ENABLED = true (or open the site with ?mpa=1 to preview it). */
const MPA_ENABLED = false || new URLSearchParams(location.search).get("mpa") === "1";

async function loadMeta() {
  let res = MPA_ENABLED ? await fetch("data_mpa.json").catch(() => null) : null;
  if (!res || !res.ok) res = await fetch("data.json");
  DATA = await res.json();
  GRID = ENG.makeGrid(DATA);
  S.meta = DATA.meta;
  SNAPS.synthetic = DATA.snapshot;
  S.meta.snapshots = {synthetic: "Synthetic - ships in MPA anchorages"};
  S.meta.default_snapshot = "synthetic";
  fillPointSelects();
  $("#origin").value = "EAST"; $("#dest").value = "SGPPT";
  $("#vessel").innerHTML = S.meta.vessels.map(v => `<option value="${v.code}">${esc(v.name)}</option>`).join("");
  $("#vessel").value = S.meta.default_vessel;
  fillSnapshots();
  $("#snapshot").value = "synthetic";
  const hasMpa = !!DATA.layers.charted;
  $("#gridinfo").textContent = `board ${S.meta.grid[0]}×${S.meta.grid[1]} @ ${S.meta.resolution_deg}° (${fmt(S.meta.cell_nm[0] * NM, 0)} m) · depth: ` +
    (hasMpa ? "MPA chart where charted, GEBCO elsewhere" : /gebco/i.test(S.meta.depth_source || "") ? "GEBCO 2026" : "proxy");
  $("#gridinfo").title = S.meta.depth_source || "";
  $("#mpalayers").hidden = !hasMpa;
  initBerths(); fillBerthSelect("origin"); fillBerthSelect("dest"); berthInfo();
  loadMpa(); depthKey();
  CM.setMeta(S.meta);
  boardLegend();
  await loadShips();
  await plan();
}

/* Each mode searches on its own and, when the ship cannot make a turn, closes
   the water round it and searches again. That repair is local, so it can push
   one mode onto a much longer detour while another mode found a sailable route
   that this mode would also have scored cheaper. Re-score every sailable route
   under each mode's own weights and keep the cheapest one, so Shortest is never
   beaten by a route it could have taken. */
function crossCheckModes(q, plans) {
  const nets = Object.fromEntries(plans.map(p => [p.mode, netFor(q, p.mode)]));
  const ok = plans.filter(p => p.metrics.feasible && p.gridPath && p.gridPath.length);
  plans.forEach((p, i) => {
    const net = nets[p.mode];
    let best = p, bestC = p.metrics.feasible ? ENG.pathCost(net, p.gridPath) : Infinity;
    ok.forEach(o => {
      if (o === p) return;
      const c = ENG.pathCost(net, o.gridPath);
      if (c < bestC - 1e-6) { best = o; bestC = c; }
    });
    if (best !== p) {
      plans[i] = Object.assign({}, best, {label: p.label, mode: p.mode,
        metrics: Object.assign({}, best.metrics, {cost_effective_nm: bestC, shared_with: best.mode})});
    }
  });
}

function pointArg(v) { return v.includes(",") ? v.split(",").map(Number) : v; }
function routeLocal(q, benchmark) {
  const o = pointArg(q.origin), d = pointArg(q.destination), ships = activeShips(q);
  // an MPA berth must be deep enough for this ship at chart datum (berth and pilotage track)
  const v = S.meta.vessels.find(x => x.code === q.vessel) || vesselObj(), need = needM(v);
  for (const x of [o, d]) {
    const p = typeof x === "string" && S.meta.ports.find(q2 => q2.code === x);
    const b = p && p.berths && p.berths.find(y => y.name === p.berth_name);
    if (b && b.controlling_depth_m != null && b.controlling_depth_m < need)
      return {found: false, reason: `${b.name}: the least charted depth to the berth is ${b.controlling_depth_m} m ` +
        `(berth ${b.berth_depth_m} m, approach ${b.track_min_depth_m} m, chart datum); a ${v.name} needs ${need.toFixed(2)} m. ` +
        `Pick a deeper berth (✕ marks the ones that do not fit) or a smaller ship.`};
  }
  const label = x => typeof x === "string" ? S.meta.ports.find(p => p.code === x).name : `${x[0].toFixed(4)} N, ${x[1].toFixed(4)} E`;
  const plans = [];
  let net0 = null;
  for (const mode of ["shortest", "balanced", "safest"]) {
    const net = netFor(q, mode); net0 = net0 || net;
    const why = [ENG.endpointCheck(net, o), ENG.endpointCheck(net, d)].filter(Boolean);
    if (why.length) return {found: false, reason: why.join("; ")};
    const p = ENG.planRoute(net, o, d, ships, q.swing);
    if (p) plans.push(p);
  }
  if (!plans.length) return {found: false, reason: "no route satisfies every hard limit for this ship"};
  crossCheckModes(q, plans);
  let bench = null;
  if (benchmark) {
    const src = ENG.nodeAt(net0, ...(typeof o === "string" ? [S.meta.ports.find(p => p.code === o).lat, S.meta.ports.find(p => p.code === o).lon] : o));
    const dst = ENG.nodeAt(net0, ...(typeof d === "string" ? [S.meta.ports.find(p => p.code === d).lat, S.meta.ports.find(p => p.code === d).lon] : d));
    const dj = ENG.search(net0, src, dst, false), as = ENG.search(net0, src, dst, true);
    bench = {dijkstra: dj, astar: as, cost_gap: Math.abs(dj.cost - as.cost)};
  }
  return {found: true, origin: q.origin, destination: q.destination, origin_label: label(o),
          destination_label: label(d), vessel: q.vessel, snapshot: q.snapshot,
          network: {nodes: net0.nodes, edges: 0}, plans, benchmark: bench};
}

async function plan(benchmark = false) {
  if (S.playing) setPlaying(false);
  if (S.busy) { S.pending = true; return; }
  if ($("#origin").value === "__pick" || $("#dest").value === "__pick") return;
  S.busy = true; S.pending = false;
  const btn = $("#plan"); btn.disabled = true; btn.textContent = "searching…";
  try {
    await tick();
    const data = routeLocal(routeQuery(), benchmark);
    S.result = data;
    const want = data.found ? data.plans.findIndex(p => p.mode === S.wantMode) : 0;
    S.sel = Math.max(0, want);
    renderSide();
    CM.setResult(S.result, S.sel);
    if (!benchmark && data.found) CM.fitResult();
    updateOwn();
    if ($("#L-board").checked) loadBoard();
  } catch (err) {
    console.error(err);
    $("#side").innerHTML = `<div class="card"><h3>Could not plan</h3><p class="note">${esc(err.message)}</p></div>`;
  } finally {
    S.busy = false; btn.disabled = false; btn.textContent = "Find route";
    if (S.pending) { S.pending = false; plan(); }
  }
}

$("#plan").onclick = () => plan();
$("#vessel").onchange = () => { fillBerthSelect("origin"); fillBerthSelect("dest"); berthInfo(); plan(); };
$("#snapshot").onchange = async () => { S.ignored.clear(); await loadShips(); plan(); };
$("#theme").onclick = () => {
  const dark = isDark();
  document.documentElement.setAttribute("data-theme", dark ? "light" : "dark");
  $("#theme").textContent = dark ? "Night" : "Day";
  CM.setTheme(!dark); CM.setShips(S.ships); if (S.result) { CM.setResult(S.result, S.sel); renderSide(); updateOwn(); }
};
new ResizeObserver(() => CM.invalidate()).observe($("#chart"));
loadMeta();
