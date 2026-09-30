/* Ship3D - a 3D view of your ship at a point on its route, with the
 * stationary ships around it placed at their true relative positions, sizes
 * and headings, and range rings on the water (0.1, 0.25, 0.5, 1 NM), so you
 * can see how close the route physically brings you to each of them.
 *
 * Everything is procedural (no downloaded models or image files), so there
 * are no asset licences to track and the view works offline:
 *
 *   hull      lofted from station sections: flat bottom with a bilge radius,
 *             parallel midbody, fine entrance with bow flare, raked stem,
 *             bulbous bow, aft cut-up and a transom stern. Painted in
 *             antifouling red / boot-top black / topside colour at the real
 *             draught, so the ship floats at its design waterline.
 *   cargo     container ships carry 40 ft and 20 ft boxes (ISO sizes) in bays
 *             with lashing bridges and hatch covers; rows follow the deck's
 *             width and stacks step down towards the bow. Tankers get a pipe
 *             rack, manifold and catwalk instead.
 *   upperworks accommodation block with window rows (lit at night), bridge
 *             wings, mast with radar scanners, funnel, lifeboat, navigation
 *             lights (red to port, green to starboard).
 *   sea / sky three.js Water and Sky shaders; the wave normal map is
 *             generated in code. Bow wave and wake scale with speed; at
 *             anchor the wake disappears and a chain runs from the hawse.
 *
 * Sun position is an equinox approximation from local solar time and
 * latitude - good enough for day/night and lighting direction, not an
 * almanac. Dimensions (LOA, beam, depth, draught, TEU) come from the vessel
 * class in study_area.py.
 */
import * as THREE from "three";
import {Sky} from "./vendor/three/addons/Sky.js";
import {Water} from "./vendor/three/addons/Water.js";
import {OrbitControls} from "./vendor/three/addons/OrbitControls.js";

/* ------------------------------------------------------------------ utils */
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
const lerp = (a, b, t) => a + (b - a) * t;
const smooth = (a, b, x) => { const t = clamp((x - a) / (b - a), 0, 1); return t * t * (3 - 2 * t); };
function rng(seed) {
  return () => { seed |= 0; seed = seed + 0x6D2B79F5 | 0;
    let t = Math.imul(seed ^ seed >>> 15, 1 | seed);
    t = t + Math.imul(t ^ t >>> 7, 61 | t) ^ t;
    return ((t ^ t >>> 14) >>> 0) / 4294967296; };
}
function canvasTex(w, h, draw, opts = {}) {
  const c = document.createElement("canvas"); c.width = w; c.height = h;
  draw(c.getContext("2d"), w, h);
  const t = new THREE.CanvasTexture(c);
  t.wrapS = t.wrapT = opts.repeat ? THREE.RepeatWrapping : THREE.ClampToEdgeWrapping;
  t.anisotropy = 4;
  if (opts.srgb !== false) t.colorSpace = THREE.SRGBColorSpace;
  return t;
}

/* ------------------------------------------------------------- textures */
function waterNormals() {
  // Tileable fractal value noise -> height field -> normal map. Several
  // octaves with a slight directional stretch read as wind-driven chop,
  // without the regular lattice a sum of sines produces.
  const N = 512, hgt = new Float32Array(N * N), r = rng(7);
  const oct = [[8, 1], [16, .55], [32, .3], [64, .16], [128, .08]];
  for (const [f, amp] of oct) {
    const fx = f, fy = Math.max(2, Math.round(f * .6));            // stretched across the wind
    const lat = Array.from({length: fx * fy}, () => r());
    const at = (i, j) => lat[((j % fy) + fy) % fy * fx + ((i % fx) + fx) % fx];
    for (let y = 0; y < N; y++) {
      const gy = y / N * fy, j0 = Math.floor(gy), ty = gy - j0, sy = ty * ty * (3 - 2 * ty);
      for (let x = 0; x < N; x++) {
        const gx = x / N * fx, i0 = Math.floor(gx), tx = gx - i0, sx = tx * tx * (3 - 2 * tx);
        const v = lerp(lerp(at(i0, j0), at(i0 + 1, j0), sx), lerp(at(i0, j0 + 1), at(i0 + 1, j0 + 1), sx), sy);
        hgt[y * N + x] += (v - .5) * amp;
      }
    }
  }
  return canvasTex(N, N, (g) => {
    const img = g.createImageData(N, N), v3 = new THREE.Vector3();
    for (let y = 0; y < N; y++) for (let x = 0; x < N; x++) {
      const dx = hgt[y * N + (x + 1) % N] - hgt[y * N + (x - 1 + N) % N];
      const dy = hgt[((y + 1) % N) * N + x] - hgt[((y - 1 + N) % N) * N + x];
      v3.set(-dx * 22, -dy * 22, 1).normalize(); const o = (y * N + x) * 4;
      img.data[o] = (v3.x * .5 + .5) * 255; img.data[o + 1] = (v3.y * .5 + .5) * 255;
      img.data[o + 2] = (v3.z * .5 + .5) * 255; img.data[o + 3] = 255;
    }
    g.putImageData(img, 0, 0);
  }, {repeat: true, srgb: false});
}

function corrugationTex() {
  return canvasTex(512, 128, (g, w, h) => {
    const ribs = 44;
    for (let x = 0; x < w; x++) {
      const s = Math.sin(x / w * ribs * Math.PI * 2);
      const v = 205 + s * 38;
      g.fillStyle = `rgb(${v},${v},${v})`; g.fillRect(x, 0, 1, h);
    }
    g.fillStyle = "rgba(40,40,40,.55)"; g.fillRect(0, 0, w, 7); g.fillRect(0, h - 8, w, 8);
    g.fillStyle = "rgba(60,60,60,.35)"; g.fillRect(0, 0, 6, h); g.fillRect(w - 6, 0, 6, h);
    const grd = g.createLinearGradient(0, 0, 0, h);
    grd.addColorStop(0, "rgba(0,0,0,0)"); grd.addColorStop(1, "rgba(60,40,20,.18)");
    g.fillStyle = grd; g.fillRect(0, 0, w, h);
  });
}
function doorTex() {
  return canvasTex(128, 128, (g, w, h) => {
    g.fillStyle = "rgb(215,215,215)"; g.fillRect(0, 0, w, h);
    for (let x = 0; x < w; x++) {
      const v = 200 + Math.sin(x / w * 10 * Math.PI * 2) * 22;
      g.fillStyle = `rgb(${v},${v},${v})`; g.fillRect(x, 8, 1, h - 16);
    }
    g.fillStyle = "rgba(40,40,40,.6)";
    g.fillRect(0, 0, w, 8); g.fillRect(0, h - 8, w, 8); g.fillRect(w / 2 - 1, 0, 2, h);
    g.fillStyle = "rgba(70,70,70,.9)";
    [0.18, 0.38, 0.62, 0.82].forEach(f => g.fillRect(f * w - 1.5, 10, 3, h - 20));
  });
}
function plainTex(base = 200) {
  return canvasTex(64, 64, (g, w, h) => {
    const r = rng(3);
    for (let i = 0; i < 400; i++) {
      const v = base + (r() - .5) * 22; g.fillStyle = `rgb(${v},${v},${v})`;
      g.fillRect(r() * w, r() * h, 3, 3);
    }
  });
}
function windowsTex(cols, rows, lit, seed) {
  // Diffuse (lit=false) or emissive (lit=true) texture for accommodation walls.
  return canvasTex(512, 256, (g, w, h) => {
    const r = rng(seed);
    g.fillStyle = lit ? "#000" : "#e9e7e1"; g.fillRect(0, 0, w, h);
    const cw = w / cols, rh = h / rows;
    for (let j = 0; j < rows; j++) for (let i = 0; i < cols; i++) {
      const x = i * cw + cw * .22, y = j * rh + rh * .3, ww = cw * .56, hh = rh * .42;
      if (lit) { if (r() < .55) { g.fillStyle = r() < .7 ? "#ffd89a" : "#fff2d8"; g.fillRect(x, y, ww, hh); } }
      else { g.fillStyle = "#26323d"; g.fillRect(x, y, ww, hh); }
    }
    if (!lit) { g.fillStyle = "rgba(0,0,0,.08)"; for (let j = 1; j < rows; j++) g.fillRect(0, j * rh - 1, w, 2); }
  });
}
function foamTex() {
  return canvasTex(256, 512, (g, w, h) => {
    const r = rng(11);
    g.fillStyle = "#000"; g.fillRect(0, 0, w, h);
    const blob = (x, y, rx, ry, a) => {
      for (const dx of [-w, 0, w]) for (const dy of [-h, 0, h]) {
        g.save(); g.translate(x + dx, y + dy); g.scale(rx, ry);
        const grd = g.createRadialGradient(0, 0, 0, 0, 0, 1);
        grd.addColorStop(0, `rgba(255,255,255,${a})`); grd.addColorStop(1, "rgba(255,255,255,0)");
        g.fillStyle = grd; g.beginPath(); g.arc(0, 0, 1, 0, 6.283); g.fill(); g.restore();
      }
    };
    for (let i = 0; i < 260; i++) blob(r() * w, r() * h, 6 + r() * 22, 14 + r() * 50, .12 + r() * .25);  // cloud
    for (let i = 0; i < 900; i++) blob(r() * w, r() * h, 1 + r() * 3, 3 + r() * 14, .35 + r() * .5);    // streaks
  }, {repeat: true, srgb: false});
}

/* ------------------------------------------------------------------ hull */
function hullModel(v) {
  const L = v.loa_m, B = v.beam_m, D = v.moulded_depth_m || v.beam_m * .65, T = v.draught_m;
  const tanker = v.hull_type === "tanker" || v.hull_type === "bulker";
  const sF = tanker ? .8 : .7, sA = tanker ? .18 : .25;         // tankers are fuller
  const bilge = B * (tanker ? .09 : .12);
  const deckH = s => D + .6 * smooth(.5, 0, s) + (tanker ? .8 : 1.6) * Math.pow(Math.max(0, (s - .55) / .45), 2)
                   + (s > .86 ? 2.6 * smooth(.86, .9, s) : 0);
  const keelH = s => (s < .14 ? .85 * T * Math.pow((.14 - s) / .14, 1.35) : 0)
                   + (s > .965 ? .7 * T * Math.pow((s - .965) / .035, 2) : 0);
  const xStern = h => -L / 2 + .012 * L * clamp(1 - h / D, 0, 1);
  const xStem = h => L / 2 - .028 * L * Math.pow(clamp(1 - h / (D + 3), 0, 1), 1.25);
  function half(s, h) {
    const hk = keelH(s), hr = Math.max(0, h - hk);
    let w = hr < bilge ? B / 2 - bilge + Math.sqrt(Math.max(0, bilge * bilge - (bilge - hr) ** 2)) : B / 2;
    if (s > sF) {
      const t = (s - sF) / (1 - sF);
      const e = lerp(tanker ? 2.2 : 1.55, tanker ? 4.2 : 3.4, clamp(h / D, 0, 1.1));   // flare: fuller higher up
      w *= Math.pow(Math.max(0, 1 - Math.pow(t, e)), tanker ? .75 : .9);
    } else if (s < sA) {
      const t = (sA - s) / sA;
      const k = clamp(1 - (h - .1 * T) / (.95 * T), 0, 1);       // underwater run narrows, transom stays wide
      w *= (1 - .96 * k * Math.pow(t, 1.5)) * (1 - .1 * t * t);
    }
    return Math.max(0, w);
  }
  return {L, B, D, T, tanker, deckH, keelH, xStern, xStem, half};
}

function buildHull(H, colours) {
  const NS = 150, NH = 34;
  const pos = [], col = [], idx = [];
  const cRed = new THREE.Color(colours.bottom), cBoot = new THREE.Color("#141414"),
        cTop = new THREE.Color(colours.topside), cStripe = new THREE.Color("#e8e6df");
  const colourAt = (y, yDeck) => y < H.T - .25 ? cRed : y < H.T + .9 ? cBoot
                               : (yDeck - y < .7 && colours.stripe ? cStripe : cTop);
  // grid of (s, j) points for the starboard side, mirrored for port
  const rows = [];
  for (let i = 0; i <= NS; i++) {
    const s = Math.pow(i / NS, 1) ;
    const hk = H.keelH(s), hd = H.deckH(s), row = [];
    for (let j = 0; j <= NH; j++) {
      // cluster heights near the keel so the bilge turns smoothly
      const u = j / NH, f = u < .35 ? Math.pow(u / .35, 1.6) * .35 : u;
      const h = lerp(hk, hd, f);
      const x = lerp(H.xStern(h), H.xStem(h), s);
      row.push([x, h, H.half(s, h), hd]);
    }
    row.unshift([lerp(H.xStern(hk), H.xStem(hk), s), hk, 0, hd]);   // bottom centreline
    rows.push(row);
  }
  const W = rows[0].length;
  for (const side of [1, -1]) {
    const base = pos.length / 3;
    rows.forEach(row => row.forEach(([x, h, w, hd]) => {
      pos.push(x, h, w * side); const c = colourAt(h, hd); col.push(c.r, c.g, c.b);
    }));
    for (let i = 0; i < NS; i++) for (let j = 0; j < W - 1; j++) {
      const a = base + i * W + j, b = a + W, c = b + 1, d = a + 1;
      if (side > 0) idx.push(a, b, d, d, b, c); else idx.push(a, d, b, d, c, b);   // outward-facing
    }
  }
  const g = new THREE.BufferGeometry();
  g.setAttribute("position", new THREE.Float32BufferAttribute(pos, 3));
  g.setAttribute("color", new THREE.Float32BufferAttribute(col, 3));
  g.setIndex(idx); g.computeVertexNormals();

  // transom: close station 0 across the centreline
  const tp = [], tc = [], ti = [], r0 = rows[0];
  r0.forEach(([x, h, w, hd]) => { tp.push(x, h, w, x, h, -w); const c = colourAt(h, hd); tc.push(c.r, c.g, c.b, c.r, c.g, c.b); });
  for (let j = 0; j < r0.length - 1; j++) { const a = j * 2; ti.push(a, a + 1, a + 2, a + 1, a + 3, a + 2); }
  const tg = new THREE.BufferGeometry();
  tg.setAttribute("position", new THREE.Float32BufferAttribute(tp, 3));
  tg.setAttribute("color", new THREE.Float32BufferAttribute(tc, 3));
  tg.setIndex(ti); tg.computeVertexNormals();

  // deck surface
  const dp = [], di = [];
  rows.forEach(row => { const [x, h, w] = row[row.length - 1]; dp.push(x, h, w, x, h, -w); });
  for (let i = 0; i < NS; i++) { const a = i * 2; di.push(a, a + 2, a + 1, a + 1, a + 2, a + 3); }
  const dg = new THREE.BufferGeometry();
  dg.setAttribute("position", new THREE.Float32BufferAttribute(dp, 3));
  dg.setIndex(di); dg.computeVertexNormals();

  const mat = new THREE.MeshStandardMaterial({vertexColors: true, roughness: .55, metalness: .25});
  const grp = new THREE.Group();
  const hull = new THREE.Mesh(g, mat); hull.castShadow = hull.receiveShadow = true;
  const tr = new THREE.Mesh(tg, mat.clone()); tr.material.side = THREE.DoubleSide;
  const deck = new THREE.Mesh(dg, new THREE.MeshStandardMaterial({color: colours.deck, roughness: .85}));
  deck.receiveShadow = true;
  grp.add(hull, tr, deck);

  // bulbous bow
  const bl = H.L * .045, bulb = new THREE.Mesh(new THREE.SphereGeometry(1, 32, 20),
    new THREE.MeshStandardMaterial({color: colours.bottom, roughness: .6, metalness: .2}));
  bulb.scale.set(bl, H.T * .24, H.B * .1);
  bulb.position.set(H.xStem(H.T * .35) - bl * .15, H.T * .38, 0);
  bulb.castShadow = true;
  grp.add(bulb);

  // bulwark around the forecastle
  const bw = [], s0 = .86;
  for (let i = 0; i <= 30; i++) {
    const s = lerp(s0, 1, i / 30), h = H.deckH(s);
    bw.push(new THREE.Vector3(lerp(H.xStern(h), H.xStem(h), s), h, H.half(s, h)));
  }
  const wallMat = new THREE.MeshStandardMaterial({color: colours.topside, roughness: .6, metalness: .2, side: THREE.DoubleSide});
  for (const side of [1, -1]) {
    const p = [], ii = [];
    bw.forEach(q => { p.push(q.x, q.y, q.z * side, q.x, q.y + 1.3, q.z * side); });
    for (let i = 0; i < bw.length - 1; i++) { const a = i * 2; ii.push(a, a + 2, a + 1, a + 1, a + 2, a + 3); }
    const wg = new THREE.BufferGeometry();
    wg.setAttribute("position", new THREE.Float32BufferAttribute(p, 3)); wg.setIndex(ii); wg.computeVertexNormals();
    grp.add(new THREE.Mesh(wg, wallMat));
  }
  return grp;
}

/* --------------------------------------------------------- upperworks */
function box(w, h, d, mat, x, y, z) {
  const m = new THREE.Mesh(new THREE.BoxGeometry(w, h, d), mat);
  m.position.set(x, y + h / 2, z); m.castShadow = true; m.receiveShadow = true; return m;
}
function buildAccommodation(H, xCentre, decks, lights) {
  // A stepped deckhouse: lower decks full width, upper decks set back and a
  // little narrower, then the bridge deck running out to the ship's side as
  // wings, with a continuous dark window band and a mast on the compass deck.
  const g = new THREE.Group(), deckY = H.deckH((xCentre + H.L / 2) / H.L);
  const len = clamp(H.L * .052, 11, 24), wid = H.B * .78, dh = 2.8;
  const white = new THREE.MeshStandardMaterial({color: "#eeede8", roughness: .6, metalness: .05});
  const trim = new THREE.MeshStandardMaterial({color: "#c9c7c0", roughness: .6});
  const mkWin = (c, r, seed) => new THREE.MeshStandardMaterial({
    map: windowsTex(c, r, false, seed), emissiveMap: windowsTex(c, r, true, seed),
    emissive: new THREE.Color("#ffcf8a"), emissiveIntensity: 0, roughness: .55});
  let y = deckY, x = xCentre, l = len, w = wid;
  const tiers = [[Math.ceil(decks * .45), 1, 1], [decks - Math.ceil(decks * .45), .86, .9]];
  tiers.forEach(([n, lf, wf], ti) => {
    l = len * lf; w = wid * wf; const hgt = n * dh;
    x = xCentre - (len - l) / 2;                                    // set back from the front
    const front = mkWin(Math.round(w / 3.2), n, 5 + ti), side = mkWin(Math.round(l / 3.2), n, 9 + ti);
    lights.push(front, side);
    const blk = new THREE.Mesh(new THREE.BoxGeometry(l, hgt, w), [front, white, white, white, side, side]);
    blk.position.set(x, y + hgt / 2, 0); blk.castShadow = blk.receiveShadow = true; g.add(blk);
    for (let k = 1; k <= n; k++) g.add(box(l + .5, .22, w + .5, trim, x, y + k * dh - .22, 0));   // deck edges
    y += hgt;
  });
  // bridge
  const glass = new THREE.MeshStandardMaterial({color: "#16222c", roughness: .08, metalness: .8,
                                                emissive: new THREE.Color("#7fa0b8"), emissiveIntensity: 0});
  lights.push(glass);
  const bx = x + l * .05, bl = l * .95, bw = H.B * 1.04;
  g.add(box(bl, 1.1, bw, white, bx, y, 0));
  g.add(box(bl * 1.005, 1.7, bw * 1.002, glass, bx, y + 1.1, 0));
  g.add(box(bl * 1.02, .6, bw * 1.01, white, bx, y + 2.8, 0));
  g.add(box(bl * .6, 1.2, w * .6, white, bx - bl * .15, y + 3.4, 0));                          // compass deck
  const by = y + 4.6;
  // mast with radar scanners and antennas
  const mastMat = new THREE.MeshStandardMaterial({color: "#dedcd5", roughness: .45, metalness: .45});
  const mx = bx - bl * .15;
  const mast = new THREE.Mesh(new THREE.CylinderGeometry(.3, .45, 8, 10), mastMat);
  mast.position.set(mx, by + 4, 0); mast.castShadow = true; g.add(mast);
  g.add(box(1.2, .3, 7, mastMat, mx, by + 3.2, 0));
  const scan = new THREE.MeshStandardMaterial({color: "#262626", roughness: .5});
  g.add(box(.35, .3, 6.5, scan, mx + 1.2, by + 3.6, 0));
  g.add(box(.35, .3, 4.5, scan, mx, by + 6.5, 0));
  [-2.8, 2.8].forEach(z => { const a = new THREE.Mesh(new THREE.CylinderGeometry(.08, .08, 3.2, 6), mastMat);
                             a.position.set(mx, by + 5, z); g.add(a); });
  // navigation lights
  const lamp = (c, x0, y0, z0, r0 = .45) => {
    const m = new THREE.Mesh(new THREE.SphereGeometry(r0, 12, 8),
      new THREE.MeshStandardMaterial({color: c, emissive: new THREE.Color(c), emissiveIntensity: 0}));
    m.position.set(x0, y0, z0); lights.push(m.material); g.add(m);
  };
  lamp("#ff3b30", bx + bl * .3, y + 3.3, -bw * .5);    // port side light
  lamp("#34d058", bx + bl * .3, y + 3.3, bw * .5);     // starboard side light
  lamp("#ffffff", mx, by + 8.2, 0);                    // masthead
  return {group: g, top: by + 8.2, bridgeTop: y + 3.4, aft: xCentre - len / 2, fore: xCentre + len / 2, lamp};
}
function buildFunnel(H, x, top) {
  const g = new THREE.Group(), deckY = H.deckH((x + H.L / 2) / H.L), h = top - deckY;
  const l = clamp(H.L * .035, 8, 16), w = H.B * .24;
  const shape = new THREE.Shape();
  shape.moveTo(-l / 2, 0); shape.lineTo(l / 2, 0); shape.lineTo(l / 2 - l * .08, h);
  shape.lineTo(-l / 2 - l * .12, h); shape.lineTo(-l / 2, 0);                        // raked aft
  const geo = new THREE.ExtrudeGeometry(shape, {depth: w, bevelEnabled: true, bevelSize: w * .12,
                                                bevelThickness: w * .12, bevelSegments: 4, curveSegments: 4});
  geo.translate(0, 0, -w / 2);
  const body = new THREE.Mesh(geo, new THREE.MeshStandardMaterial({color: "#f1f0eb", roughness: .55}));
  body.position.set(x, deckY, 0); body.castShadow = true; g.add(body);
  const band = new THREE.Mesh(new THREE.BoxGeometry(l * 1.02, h * .14, w * 1.3),
    new THREE.MeshStandardMaterial({color: "#1f5d99", roughness: .45}));
  band.position.set(x - l * .06, deckY + h * .7, 0); g.add(band);
  const cap = new THREE.Mesh(new THREE.BoxGeometry(l * 1.1, h * .07, w * 1.27),
    new THREE.MeshStandardMaterial({color: "#151515", roughness: .8}));
  cap.position.set(x - l * .1, deckY + h * .965, 0); g.add(cap);
  [-.18, .18].forEach(z => {                                                         // exhaust pipes
    const p = new THREE.Mesh(new THREE.CylinderGeometry(.7, .7, 2.4, 12),
      new THREE.MeshStandardMaterial({color: "#2a2a2a", roughness: .6, metalness: .5}));
    p.position.set(x - l * .1, deckY + h + 1, z * w); g.add(p);
  });
  return g;
}
function buildForecastle(H, colours, lamp) {
  const g = new THREE.Group();
  const grey = new THREE.MeshStandardMaterial({color: "#8d9496", roughness: .7, metalness: .3});
  const dark = new THREE.MeshStandardMaterial({color: "#2c2f31", roughness: .6, metalness: .5});
  // breakwater: a V-shaped screen protecting the forward bays
  const sB = .855, hB = H.deckH(sB), xB = lerp(H.xStern(hB), H.xStem(hB), sB), wB = H.half(sB, hB);
  const shape = new THREE.Shape();
  shape.moveTo(0, -wB); shape.lineTo(6, 0); shape.lineTo(0, wB); shape.lineTo(-.6, wB); shape.lineTo(5.4, 0); shape.lineTo(-.6, -wB);
  const bw = new THREE.Mesh(new THREE.ExtrudeGeometry(shape, {depth: 4.2, bevelEnabled: false}), grey);
  bw.rotation.x = -Math.PI / 2; bw.position.set(xB, hB, 0); bw.castShadow = true; g.add(bw);
  // mooring winches and bitts on the forecastle
  const sF = .94, hF = H.deckH(sF), xF = lerp(H.xStern(hF), H.xStem(hF), sF);
  for (const z of [-1, 1]) {
    const drum = new THREE.Mesh(new THREE.CylinderGeometry(1.1, 1.1, 2.6, 16), dark);
    drum.rotation.x = Math.PI / 2; drum.position.set(xF - 6, hF + 1.3, z * H.B * .2); g.add(drum);
    const wl = new THREE.Mesh(new THREE.CylinderGeometry(.9, .9, 1.8, 16), grey);
    wl.rotation.x = Math.PI / 2; wl.position.set(xF, hF + 1, z * H.B * .12); g.add(wl);
    // anchors in their pockets on the bow flare
    const sa = .972, ha = H.deckH(sa) - 3.2, xa = lerp(H.xStern(ha), H.xStem(ha), sa);
    const an = box(3.2, 3.6, .5, dark, xa, ha - 1.8, z * (H.half(sa, ha) + .15));
    an.rotation.y = z * .35; g.add(an);
  }
  // foremast with a white masthead light
  const sM = .975, hM = H.deckH(sM), xM = lerp(H.xStern(hM), H.xStem(hM), sM);
  const fm = new THREE.Mesh(new THREE.CylinderGeometry(.25, .35, 10, 8),
    new THREE.MeshStandardMaterial({color: "#e7d33c", roughness: .5}));
  fm.position.set(xM - 2, hM + 5, 0); fm.castShadow = true; g.add(fm);
  lamp("#ffffff", xM - 2, hM + 10.2, 0, .4);
  return g;
}
function buildLifeboat(H) {
  const g = new THREE.Group(), s = .03, h = H.deckH(s);
  const x = lerp(H.xStern(h), H.xStem(h), s) + 2;
  const hullM = new THREE.MeshStandardMaterial({color: "#ff6a13", roughness: .5});
  const lb = new THREE.Mesh(new THREE.CapsuleGeometry(1.6, 6, 6, 12), hullM);
  lb.rotation.z = Math.PI / 2 + .55; lb.position.set(x + 2, h + 3.2, 0); lb.castShadow = true; g.add(lb);
  const ramp = box(8, .4, 4, new THREE.MeshStandardMaterial({color: "#d0cec8"}), x + 2, h + 1.2, 0);
  ramp.rotation.z = .55; g.add(ramp);
  return g;
}

/* ---------------------------------------------------------- containers */
const BOX_COLOURS = [["#8e3a2c", 16], ["#1f4f7a", 14], ["#2f6b4f", 9], ["#b9bab4", 12], ["#c7772c", 7],
  ["#56616b", 10], ["#7a2632", 8], ["#dcd6c8", 8], ["#2a6f7f", 7], ["#a88a2d", 5], ["#3b3f46", 4]];
function pickColour(r) {
  const tot = BOX_COLOURS.reduce((a, c) => a + c[1], 0); let x = r() * tot;
  for (const [c, w] of BOX_COLOURS) { if ((x -= w) <= 0) return c; }
  return BOX_COLOURS[0][0];
}
function buildContainers(H, xFrom, xTo, tiersBase, seed) {
  const r = rng(seed), g = new THREE.Group();
  const L40 = 12.19, L20 = 6.06, BW = 2.44, BH = 2.59, gapRow = .03, lash = 1.5, pitch = L40 + lash;
  const side = corrugationTex(), door = doorTex(), roof = plainTex(190);
  const mats = [door, door, roof, roof, side, side].map((t, i) =>
    new THREE.MeshStandardMaterial({map: t, bumpMap: i >= 4 ? t : null, bumpScale: .25, roughness: .72, metalness: .15}));
  const slots40 = [], slots20 = [];
  const nBays = Math.floor((xTo - xFrom) / pitch);
  const hatchMat = new THREE.MeshStandardMaterial({color: "#6d766c", roughness: .8});
  const lashMat = new THREE.MeshStandardMaterial({color: "#9aa0a4", roughness: .6, metalness: .5});
  for (let b = 0; b < nBays; b++) {
    const xc = xFrom + pitch * (b + .5);
    const s = clamp((xc + H.L / 2) / H.L, 0, 1);
    const yDeck = H.deckH(s), halfW = Math.min(H.half(s, yDeck), H.half(clamp(s + .02, 0, 1), yDeck));
    const rows = Math.max(0, Math.floor((2 * halfW - .8) / (BW + gapRow)));
    if (rows < 2) continue;
    const hatchY = yDeck + .9;
    g.add(box(L40 + .6, .9, rows * (BW + gapRow) + .6, hatchMat, xc, yDeck, 0));
    // stacks step down in the forward bays (bridge visibility line)
    const fwd = nBays - 1 - b;
    let bayTiers = tiersBase - (fwd === 0 ? 2 : fwd === 1 ? 1 : 0);
    const lashH = 2 * BH;
    // lashing bridge on the aft side of the bay
    for (const zz of [-1, 1]) g.add(box(.5, lashH, .5, lashMat, xc - L40 / 2 - lash / 2, hatchY, zz * rows * (BW + gapRow) / 2));
    g.add(box(.9, .25, rows * (BW + gapRow), lashMat, xc - L40 / 2 - lash / 2, hatchY + lashH - .25, 0));
    g.add(box(.9, .25, rows * (BW + gapRow), lashMat, xc - L40 / 2 - lash / 2, hatchY + BH - .1, 0));
    for (let row = 0; row < rows; row++) {
      const z = (row - (rows - 1) / 2) * (BW + gapRow);
      if (r() < .035) continue;                                          // an empty stack now and then
      const edge = Math.min(row, rows - 1 - row);
      let tiers = bayTiers - (edge === 0 && r() < .45 ? 1 : 0) - (r() < .18 ? Math.floor(r() * 3) : 0);
      for (let t = 0; t < Math.max(0, tiers); t++) {
        const y = hatchY + t * (BH + .02) + BH / 2;
        const c = pickColour(r);
        if (r() < .14) {                                                 // two 20 ft boxes in a 40 ft slot
          slots20.push([xc - L20 / 2 - .04, y, z, c]); slots20.push([xc + L20 / 2 + .04, y, z, pickColour(r)]);
        } else slots40.push([xc, y, z, c]);
      }
    }
  }
  const mk = (list, len) => {
    const im = new THREE.InstancedMesh(new THREE.BoxGeometry(len, BH, BW), mats, list.length);
    const m = new THREE.Matrix4(), col = new THREE.Color();
    list.forEach(([x, y, z, c], i) => {
      m.makeTranslation(x, y, z); im.setMatrixAt(i, m);
      col.set(c); col.offsetHSL(0, 0, (r() - .5) * .06); im.setColorAt(i, col);
    });
    im.castShadow = im.receiveShadow = true; return im;
  };
  if (slots40.length) g.add(mk(slots40, L40));
  if (slots20.length) g.add(mk(slots20, L20));
  return {group: g, count: slots40.length * 2 + slots20.length};
}

function buildTankerDeck(H, xFrom, xTo) {
  const g = new THREE.Group();
  const pipe = new THREE.MeshStandardMaterial({color: "#c9c3b5", roughness: .5, metalness: .5});
  const green = new THREE.MeshStandardMaterial({color: "#4f7a4a", roughness: .6, metalness: .3});
  const y = H.deckH(.5) + .1, len = xTo - xFrom, xc = (xFrom + xTo) / 2;
  [-3, -1.5, 0, 1.5, 3].forEach((z, i) => {
    const p = new THREE.Mesh(new THREE.CylinderGeometry(.35 + (i === 2 ? .2 : 0), .35, len, 10), i % 2 ? green : pipe);
    p.rotation.z = Math.PI / 2; p.position.set(xc, y + 1.6, z); p.castShadow = true; g.add(p);
  });
  g.add(box(len, .25, 1.4, pipe, xc, y + 2.6, 5));                                   // catwalk
  for (let x = xFrom; x < xTo; x += 9) g.add(box(.4, 2.6, 8, pipe, x, y, 0));        // pipe supports
  g.add(box(6, 3, H.B * .8, green, xc, y, 0));                                       // manifold
  for (const z of [-1, 1]) {                                                         // manifold cranes
    const c = new THREE.Mesh(new THREE.CylinderGeometry(.5, .6, 10, 10), pipe);
    c.position.set(xc - 5, y + 5, z * H.B * .3); g.add(c);
    const jib = box(.5, .5, 12, pipe, xc - 5, y + 9.5, z * H.B * .3 - z * 5); g.add(jib);
  }
  return g;
}

function buildBulkerDeck(H, xFrom, xTo, cranes) {
  // Hatch covers over each hold, with deck cranes between them (geared bulker
  // / general cargo ship).
  const g = new THREE.Group();
  const cover = new THREE.MeshStandardMaterial({color: "#6f7a52", roughness: .7, metalness: .2});
  const crane = new THREE.MeshStandardMaterial({color: "#d6b23a", roughness: .5, metalness: .4});
  const y = H.deckH(.5), len = xTo - xFrom;
  const holds = Math.max(3, Math.round(len / 28)), pitch = len / holds;
  for (let i = 0; i < holds; i++) {
    const xc = xFrom + pitch * (i + .5), s = clamp((xc + H.L / 2) / H.L, 0, 1);
    const w = Math.min(H.half(s, H.deckH(s)), H.half(clamp(s + .03, 0, 1), H.deckH(s))) * 2 * .72;
    if (w < 2) continue;
    g.add(box(pitch * .72, 1.8, w, cover, xc, H.deckH(s), 0));
    if (cranes && i < holds - 1) {
      const xk = xc + pitch / 2, yk = H.deckH(clamp((xk + H.L / 2) / H.L, 0, 1));
      const post = new THREE.Mesh(new THREE.CylinderGeometry(.9, 1.1, 12, 10), crane);
      post.position.set(xk, yk + 6, H.B * .18 * (i % 2 ? 1 : -1)); post.castShadow = true; g.add(post);
      const jib = box(22, .8, .8, crane, xk - 9, yk + 11, H.B * .18 * (i % 2 ? 1 : -1));
      jib.rotation.z = .35; g.add(jib);
    }
  }
  return g;
}

/* --------------------------------------------------------------- wake */
function ribbon(pts, widths, fade, uvLen) {
  // A flat strip on the water following pts (x,z), with per-point half-width and fade.
  const p = [], uv = [], c = [], ii = [];
  let acc = 0;
  pts.forEach((q, i) => {
    if (i) acc += Math.hypot(q[0] - pts[i - 1][0], q[1] - pts[i - 1][1]);
    const nx = i < pts.length - 1 ? pts[i + 1][0] - q[0] : q[0] - pts[i - 1][0];
    const nz = i < pts.length - 1 ? pts[i + 1][1] - q[1] : q[1] - pts[i - 1][1];
    const l = Math.hypot(nx, nz) || 1, ox = -nz / l * widths[i], oz = nx / l * widths[i];
    p.push(q[0] - ox, .07, q[1] - oz, q[0] + ox, .07, q[1] + oz);
    uv.push(0, acc / uvLen, 1, acc / uvLen);
    c.push(fade[i], fade[i], fade[i], fade[i], fade[i], fade[i]);
  });
  for (let i = 0; i < pts.length - 1; i++) { const a = i * 2; ii.push(a, a + 1, a + 2, a + 1, a + 3, a + 2); }
  const g = new THREE.BufferGeometry();
  g.setAttribute("position", new THREE.Float32BufferAttribute(p, 3));
  g.setAttribute("uv", new THREE.Float32BufferAttribute(uv, 2));
  g.setAttribute("color", new THREE.Float32BufferAttribute(c, 3));
  g.setIndex(ii); return g;
}
function foamMaterial(foam) {
  // Additive white, modulated by the foam texture and the per-vertex fade.
  return new THREE.ShaderMaterial({
    transparent: true, depthWrite: false, side: THREE.DoubleSide, blending: THREE.AdditiveBlending,
    uniforms: {map: {value: foam}, offset: {value: 0}, opacity: {value: .8}},
    vertexShader: `attribute vec3 color; varying vec2 vUv; varying float vF;
      void main(){ vUv = uv; vF = color.r; gl_Position = projectionMatrix * modelViewMatrix * vec4(position,1.); }`,
    fragmentShader: `uniform sampler2D map; uniform float offset; uniform float opacity;
      varying vec2 vUv; varying float vF;
      void main(){ float f = texture2D(map, vec2(vUv.x, vUv.y + offset)).g;
        float edge = smoothstep(0., .35, vUv.x) * smoothstep(1., .65, vUv.x);
        gl_FragColor = vec4(vec3(1.), 1.) * f * vF * opacity * (.35 + .65 * edge); }`,
  });
}
function buildWake(H, foam) {
  const g = new THREE.Group(), mats = [];
  const xS = -H.L / 2, n = 60;
  // turbulent propeller wash straight astern
  const wash = [], ww = [], wf = [];
  for (let i = 0; i <= n; i++) {
    const t = i / n; wash.push([xS + 4 - t * H.L * 2.2, 0]);
    ww.push(H.B * (.28 + 1.1 * Math.pow(t, .7))); wf.push(Math.pow(1 - t, 1.3));
  }
  const m1 = foamMaterial(foam); mats.push(m1);
  g.add(new THREE.Mesh(ribbon(wash, ww, wf, 60), m1));
  // Kelvin arms: two thin lines diverging at about 19.5 degrees from the stern
  const ang = 19.5 * Math.PI / 180;
  for (const side of [1, -1]) {
    const arm = [], aw = [], af = [];
    for (let i = 0; i <= n; i++) {
      const t = i / n, d = t * H.L * 2.4;
      arm.push([xS + H.L * .25 - d * Math.cos(ang), side * (H.B * .5 + d * Math.sin(ang))]);
      aw.push(2 + 9 * t); af.push(Math.pow(1 - t, 1.1) * .8);
    }
    const m = foamMaterial(foam); mats.push(m);
    g.add(new THREE.Mesh(ribbon(arm, aw, af, 50), m));
  }
  // bow wave hugging the waterline from the stem aft
  for (const side of [1, -1]) {
    const pts = [], w = [], f = [], nb = 40;
    for (let i = 0; i <= nb; i++) {
      const t = i / nb, s = lerp(.995, .6, t), h = H.T;
      const x = lerp(H.xStern(h), H.xStem(h), s), half = H.half(s, h);
      const out = 1.5 + 6 * t;
      pts.push([x, side * (half + out * .55)]); w.push(out * .55); f.push(Math.pow(1 - t, .7));
    }
    const m = foamMaterial(foam); mats.push(m);
    g.add(new THREE.Mesh(ribbon(pts, w, f, 25), m));
  }
  g.children.forEach(c => { c.renderOrder = 2; });
  return {group: g, mats};
}

function buildAnchorChain(H) {
  const s = .975, h = H.deckH(s) - 1.5;
  const x = lerp(H.xStern(h), H.xStem(h), s), z = H.half(s, h) + .3;
  const curve = new THREE.CatmullRomCurve3([
    new THREE.Vector3(x, h, z), new THREE.Vector3(x + 6, h * .45, z + 1),
    new THREE.Vector3(x + 14, -H.T * .5, z + 2), new THREE.Vector3(x + 22, -H.T * 2, z + 2)]);
  const m = new THREE.Mesh(new THREE.TubeGeometry(curve, 40, .35, 6),
    new THREE.MeshStandardMaterial({color: "#2b2b2b", roughness: .9, metalness: .4}));
  m.position.y = 0; return m;
}

/* ------------------------------------------------------------ the ship */
function buildShip(v) {
  const H = hullModel(v);
  const tanker = H.tanker, bulker = v.hull_type === "bulker";
  const lights = [];
  const colours = v.colours || (bulker
    ? {topside: "#2d3a2a", bottom: "#8a2e24", deck: "#6d5a3c", stripe: false}
    : tanker
    ? {topside: "#3c2b2b", bottom: "#7d2a24", deck: "#7a3b2f", stripe: false}
    : {topside: "#223a5c", bottom: "#9a2f25", deck: "#56615d", stripe: true});
  const root = new THREE.Group();
  const hull = buildHull(H, colours); root.add(hull);

  // Layout: big container ships (>= 350 m) carry the bridge forward with the
  // funnel aft; everything else has accommodation and funnel together aft.
  const decks = Math.round(clamp(H.L / 34, 5, 10)) + (tanker ? 0 : 1);
  const forwardBridge = !tanker && H.L >= 350;
  const accX = forwardBridge ? H.L * .12 : -H.L * .5 + H.L * .13;
  const acc = buildAccommodation(H, accX, decks, lights); root.add(acc.group);
  const funL = clamp(H.L * .035, 8, 16);
  const funX = forwardBridge ? -H.L * .5 + H.L * .1 : acc.aft - funL * .55;
  root.add(buildFunnel(H, funX, acc.bridgeTop + (forwardBridge ? -2 : 4)));
  root.add(buildLifeboat(H));
  root.add(buildForecastle(H, colours, acc.lamp));

  const cargoStart = forwardBridge ? funX + funL : acc.fore + 2;
  const cargoEnd = lerp(H.xStern(H.D), H.xStem(H.D), .852) - 1;
  let boxes = 0;
  if (bulker) root.add(buildBulkerDeck(H, acc.fore + 4, cargoEnd, v.cranes !== false));
  else if (tanker) root.add(buildTankerDeck(H, acc.fore + 4, cargoEnd));
  else {
    const tiers = Math.round(clamp(H.D / 3.6, 3, 9));
    if (forwardBridge) {
      const a = buildContainers(H, cargoStart, acc.aft - 2, tiers, 21);
      const b = buildContainers(H, acc.fore + 2, cargoEnd, tiers - 1, 22);
      root.add(a.group, b.group); boxes = a.count + b.count;
    } else {
      const a = buildContainers(H, cargoStart, cargoEnd, tiers, 21);
      root.add(a.group); boxes = a.count;
    }
  }
  root.position.y = -H.T;              // float at the design draught
  return {root, H, lights, boxes};
}

/* ------------------------------------------------------------ the view */
function sunFromTime(hour, lat) {
  // Equinox approximation: declination 0, hour angle from local solar time.
  const Hr = (hour - 12) * 15 * Math.PI / 180, ph = lat * Math.PI / 180;
  const el = Math.asin(clamp(Math.cos(ph) * Math.cos(Hr), -1, 1));
  let az = Math.atan2(Math.sin(Hr), Math.cos(Hr) * Math.sin(ph));   // from south, +west
  az = az + Math.PI;                                                // from north, clockwise
  return {el, az};
}

/* ------------------------------------------------- neighbours & rings */
const NM_M = 1852;
function neighbourSpec(n) {
  // Map an AIS/snapshot ship onto a hull family and give it plausible depth.
  const t = n.ship_type;
  const hull = t === "container" ? "container" : (t === "tanker" || t === "gas") ? "tanker" : "bulker";
  const L = Math.max(30, n.loa_m), B = Math.max(8, n.beam_m);
  return {code: `${hull}-${Math.round(L)}-${Math.round(B)}`, hull_type: hull, loa_m: L, beam_m: B,
          draught_m: Math.max(3, Math.min(n.draught_m || L / 20, 22)),
          moulded_depth_m: Math.max(4, Math.min(L / 11.5, 33)), teu: 0,
          cranes: t !== "service" && t !== "passenger",
          colours: t === "gas" ? {topside: "#e8e6df", bottom: "#8a2e24", deck: "#7a7d80", stripe: false}
                 : t === "service" ? {topside: "#e36b1c", bottom: "#2d2d2d", deck: "#5a5a5a", stripe: false}
                 : null};
}
function textSprite(lines, colour = "#ffffff", bg = "rgba(8,16,24,.72)") {
  const c = document.createElement("canvas"), g = c.getContext("2d"), fs = 30;
  g.font = `600 ${fs}px IBM Plex Mono, monospace`;
  const w = Math.max(...lines.map(l => g.measureText(l).width)) + 28;
  c.width = Math.ceil(w); c.height = lines.length * (fs + 8) + 16;
  g.font = `600 ${fs}px IBM Plex Mono, monospace`;
  g.fillStyle = bg; g.beginPath(); g.roundRect(0, 0, c.width, c.height, 12); g.fill();
  lines.forEach((l, i) => { g.fillStyle = i ? "#c9d6de" : colour; g.fillText(l, 14, 12 + fs + i * (fs + 8) - 4); });
  const t = new THREE.CanvasTexture(c); t.colorSpace = THREE.SRGBColorSpace;
  const m = new THREE.SpriteMaterial({map: t, sizeAttenuation: false, depthTest: false, transparent: true});
  const sp = new THREE.Sprite(m); sp.renderOrder = 10;
  const k = .00030; sp.scale.set(c.width * k, c.height * k, 1); sp.center.set(.5, 0);
  return sp;
}
function buildRings() {
  const g = new THREE.Group();
  [[.1, "0.1 NM"], [.25, "0.25 NM"], [.5, "0.5 NM"], [1, "1 NM"]].forEach(([nm, lab]) => {
    const r = nm * NM_M, w = Math.max(1.6, r * .006);
    const ring = new THREE.Mesh(new THREE.RingGeometry(r - w, r + w, 160),
      new THREE.MeshBasicMaterial({color: 0xffffff, transparent: true, opacity: .55, side: THREE.DoubleSide,
                                   depthWrite: false, fog: false}));
    ring.rotation.x = -Math.PI / 2; ring.position.y = .4; ring.renderOrder = 3; g.add(ring);
    const sp = textSprite([lab], "#ffffff", "rgba(8,16,24,.5)");
    sp.scale.multiplyScalar(.8); sp.position.set(0, 2, -r); g.add(sp);
  });
  return g;
}

export function createShipView(container) {
  let renderer;
  try {
    renderer = new THREE.WebGLRenderer({antialias: true, powerPreference: "high-performance"});
  } catch (e) {
    container.innerHTML = '<p class="note" style="padding:12px">3D view needs WebGL, which this browser has turned off.</p>';
    return {setParams() {}, attach() {}, dispose() {}, view() {}};
  }
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 1.5));
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.shadowMap.enabled = true; renderer.shadowMap.type = THREE.PCFSoftShadowMap;
  renderer.domElement.className = "ship3d-canvas";

  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(34, 1, 1, 60000);
  const controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true; controls.dampingFactor = .08;
  controls.maxPolarAngle = Math.PI * .485; controls.enablePan = false;

  const sky = new Sky(); sky.scale.setScalar(45000); scene.add(sky);
  const su = sky.material.uniforms;
  su.turbidity.value = 2.6; su.rayleigh.value = 1.9; su.mieCoefficient.value = .004; su.mieDirectionalG.value = .85;
  // Night dome: fades the Sky shader (which goes a muddy brown below the
  // horizon) into a deep navy night sky.
  const dome = new THREE.Mesh(new THREE.SphereGeometry(30000, 32, 16),
    new THREE.MeshBasicMaterial({color: 0x050b17, side: THREE.BackSide, transparent: true, opacity: 0,
                                 depthWrite: false, fog: false}));
  dome.renderOrder = -1; scene.add(dome);

  const water = new Water(new THREE.PlaneGeometry(40000, 40000), {
    textureWidth: 512, textureHeight: 512, waterNormals: waterNormals(),
    sunDirection: new THREE.Vector3(), sunColor: 0xffffff, waterColor: 0x0b3a52,
    distortionScale: 3.0, fog: false});
  water.rotation.x = -Math.PI / 2; water.material.uniforms.size.value = 1.6; scene.add(water);

  const sun = new THREE.DirectionalLight(0xffffff, 2.2); sun.castShadow = true;
  sun.shadow.mapSize.set(2048, 2048); sun.shadow.bias = -.0004; sun.shadow.normalBias = .6;
  scene.add(sun, sun.target);
  const hemi = new THREE.HemisphereLight(0xbcd8ff, 0x0a2233, .5); scene.add(hemi);
  const moon = new THREE.DirectionalLight(0x9db4e0, 0); scene.add(moon);
  const moonDir = new THREE.Vector3(.45, .55, -.7).normalize();

  // stars for night
  const starGeo = new THREE.BufferGeometry(), sp = [], r = rng(5);
  for (let i = 0; i < 1500; i++) {
    const th = r() * Math.PI * 2, ph = Math.acos(r() * .95);
    sp.push(Math.sin(ph) * Math.cos(th) * 20000, Math.cos(ph) * 20000, Math.sin(ph) * Math.sin(th) * 20000);
  }
  starGeo.setAttribute("position", new THREE.Float32BufferAttribute(sp, 3));
  const stars = new THREE.Points(starGeo, new THREE.PointsMaterial({color: 0xffffff, size: 1.6, sizeAttenuation: false,
                                                                     transparent: true, opacity: 0}));
  scene.add(stars);

  const pmrem = new THREE.PMREMGenerator(renderer);
  let envRT = null;
  const foam = foamTex();

  let ship = null, wake = null, chain = null, vesselCode = null;
  let P = {sog: 12, heading: 90, lat: 1.2, lon: 103.8, utcHour: 4, anchored: false};
  let visible = true, current = "quarter";
  const rings = buildRings(); scene.add(rings);
  const others = new THREE.Group(); scene.add(others);
  const templates = new Map();          // spec.code -> built ship (shared geometry)
  let neighbourKey = "", nearR = 900;

  function setNeighbours(list) {
    const key = (list || []).map(n => `${n.id}:${Math.round(n.east_m)}:${Math.round(n.north_m)}:${Math.round(P.heading)}`).join("|");
    if (key === neighbourKey) return;
    neighbourKey = key;
    while (others.children.length) others.remove(others.children[0]);
    const h = P.heading * Math.PI / 180;
    const ds = (list || []).slice(0, 3).map(n => Math.hypot(n.east_m, n.north_m) + n.loa_m / 2);
    nearR = ds.length ? Math.max(...ds) : 900;
    (list || []).forEach((n, i) => {
      const spec = neighbourSpec(n);
      if (!templates.has(spec.code)) templates.set(spec.code, buildShip(spec));
      const tpl = templates.get(spec.code);
      const obj = tpl.root.clone();
      const fwd = n.north_m * Math.cos(h) + n.east_m * Math.sin(h);
      const stbd = n.east_m * Math.cos(h) - n.north_m * Math.sin(h);
      obj.position.set(fwd, -tpl.H.T, stbd);
      obj.rotation.y = -((n.heading - P.heading) * Math.PI / 180);
      others.add(obj);
      const col = n.cls === "bad" ? "#ff8a7a" : n.cls === "warn" ? "#ffc56b" : "#ffffff";
      // a ring on the water round each ship keeps it findable from far away
      const rr = n.loa_m / 2 + 25;
      const halo = new THREE.Mesh(new THREE.RingGeometry(rr, rr + Math.max(4, rr * .08), 48),
        new THREE.MeshBasicMaterial({color: col, transparent: true, opacity: .7, side: THREE.DoubleSide,
                                     depthWrite: false, fog: false}));
      halo.rotation.x = -Math.PI / 2; halo.position.set(fwd, .5, stbd); halo.renderOrder = 3; others.add(halo);
      if (i < 5) {
        const sp = textSprite([`${(n.hull_nm).toFixed(2)} NM · ${Math.round(n.hull_nm * NM_M)} m`,
                               `${n.ship_type} ${Math.round(n.loa_m)} m`], col);
        sp.position.set(fwd, tpl.H.D * 1.9 + 8, stbd); others.add(sp);
        if (i < 3) {
          const geo = new THREE.BufferGeometry().setFromPoints([new THREE.Vector3(0, 1, 0), new THREE.Vector3(fwd, 1, stbd)]);
          const ln = new THREE.Line(geo, new THREE.LineDashedMaterial({color: col, dashSize: 12, gapSize: 8,
                                                                       transparent: true, opacity: .9, depthWrite: false}));
          ln.computeLineDistances(); ln.renderOrder = 4; others.add(ln);
        }
      }
      tpl.lights.forEach(m => { if (!ship.lights.includes(m)) ship.lights.push(m); });
    });
  }

  function frame(preset) {
    if (!ship) return;
    const L = ship.H.L;
    const tgt = new THREE.Vector3(0, ship.H.D * .45, 0);
    if (preset === "surroundings" || preset === "bridge") {
      current = preset;
      if (preset === "surroundings") {
        // high oblique view over the stern that takes in the 1 NM ring
        // frame the nearest ships: fit a circle of radius R round own ship
        const R = clamp(nearR * 1.15, 650, 4200);
        tgt.set(0, 0, 0);
        const dir = new THREE.Vector3(-.42, .82, .38).normalize();
        camera.position.copy(dir.multiplyScalar(R / Math.tan(camera.fov * Math.PI / 360) * .95));
      } else {
        // eye on the bridge wing looking ahead
        tgt.set(L * 3, ship.H.D, 0);
        camera.position.set(-L * .32, ship.H.D * 1.55 + 6, ship.H.B * .45);
      }
      controls.target.copy(tgt);
      controls.minDistance = 5; controls.maxDistance = 9000;
      camera.near = 2; camera.far = 60000; camera.updateProjectionMatrix(); controls.update();
      const s0 = sun.shadow.camera, ext = L * .62;
      s0.left = -ext; s0.right = ext; s0.top = ext; s0.bottom = -ext; s0.near = 1; s0.far = L * 4; s0.updateProjectionMatrix();
      return;
    }
    const dirs = {quarter: [.55, .2, .81], side: [.02, .1, 1], bow: [.93, .16, .33], aerial: [.25, .82, .52]};
    const d = new THREE.Vector3(...(dirs[preset] || dirs.quarter)).normalize();
    // Fit the ship's bounding box to ~86% of the frame: project its corners
    // and rescale the camera distance until the widest one sits at the margin.
    const H0 = ship.H, top = H0.D * 1.9 - H0.T;
    const corners = [];
    for (const x of [-L / 2, L / 2]) for (const y of [-H0.T * .3, top]) for (const z of [-H0.B / 2, H0.B / 2])
      corners.push(new THREE.Vector3(x, y, z));
    let dist = L;
    for (let it = 0; it < 6; it++) {
      camera.position.copy(tgt).addScaledVector(d, dist); camera.lookAt(tgt); camera.updateMatrixWorld();
      camera.updateProjectionMatrix();
      let m = 0;
      corners.forEach(c => { const p = c.clone().project(camera); m = Math.max(m, Math.abs(p.x), Math.abs(p.y)); });
      dist *= m / .86;
    }
    camera.position.copy(tgt).addScaledVector(d, dist); controls.target.copy(tgt);
    controls.minDistance = L * .2; controls.maxDistance = 9000;
    current = preset || "quarter";
    camera.near = L * .01; camera.far = 60000; camera.updateProjectionMatrix(); controls.update();
    const s = sun.shadow.camera, ext = L * .62;
    s.left = -ext; s.right = ext; s.top = ext; s.bottom = -ext; s.near = 1; s.far = L * 4; s.updateProjectionMatrix();
  }

  function setVessel(v) {
    if (ship) { scene.remove(ship.root); if (wake) scene.remove(wake.group); if (chain) scene.remove(chain); }
    ship = buildShip(v); scene.add(ship.root);
    wake = buildWake(ship.H, foam); scene.add(wake.group);
    chain = buildAnchorChain(ship.H); chain.position.y = -ship.H.T; scene.add(chain);
    vesselCode = v.code; frame("quarter");
  }

  function applyEnvironment() {
    if (!ship) return;
    const solar = ((P.utcHour + P.lon / 15) % 24 + 24) % 24;
    const {el, az} = sunFromTime(solar, P.lat);
    // scene frame: +x = bow. Rotate the compass azimuth into the ship's frame.
    const rel = az - P.heading * Math.PI / 180;
    const dir = new THREE.Vector3(Math.cos(rel) * Math.cos(el), Math.sin(el), Math.sin(rel) * Math.cos(el));
    su.sunPosition.value.copy(dir);
    const day = smooth(-.12, .12, el);                 // 0 night .. 1 day
    // the sea glitters with the sun by day and the moon by night
    water.material.uniforms.sunDirection.value.copy(day > .5 ? dir : moonDir).normalize();
    dome.material.opacity = 1 - smooth(-.15, .04, el);
    moon.position.copy(moonDir).multiplyScalar(ship.H.L * 2);
    moon.intensity = .9 * (1 - day);
    const low = 1 - smooth(.05, .5, el);               // warm light near the horizon
    sun.position.copy(dir.clone().multiplyScalar(ship.H.L * 1.6).setY(Math.max(dir.y, .08) * ship.H.L * 1.6));
    sun.intensity = 3.0 * day;
    sun.color.setHSL(.09, .55 * low, .92 - .15 * low);
    hemi.intensity = .3 + .3 * day;
    hemi.color.setHSL(.61, lerp(.55, .45, day), lerp(.32, .72, day));
    renderer.toneMappingExposure = lerp(.62, .42, day);
    water.material.uniforms.waterColor.value.setHSL(.56, .75, lerp(.02, .09, day));
    water.material.uniforms.sunColor.value.setRGB(lerp(.28, 1, day), lerp(.32, 1, day), lerp(.42, 1, day));
    stars.material.opacity = 1 - smooth(-.2, .02, el);
    const lit = 1 - smooth(-.08, .1, el);
    ship.lights.forEach(m => { m.emissiveIntensity = (m.map ? 1.3 : 2.5) * lit; });
    // environment map for reflections on the hull and boxes
    if (envRT) envRT.dispose();
    const envScene = new THREE.Scene(); const skyClone = sky.clone(); envScene.add(skyClone);
    envRT = pmrem.fromScene(envScene); scene.environment = envRT.texture;
    scene.environmentIntensity = lerp(.08, 1, day);
    // sea state from speed
    const anchored = P.anchored || P.sog < .5;
    water.material.uniforms.distortionScale.value = anchored ? 1.8 : 3.0;
    wake.group.visible = !anchored; chain.visible = anchored;
    const k = clamp(P.sog / 20, 0, 1);
    wake.mats.forEach(m => { m.uniforms.opacity.value = (.35 + 1.0 * k) * lerp(.4, 1, day); });
  }

  const clock = new THREE.Clock();
  let onScreen = true, last = 0;
  const io = new IntersectionObserver(es => { onScreen = es.some(e => e.isIntersecting); });
  function tick(now) {
    // ~30 fps is plenty for a slow-moving ship, and halves the GPU load;
    // nothing renders while the view is scrolled away or the tab is hidden.
    if (!visible || !onScreen || !ship || now - last < 32) return;
    last = now;
    const dt = Math.min(clock.getDelta(), .05), t = clock.elapsedTime;
    water.material.uniforms.time.value += dt * (P.anchored ? .35 : .6);
    const anchored = P.anchored || P.sog < .5;
    const amp = anchored ? .35 : .6;
    ship.root.rotation.z = Math.sin(t * .45) * .004 * amp * 3;      // pitch (about the beam axis)
    ship.root.rotation.x = Math.sin(t * .31 + 1) * .012 * amp;      // roll
    ship.root.position.y = -ship.H.T + Math.sin(t * .6) * .25 * amp;
    if (!anchored) {
      const off = t * P.sog * .012;
      wake.mats.forEach(m => { m.uniforms.offset.value = off; });
    }
    controls.update();
    renderer.render(scene, camera);
  }
  renderer.setAnimationLoop(tick);

  function resize() {
    const w = renderer.domElement.parentElement ? renderer.domElement.parentElement.clientWidth : 300;
    const h = renderer.domElement.parentElement ? renderer.domElement.parentElement.clientHeight : 200;
    if (!w || !h) return;
    renderer.setSize(w, h, false);
    renderer.domElement.style.width = w + "px"; renderer.domElement.style.height = h + "px";
    camera.aspect = w / h; camera.updateProjectionMatrix();
    if (ship) frame(current);
  }
  const ro = new ResizeObserver(resize);
  function attach(el) {
    if (renderer.domElement.parentElement) ro.unobserve(renderer.domElement.parentElement);
    el.prepend(renderer.domElement); ro.observe(el); io.disconnect(); io.observe(el); resize();
  }
  attach(container);
  document.addEventListener("visibilitychange", () => { visible = !document.hidden; });

  return {
    setParams(p) {
      P = Object.assign({}, P, p);
      if (p.vessel && p.vessel.code !== vesselCode) { setVessel(p.vessel); neighbourKey = ""; }
      if (p.neighbours) { const k0 = neighbourKey; setNeighbours(p.neighbours);
        if (k0 !== neighbourKey && current === "surroundings") frame("surroundings"); }
      applyEnvironment();
    },
    attach,
    view(preset) { frame(preset); },
    setVisible(v) { visible = v; },
    get boxes() { return ship ? ship.boxes : 0; },
    dispose() { renderer.setAnimationLoop(null); ro.disconnect(); io.disconnect(); renderer.dispose(); },
  };
}
