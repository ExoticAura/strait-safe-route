/* Strait Safe Route - the sea.
 *
 * A wave-displaced ocean in place of three.js's flat mirror plane:
 *
 *  waves    16 Gerstner wave trains (one per frequency band, each in its own
 *           direction within +-60 deg of the wind, short waves spread widest) from a JONSWAP spectrum for a
 *           fetch-limited sea (Singapore Strait: sheltered, 40 km fetch,
 *           short steep wind chop, no ocean swell). Sea state sets the wind:
 *           calm 3 m/s, moderate 7 m/s, fresh 10 m/s -> Hs 0.31 / 0.72 /
 *           1.03 m, peak periods 3.1 / 4.1 / 4.6 s. Deep-water dispersion w^2 = g k.
 *  mesh     a radial grid that follows the camera's look point, fine near
 *           it (1.5 m) and coarse towards the horizon (45 km), so the vertex
 *           count is fixed wherever you look. Each wave fades out where the
 *           grid is too coarse to carry it. The wave field itself is fixed to
 *           the Earth: moving the grid never moves the waves.
 *  shading  three.js physical material: Fresnel from water's index of
 *           refraction (1.333, F0 ~ 0.02), soft sky reflection from the
 *           environment map, GGX sun glint, turbid green-grey body colour
 *           with light scattering up through the crests, detail ripples from
 *           two world-anchored normal maps, whitecaps where the surface
 *           folds (Jacobian of the Gerstner displacement).
 *  buoyancy heightAt() evaluates the same waves on the CPU, so ships, the
 *           wake and the range rings ride the surface that is drawn.
 */
import * as THREE from "three";

export const SEA_STATES = {                      // steep: Gerstner crest sharpness
  calm:     {label: "Calm · Beaufort 2", wind: 3.0, steep: .4},
  moderate: {label: "Moderate · Beaufort 4", wind: 7.0, steep: .7},
  fresh:    {label: "Fresh · Beaufort 5", wind: 10.0, steep: .85},
};
const G = 9.81, N = 16, TAU = Math.PI * 2;
const GRID_S0 = 1.5, GRID_G = 1.02, GRID_R = 45000, GRID_A = 384;
// direction scatter of the 16 wave trains about the mean wind (deg), fixed so
// the sea looks the same every time
const SPREAD = [-12, 23, -37, 8, 41, -26, 15, -48, 31, -5, 52, -19, 36, -44, 27, -58];

/* JONSWAP spectrum for wind speed U (m/s) over fetch F (m), discretised into
   16 frequency bands. Returns wave trains and summary figures. */
export function jonswapWaves(U, fetch = 40000, windFromDeg = 45, steep = .55) {
  const X = G * fetch / (U * U);
  const wp = 22 * Math.pow(G * G / (U * fetch), 1 / 3);          // peak angular frequency
  const alpha = .076 * Math.pow(X, -.22), gamma = 3.3;
  const S = w => {
    const s = w <= wp ? .07 : .09;
    const r = Math.exp(-((w - wp) ** 2) / (2 * s * s * wp * wp));
    return alpha * G * G * Math.pow(w, -5) * Math.exp(-1.25 * Math.pow(wp / w, 4)) * Math.pow(gamma, r);
  };
  // 16 frequency bands, one wave train each, every train in its own direction
  // (pairs of trains at one frequency beat into a visible cross-hatch)
  const w0 = .7 * wp, w1 = 4.5 * wp, nb = N, edge = j => w0 * Math.pow(w1 / w0, j / nb);
  const toward = (windFromDeg + 180) * Math.PI / 180;
  const waves = [];
  let m0 = 0;
  for (let j = 0; j < nb; j++) {
    const w = edge(j + .5) * (1 + .03 * Math.sin(j * 12.9898)), dw = edge(j + 1) - edge(j);
    const A = Math.sqrt(2 * S(w) * dw) * (1 + .18 * Math.sin(j * 78.233)), k = w * w / G;
    m0 += A * A / 2;
    const b = toward + SPREAD[j] * Math.PI / 180;
    // scene frame: x east, z south
    waves.push({A, k, w, dx: Math.sin(b), dz: -Math.cos(b), Q: 0});
  }
  // match the fetch-limited JONSWAP wave height, gHs/U^2 = 1.6e-3 (gF/U^2)^0.5
  // (the discretisation slightly over-counts the tail)
  const hs = 1.6e-3 * Math.sqrt(X) * U * U / G, f = hs / (4 * Math.sqrt(m0));
  waves.forEach(v => { v.A *= f; v.Q = Math.min(1, steep / (v.k * v.A * N)); });
  return {waves, hs, tp: TAU / wp, lp: TAU / (wp * wp / G)};
}

// GLSL shared by the ocean, the wake and anything that floats on the surface
export const SEA_GLSL = /* glsl */`
#define SEA_N ${N}
uniform vec2 uWaveDir[SEA_N];
uniform float uWaveK[SEA_N];
uniform float uWaveA[SEA_N];
uniform float uWaveQ[SEA_N];
uniform float uWavePh[SEA_N];
uniform vec2 uSeaCentre;
uniform vec2 uSeaGrid;
float seaFade(float k, vec2 p) {
  float s = uSeaGrid.x + uSeaGrid.y * length(p - uSeaCentre);
  return clamp((6.2831853 / k / s - 1.5) / 1.5, 0.0, 1.0);
}
vec3 seaDisplace(vec2 p, out vec3 n, out float jac) {
  vec3 d = vec3(0.0), nn = vec3(0.0, 1.0, 0.0);
  jac = 1.0;
  for (int i = 0; i < SEA_N; i++) {
    float A = uWaveA[i] * seaFade(uWaveK[i], p), k = uWaveK[i];
    vec2 D = uWaveDir[i];
    float th = k * dot(D, p) + uWavePh[i], s = sin(th), c = cos(th);
    float QA = uWaveQ[i] * A, WA = k * A;
    d += vec3(QA * D.x * c, A * s, QA * D.y * c);
    nn -= vec3(D.x * WA * c, uWaveQ[i] * WA * s, D.y * WA * c);
    jac -= uWaveQ[i] * WA * s;
  }
  n = normalize(nn);
  return d;
}
// shading normal and fold (whitecaps) per pixel: every wave the pixel can
// resolve, whatever the mesh carries, so there is no level-of-detail ring
vec3 seaNormalPix(vec2 p, float foot, out float jac) {
  vec3 nn = vec3(0.0, 1.0, 0.0);
  jac = 1.0;
  for (int i = 0; i < SEA_N; i++) {
    float k = uWaveK[i];
    float A = uWaveA[i] * clamp((6.2831853 / k / foot - 3.0) / 3.0, 0.0, 1.0);
    vec2 D = uWaveDir[i];
    float th = k * dot(D, p) + uWavePh[i], s = sin(th), c = cos(th), WA = k * A;
    nn -= vec3(D.x * WA * c, uWaveQ[i] * WA * s, D.y * WA * c);
    jac -= uWaveQ[i] * WA * s;
  }
  return normalize(nn);
}
float seaHeight(vec2 p) {                 // surface height above scene point p
  vec3 n; float j;
  vec3 d = seaDisplace(p, n, j);
  d = seaDisplace(p - d.xz, n, j);
  return d.y;
}
`;

function radialGrid() {
  const radii = [0];
  let r = 0, j = 0;
  while (r < GRID_R) { j++; r = GRID_S0 * (Math.pow(GRID_G, j) - 1) / (GRID_G - 1); radii.push(r); }
  const pos = [0, 0, 0], idx = [];
  for (let i = 1; i < radii.length; i++)
    for (let a = 0; a < GRID_A; a++) {
      const t = a / GRID_A * TAU;
      pos.push(Math.cos(t) * radii[i], 0, Math.sin(t) * radii[i]);
    }
  const at = (i, a) => i === 0 ? 0 : 1 + (i - 1) * GRID_A + (a % GRID_A);
  for (let i = 0; i < radii.length - 1; i++)
    for (let a = 0; a < GRID_A; a++) {
      if (i === 0) { idx.push(0, at(1, a + 1), at(1, a)); continue; }
      const p = at(i, a), q = at(i, a + 1), s = at(i + 1, a), u = at(i + 1, a + 1);
      idx.push(p, q, s, q, u, s);
    }
  const g = new THREE.BufferGeometry();
  g.setAttribute("position", new THREE.Float32BufferAttribute(pos, 3));
  g.setAttribute("normal", new THREE.Float32BufferAttribute(new Float32Array(pos.length).map((_, i) => i % 3 === 1 ? 1 : 0), 3));
  g.setIndex(new THREE.Uint32BufferAttribute(idx, 1));
  g.boundingSphere = new THREE.Sphere(new THREE.Vector3(), GRID_R * 1.5);
  return g;
}

export function createOcean({detailNormals}) {
  const U = {
    uWaveDir: {value: Array.from({length: N}, () => new THREE.Vector2())},
    uWaveK: {value: new Array(N).fill(1)},
    uWaveA: {value: new Array(N).fill(0)},
    uWaveQ: {value: new Array(N).fill(0)},
    uWavePh: {value: new Array(N).fill(0)},
    uSeaCentre: {value: new THREE.Vector2()},
    uSeaGrid: {value: new THREE.Vector2(GRID_S0, GRID_G - 1)},
  };
  const F = {                                     // ocean-only uniforms
    uDetail: {value: detailNormals},
    uDetailOff: {value: new THREE.Vector4()},
    uDetailStr: {value: .2},
    uScatter: {value: 1},
    uScatterCol: {value: new THREE.Color("#35573f")},
    uFoamAmt: {value: 1},
    uDepthTex: {value: null},
    uDepthRect: {value: new THREE.Vector4(0, 0, 1, 1)},     // earth x0, z0 (north edge), width, height in metres
    uDepthOn: {value: 0},
    uDepthScale: {value: 60 / 255},
    uEarth: {value: new THREE.Vector2()},
  };
  let waves = [], info = {}, t = 0, windFrom = 45;

  const mat = new THREE.MeshPhysicalMaterial({
    color: new THREE.Color("#203a30"), roughness: .09, metalness: 0,
    ior: 1.333, specularIntensity: 1, envMapIntensity: 1,
  });
  mat.onBeforeCompile = sh => {
    Object.assign(sh.uniforms, U, F);
    sh.vertexShader = sh.vertexShader
      .replace("#include <common>", "#include <common>\n" + SEA_GLSL +
               "varying vec2 vSeaP;\nvarying float vCrest;\nvarying vec2 vSeaXZ;")
      .replace("#include <beginnormal_vertex>", `
        vec3 seaN; float seaJ;
        vec2 seaP = (modelMatrix * vec4(position, 1.0)).xz;
        vec3 seaD = seaDisplace(seaP, seaN, seaJ);
        vec3 objectNormal = seaN;
        vSeaP = seaP;
        vCrest = seaD.y;
        vSeaXZ = seaP + seaD.xz;`)
      .replace("#include <begin_vertex>", "vec3 transformed = position + seaD;");
    sh.fragmentShader = sh.fragmentShader
      .replace("#include <common>", `#include <common>
        ${SEA_GLSL}
        uniform sampler2D uDetail; uniform vec4 uDetailOff; uniform float uDetailStr;
        uniform float uScatter; uniform vec3 uScatterCol; uniform float uFoamAmt;
        uniform sampler2D uDepthTex; uniform vec4 uDepthRect; uniform float uDepthOn, uDepthScale;
        uniform vec2 uEarth;
        varying vec2 vSeaP; varying float vCrest; varying vec2 vSeaXZ;
        float seaFoam;`)
      .replace("#include <color_fragment>", `#include <color_fragment>
        // wave normal and whitecaps per pixel (not per vertex): no grid pattern
        float seaJ;
        vec2 fw = fwidth(vSeaP);
        vec3 seaNw = seaNormalPix(vSeaP, max(max(fw.x, fw.y), 0.05), seaJ);
        seaFoam = smoothstep(0.6, 0.15, seaJ) * uFoamAmt;
        diffuseColor.rgb = mix(diffuseColor.rgb, vec3(0.86, 0.9, 0.9), seaFoam);
        // sea depth (GEBCO + dredged pockets): shallow water is greener and sandier
        if (uDepthOn > 0.5) {
          vec2 eP = vSeaP + uEarth;
          vec2 duv = vec2((eP.x - uDepthRect.x) / uDepthRect.z, (eP.y - uDepthRect.y) / uDepthRect.w);
          if (duv.x > 0.0 && duv.x < 1.0 && duv.y > 0.0 && duv.y < 1.0) {
            float dm = texture2D(uDepthTex, duv).r * 255.0 * uDepthScale;
            float sh = 1.0 - smoothstep(3.0, 30.0, dm);
            diffuseColor.rgb = mix(diffuseColor.rgb, vec3(0.20, 0.36, 0.31), sh * 0.38 * (1.0 - seaFoam));
          }
        }`)
      .replace("#include <roughnessmap_fragment>", `#include <roughnessmap_fragment>
        roughnessFactor = mix(roughnessFactor, 0.8, seaFoam);`)
      .replace("#include <normal_fragment_maps>", `#include <normal_fragment_maps>
        {
          normal = normalize((viewMatrix * vec4(seaNw, 0.0)).xyz);
          // ripples the grid cannot carry: two world-anchored normal maps at
          // unrelated scales and angles, fading with distance (no tiling)
          vec2 q1 = mat2(0.825, 0.565, -0.565, 0.825) * vSeaXZ / 19.7 + uDetailOff.xy;
          vec2 q2 = mat2(0.454, -0.891, 0.891, 0.454) * vSeaXZ / 53.3 + uDetailOff.zw;
          vec3 t1 = texture2D(uDetail, q1).xyz * 2.0 - 1.0;
          vec3 t2 = texture2D(uDetail, q2).xyz * 2.0 - 1.0;
          float fall = 1.0 / (1.0 + length(vViewPosition) / 350.0);
          vec2 dd = (t1.xy * 0.55 + t2.xy * 0.45) * uDetailStr * fall * (1.0 - seaFoam);
          normal = normalize(normal + (viewMatrix * vec4(dd.x, 0.0, dd.y, 0.0)).xyz);
        }`)
      .replace("#include <emissivemap_fragment>", `#include <emissivemap_fragment>
        // light scattered up through the turbid water, strongest in thin crests
        totalEmissiveRadiance += uScatterCol * uScatter * (0.45 + 0.55 * smoothstep(-0.3, 0.5, vCrest)) * (1.0 - seaFoam);`);
  };
  const mesh = new THREE.Mesh(radialGrid(), mat);
  mesh.receiveShadow = true;
  mesh.frustumCulled = false;
  mesh.renderOrder = -2;

  function setSea(key, fromDeg = windFrom) {
    const st = SEA_STATES[key] || SEA_STATES.moderate;
    windFrom = fromDeg;
    const r = jonswapWaves(st.wind, 40000, windFrom, st.steep);
    waves = r.waves;
    info = {key, label: st.label, wind_ms: st.wind, hs_m: r.hs, tp_s: r.tp, lp_m: r.lp};
    waves.forEach((v, i) => {
      U.uWaveDir.value[i].set(v.dx, v.dz); U.uWaveK.value[i] = v.k;
      U.uWaveA.value[i] = v.A; U.uWaveQ.value[i] = v.Q;
    });
    F.uFoamAmt.value = key === "calm" ? .3 : 1;
    F.uDetailStr.value = key === "calm" ? .12 : key === "fresh" ? .3 : .2;
  }
  setSea("moderate");

  const E = new THREE.Vector2(), C = new THREE.Vector2();
  // earthX/Z: where the scene origin is, in metres from the fixed world origin.
  // centre: the scene point the fine part of the grid should sit under.
  function update(dt, earthX, earthZ, centre) {
    t += dt;
    E.set(earthX, earthZ);
    F.uEarth.value.set(earthX, earthZ);
    C.set(Math.round(centre.x * 2) / 2, Math.round(centre.z * 2) / 2);
    mesh.position.set(C.x, 0, C.y);
    U.uSeaCentre.value.copy(C);
    waves.forEach((v, i) => {
      // phase in double precision on the CPU; the shader only sees small numbers
      const ph = v.k * (v.dx * E.x + v.dz * E.y) - v.w * t;
      U.uWavePh.value[i] = ph - TAU * Math.floor(ph / TAU);
    });
    const drift = .45 * t, b = (windFrom + 180) * Math.PI / 180;
    const ex = E.x + Math.sin(b) * drift, ez = E.y - Math.cos(b) * drift;
    const fr = x => x - Math.floor(x);
    // the same rotations as the shader, so the ripples stay fixed to the Earth
    F.uDetailOff.value.set(fr((.825 * ex - .565 * ez) / 19.7), fr((.565 * ex + .825 * ez) / 19.7),
                           fr((.454 * ex + .891 * ez) / 53.3), fr((-.891 * ex + .454 * ez) / 53.3));
  }
  function fade(k, x, z) {
    const s = GRID_S0 + (GRID_G - 1) * Math.hypot(x - C.x, z - C.y);
    return Math.min(1, Math.max(0, (TAU / k / s - 1.5) / 1.5));
  }
  function displace(x, z) {
    let dx = 0, dy = 0, dz = 0;
    waves.forEach((v, i) => {
      const A = v.A * fade(v.k, x, z), th = v.k * (v.dx * x + v.dz * z) + U.uWavePh.value[i];
      const c = Math.cos(th);
      dx += v.Q * A * v.dx * c; dz += v.Q * A * v.dz * c; dy += A * Math.sin(th);
    });
    return [dx, dy, dz];
  }
  // height of the drawn surface above scene point (x, z)
  function heightAt(x, z) {
    const d = displace(x, z);
    return displace(x - d[0], z - d[2])[1];
  }
  // tint shallow water from a depth map: texture red = depth_m / scale_m (row 0 = north edge)
  function setDepth(tex, rect, scale_m) {
    tex.flipY = false; tex.minFilter = tex.magFilter = THREE.LinearFilter; tex.generateMipmaps = false;
    tex.wrapS = tex.wrapT = THREE.ClampToEdgeWrapping; tex.colorSpace = THREE.NoColorSpace;
    F.uDepthTex.value = tex; F.uDepthRect.value.set(...rect); F.uDepthScale.value = scale_m; F.uDepthOn.value = 1;
  }
  function setDay(day) {
    F.uScatter.value = .02 + .9 * day;
  }
  // make a material ride the surface, `lift` metres above it
  function floatMaterial(material, lift) {
    material.onBeforeCompile = sh => {
      Object.assign(sh.uniforms, U);
      sh.vertexShader = sh.vertexShader
        .replace("#include <common>", "#include <common>\n" + SEA_GLSL)
        .replace("#include <begin_vertex>", `#include <begin_vertex>
          { vec4 wp = modelMatrix * vec4(transformed, 1.0);
            wp.y = seaHeight(wp.xz) + ${lift.toFixed(3)};
            transformed = (inverse(modelMatrix) * wp).xyz; }`);
    };
    material.customProgramCacheKey = () => "float" + lift;
    return material;
  }
  return {mesh, material: mat, uniforms: U, glsl: SEA_GLSL, setSea, update, heightAt, setDay, setDepth,
          floatMaterial, get info() { return info; }};
}
