// Shaders. Every IR texture stores code = (320 K − BT) / 140 K in its red channel.
import { Vector2 } from "three";

/**
 * Shared helpers: brightness temperature, cloud opacity, cloud-top height, and the three imagery styles.
 * style 0 = Visible (true colour where a close-up has it, plain IR elsewhere), 1 = Infra, 2 = Blue.
 */
export const GLSL_IR = /* glsl */ `
  float btOf(float code) { return 320.0 - code * 140.0; }
  float cloudOf(float bt) { return pow(smoothstep(290.0, 228.0, bt), 0.8); }
  float topKm(float bt) { return clamp((300.0 - bt) / 6.3, 0.0, 17.5) * smoothstep(286.0, 262.0, bt); }
  // plain IR: low warm cloud dim grey, high cold tops bright. The ramp runs on down to -88 C (185 K) and steepens at the
  // cold end, so a storm's central cloud shield (often -65 to -85 C) keeps its spiral bands instead of one flat white.
  vec3 greyIR(float bt) { return vec3(0.30 + 0.58 * pow(clamp((290.0 - bt) / 105.0, 0.0, 1.0), 1.3)); }
  // forecaster IR: grey down to -30 C, then blue, cyan, green, yellow, orange, red and deep red at the coldest tops
  vec3 infra(float bt) {
    float c = bt - 273.15;
    vec3 col = vec3(mix(0.16, 0.85, smoothstep(30.0, -30.0, c)));
    col = mix(col, vec3(0.10, 0.25, 0.95), smoothstep(-31.0, -35.0, c));
    col = mix(col, vec3(0.03, 0.62, 0.85), smoothstep(-39.0, -43.0, c));
    col = mix(col, vec3(0.10, 0.70, 0.12), smoothstep(-46.0, -50.0, c));
    col = mix(col, vec3(0.95, 0.80, 0.05), smoothstep(-53.0, -57.0, c));
    col = mix(col, vec3(0.95, 0.35, 0.02), smoothstep(-59.0, -62.0, c));
    col = mix(col, vec3(0.70, 0.05, 0.02), smoothstep(-64.0, -67.0, c));
    col = mix(col, vec3(0.30, 0.01, 0.01), smoothstep(-70.0, -76.0, c));
    col = mix(col, vec3(0.16, 0.0, 0.02), smoothstep(-80.0, -86.0, c));
    return col;
  }
  // blue: bright edges and low cloud, the tall cold core in soft grey, over a deep blue sea
  vec3 blueIR(float bt) { return vec3(0.9, 0.93, 0.98) * mix(1.0, 0.42, smoothstep(238.0, 196.0, bt)); }
  vec3 styleIR(float bt, float style) { return style < 0.5 ? greyIR(bt) : style < 1.5 ? infra(bt) : blueIR(bt); }
`;

/** Height field of the storm frame, cross-fading two loop frames, lightly smoothed so single pixels don't spike. */
const GLSL_FRAME = /* glsl */ `
  float codeAt(vec2 uv){ vec2 F = flowAt(frameFlow, uv, frameFlowMax);
    return mix(texture2D(frameA, uv - frameMix * F).r, texture2D(frameB, uv + (1.0 - frameMix) * F).r, frameFade); }
  float hAt(vec2 uv){ vec2 o = vec2(2.0 / 768.0, 0.0);
    return (topKm(btOf(codeAt(uv))) * 2.0 + topKm(btOf(codeAt(uv + o))) + topKm(btOf(codeAt(uv - o))) + topKm(btOf(codeAt(uv + o.yx))) + topKm(btOf(codeAt(uv - o.yx)))) / 6.0; }
`;

export const SKY = {
  vertex: /* glsl */ `varying vec2 vUv; void main(){ vUv = uv; gl_Position = projectionMatrix * modelViewMatrix * vec4(position,1.0); }`,
  fragment: /* glsl */ `uniform sampler2D map; varying vec2 vUv; void main(){ vec3 c = texture2D(map, vUv).rgb; gl_FragColor = vec4(pow(c, vec3(1.35)) * 1.6, 1.0); }`,
};

const SPHERE_VERTEX = /* glsl */ `
  varying vec2 vUv; varying vec3 vN; varying vec3 vP;
  void main(){ vUv = uv; vN = normalize(mat3(modelMatrix) * normal); vec4 w = modelMatrix * vec4(position,1.0); vP = w.xyz; gl_Position = projectionMatrix * viewMatrix * w; }`;

/**
 * Motion between the two replay frames on screen (pipeline/flow.py): rg = 0.5 + dx/max * 127/255, in the frame's pixels,
 * y down the image. A frame is carried forward along it and the next one back, so clouds slide instead of fading.
 * Paused, or for a pair without a motion file, the textures are a neutral 1 x 1 pixel: no motion.
 */
const FLOW = /* glsl */ `
  uniform sampler2D frameFlow, visFlow, irFlow; uniform vec2 frameFlowMax, visFlowMax, irFlowMax;
  uniform float frameFade, visFade, irFade;  // how much of the next frame shows; the *Mix uniforms are how far along the motion
  vec2 flowAt(sampler2D m, vec2 uv, vec2 maxUv) { vec2 f = (texture2D(m, uv).rg * 255.0 - 128.0) / 127.0 * maxUv; return vec2(f.x, -f.y); }
`;

// the global IR while a loop plays: two hourly frames either side of the moment, slid along their motion and cross-faded
const GLOBE_IR = /* glsl */ `float irAt(vec2 uv) { vec2 F = flowAt(irFlow, uv, irFlowMax);
  return mix(texture2D(ir, uv - irMix * F).r, texture2D(irB, uv + (1.0 - irMix) * F).r, irFade); }`;

/**
 * The live day side (pipeline/daymap.py): how much of the satellites' true-colour picture to show here, 0 to 1. Only in
 * True colour, only while the latest frame is up (dayOn fades it out for the replay), and only where a satellite saw
 * this spot in daylight at the frame time (the mask); everywhere else the Earth is Blue Marble under the infrared clouds.
 */
const DAY_LIVE = /* glsl */ `
  uniform sampler2D dayMask; uniform float dayOn;
  float dayLive(vec2 uv) { return dayOn * (1.0 - step(0.5, style)) * texture2D(dayMask, uv).r; }
`;

/**
 * The Earth: by day the satellites' live true-colour picture where there is one (clouds and all), Blue Marble elsewhere;
 * city lights at night, ocean tint, terrain bump, and shadows cast by the IR clouds.
 */
export const EARTH = {
  vertex: SPHERE_VERTEX,
  fragment: /* glsl */ `
    uniform sampler2D day, night, water, topo, ir, irB, dayTC; uniform vec3 sunDir; uniform float style, irMix; varying vec2 vUv; varying vec3 vN; varying vec3 vP;
    ${DAY_LIVE}
    ${GLSL_IR}
    ${FLOW}
    ${GLOBE_IR}
    void main(){
      vec3 N = normalize(vN), V = normalize(cameraPosition - vP), L = normalize(sunDir);
      vec2 px = vec2(1.0/2048.0, 1.0/1024.0);
      float hC = texture2D(topo, vUv).r, hE = texture2D(topo, vUv + vec2(px.x,0.)).r, hN = texture2D(topo, vUv + vec2(0.,px.y)).r;
      vec3 t = normalize(cross(vec3(0.,1.,0.), N)), b = normalize(cross(N, t));
      vec3 Nb = normalize(N - 1.6 * ((hE - hC) * t + (hN - hC) * b));
      float ndl = dot(Nb, L), ndlS = dot(N, L);
      float dayF = smoothstep(-0.12, 0.22, ndlS);
      // cloud shadow: sample the IR clouds shifted away from the sun
      vec3 Lt = L - N * dot(L, N);
      vec2 sh = vec2(dot(Lt, t), dot(Lt, b)) * vec2(0.0045, 0.009);
      // the live picture already holds its clouds and their shadows: no infrared shadow on top of it
      float live = dayLive(vUv);
      float shadow = cloudOf(btOf(irAt(vUv + sh))) * 0.72 * (1.0 - live);
      vec3 albedo = texture2D(day, vUv).rgb;
      float w = texture2D(water, vUv).r;
      albedo = mix(albedo, albedo * vec3(0.55, 0.75, 1.05), w * 0.5);
      if (style > 1.5) albedo = mix(albedo * vec3(0.55, 0.85, 0.45), vec3(0.03, 0.09, 0.36), w);
      albedo = mix(albedo, texture2D(dayTC, vUv).rgb, live);
      vec3 sunCol = vec3(1.0, 0.96, 0.9) * 2.3;
      vec3 col = albedo * sunCol * max(ndl, 0.0) * (1.0 - shadow);
      // sunset tint near the terminator
      col *= mix(vec3(1.0, 0.55, 0.3), vec3(1.0), smoothstep(0.0, 0.35, ndlS));
      // city lights on the night side, dimmed under cloud
      vec3 lights = pow(texture2D(night, vUv).rgb, vec3(1.6)) * vec3(1.0, 0.78, 0.5) * 7.0;
      float cl = cloudOf(btOf(irAt(vUv)));
      col += lights * (1.0 - dayF) * (1.0 - cl * 0.85);
      col += albedo * vec3(0.012, 0.018, 0.03);
      // aerial perspective toward the limb
      float fres = pow(1.0 - max(dot(N, V), 0.0), 3.0);
      col = mix(col, vec3(0.22, 0.5, 1.0) * 0.6 * max(dayF, 0.04), fres * 0.28);
      gl_FragColor = vec4(col, 1.0);
    }`,
};

/** The stitched global IR as a cloud shell. Live satellite coverage is coloured; the archival gap stays grey. */
export const CLOUDS = {
  vertex: SPHERE_VERTEX,
  fragment: /* glsl */ `
    uniform sampler2D ir, irB, live, hd; uniform vec3 sunDir, eye, east, north; uniform float style, patchOn, patchAng, realSun, irMix, hdOn;
    uniform vec4 hdRect;
    varying vec2 vUv; varying vec3 vN; varying vec3 vP;
    ${GLSL_IR}
    ${FLOW}
    ${GLOBE_IR}
    ${DAY_LIVE}
    // the HD window: sharper tiles where they have loaded (alpha 1), the global map everywhere else
    // (sampled outside any branch, so the GPU still picks the right mip level)
    float cloudCode(vec2 uv) {
      float g = irAt(uv);
      vec2 d = vec2(fract(uv.x - hdRect.x), uv.y - hdRect.y) / hdRect.zw;
      vec4 h = texture2D(hd, d);
      float inside = hdOn * step(0.0, d.y) * step(d.x, 1.0) * step(d.y, 1.0);
      return mix(g, h.r / max(h.a, 1e-3), h.a * inside);
    }
    void main(){
      vec3 N = normalize(vN), V = normalize(cameraPosition - vP), L = normalize(sunDir);
      float bt = btOf(cloudCode(vUv));
      float a = pow(smoothstep(286.0, 232.0, bt), 1.15);
      float isLive = texture2D(live, vUv).r;
      // hide where the 3D patch takes over
      vec3 d = N - eye; float s = dot(d, east) / patchAng, tt = dot(d, north) / patchAng;
      float inPatch = (1.0 - smoothstep(0.38, 0.47, max(abs(s), abs(tt)))) * step(0.0, dot(N, eye));
      a *= 1.0 - inPatch * patchOn;
      // where the ground shows the live true-colour picture, its own clouds take the place of the infrared ones
      a *= 1.0 - dayLive(vUv);
      float ndl = dot(N, L);
      // studio light wraps softly; the real sun gives a sharp terminator (high tops catch light just past it)
      float lit = mix(smoothstep(-0.25, 0.6, ndl), smoothstep(-0.05, 0.3, ndl), realSun);
      // the archival gap stays plain grey whatever the style
      vec3 base = mix(greyIR(bt), styleIR(bt, style), isLive);
      vec3 sunCol = vec3(1.0, 0.95, 0.88) * 0.95 * mix(vec3(1.0, 0.5, 0.25), vec3(1.0), smoothstep(0.0, 0.4, ndl));
      vec3 col = base * (sunCol * lit + vec3(0.10, 0.13, 0.18));
      // IR sees through the night: keep a cool moonlit read of the cloud tops
      col += base * vec3(0.20, 0.24, 0.32) * mix(1.0, 0.35, realSun) * (1.0 - lit) * a;
      float fres = pow(1.0 - max(dot(N, V), 0.0), 2.0);
      col = mix(col, vec3(0.6, 0.8, 1.0) * lit, fres * 0.12);
      gl_FragColor = vec4(col, a * mix(0.4, 0.85, isLive));
    }`,
};

export const ATMOSPHERE = {
  vertex: /* glsl */ `varying vec3 vNv; varying vec3 vNw; void main(){ vNv = normalize(normalMatrix * normal); vNw = normalize(mat3(modelMatrix) * normal); gl_Position = projectionMatrix * modelViewMatrix * vec4(position,1.); }`,
  fragment: /* glsl */ `
    uniform vec3 sunDir; varying vec3 vNv; varying vec3 vNw;
    void main(){
      // back faces: t is 0 at the halo's outer edge and about 0.33 at the globe's limb
      float t = clamp(-vNv.z, 0.0, 1.0);
      float k = pow(smoothstep(0.0, 0.34, t), 3.0);
      float sd = dot(-vNw, normalize(sunDir));
      float day = smoothstep(-0.35, 0.45, sd);
      float term = exp(-pow(sd * 3.0, 2.0));
      vec3 col = mix(vec3(0.25, 0.55, 1.0), vec3(1.0, 0.42, 0.15), term * 0.45) * k * (0.01 + 0.06 * day + 0.02 * term);
      gl_FragColor = vec4(col, 1.0);
    }`,
};

/**
 * Mode 0: the selected storm's own close-up draped on the globe, sharper than the global map: 1 km true colour where the
 * satellite saw it in daylight (Visible), otherwise the 2 km infrared frame in the chosen style. It fades into the global
 * cloud shell at its edges.
 */
export const IMAGERY = {
  vertex: /* glsl */ `
    uniform float patchAng; uniform vec3 eye, east, north;
    varying vec2 vUv; varying vec3 vUp; varying vec3 vP;
    void main(){
      vUv = uv;
      vec2 st = uv - 0.5;
      vec3 dir = normalize(eye + (st.x * east + st.y * north) * patchAng);
      vUp = dir;
      vec4 w = modelMatrix * vec4(dir * 1.0047, 1.0); vP = w.xyz;
      gl_Position = projectionMatrix * viewMatrix * w;
    }`,
  fragment: /* glsl */ `
    uniform sampler2D frameA, frameB, vis, visB;
    vec3 visAt(vec2 uv); uniform float frameMix, visMix, style, visOn, realSun; uniform vec3 sunDir, sunReal;
    varying vec2 vUv; varying vec3 vUp; varying vec3 vP;
    ${GLSL_IR}
    ${FLOW}
    vec3 visAt(vec2 uv){ vec2 F = flowAt(visFlow, uv, visFlowMax);
    return mix(texture2D(vis, uv - visMix * F).rgb, texture2D(visB, uv + (1.0 - visMix) * F).rgb, visFade); }
    float codeAt(vec2 uv){ vec2 F = flowAt(frameFlow, uv, frameFlowMax);
    return mix(texture2D(frameA, uv - frameMix * F).r, texture2D(frameB, uv + (1.0 - frameMix) * F).r, frameFade); }
    void main(){
      float edge = 1.0 - smoothstep(0.38, 0.47, max(abs(vUv.x - 0.5), abs(vUv.y - 0.5)));
      if (edge <= 0.0) discard;
      vec3 N = normalize(vUp), V = normalize(cameraPosition - vP), L = normalize(sunDir);
      float ndl = dot(N, L);
      float lit = mix(smoothstep(-0.25, 0.6, ndl), smoothstep(-0.05, 0.3, ndl), realSun);
      vec3 sunCol = vec3(1.0, 0.95, 0.88) * 0.95 * mix(vec3(1.0, 0.5, 0.25), vec3(1.0), smoothstep(0.0, 0.4, ndl));
      float bt = btOf(codeAt(vUv));
      float a = pow(smoothstep(286.0, 232.0, bt), 1.15);
      vec3 base = styleIR(bt, style);
      vec3 ir = base * (sunCol * lit + vec3(0.10, 0.13, 0.18)) + base * vec3(0.20, 0.24, 0.32) * mix(1.0, 0.35, realSun) * (1.0 - lit);
      // true colour only where the sun was up when the satellite took it
      float k = visOn * smoothstep(0.03, 0.16, dot(N, normalize(sunReal)));
      vec3 v = visAt(vUv);
      // keep the cloud and let the globe's own sea and land show through, so the close-up has no visible square edge
      float cloud = smoothstep(0.05, 0.32, dot(v, vec3(0.3, 0.45, 0.25)));
      vec3 tc = v * (sunCol * 1.05 * lit + vec3(0.03, 0.035, 0.045));
      vec3 col = mix(ir, tc, k);
      float fres = pow(1.0 - max(dot(N, V), 0.0), 2.0);
      col = mix(col, vec3(0.6, 0.8, 1.0) * lit, fres * 0.12);
      gl_FragColor = vec4(col, mix(a * 0.85, cloud, k) * edge);
    }`,
};

/** Mode 1: the real 2 km frame on a dense grid, each vertex lifted to its cloud-top height, with self-shadowing. */
export const CLOUD_TOPS = {
  vertex: /* glsl */ `
    uniform sampler2D frameA, frameB; uniform float frameMix, exag, patchAng; uniform vec3 eye, east, north;
    varying vec2 vUv; varying vec3 vUp; varying vec3 vP;
    ${GLSL_IR}
    ${FLOW}
    ${GLSL_FRAME}
    void main(){
      vUv = uv;
      float h = hAt(uv);
      vec2 st = uv - 0.5;
      vec3 dir = normalize(eye + (st.x * east + st.y * north) * patchAng);
      vUp = dir;
      vec3 p = dir * (1.0 + 0.0008 + h / 6371.0 * exag);
      vec4 w = modelMatrix * vec4(p, 1.0); vP = w.xyz;
      gl_Position = projectionMatrix * viewMatrix * w;
    }`,
  fragment: /* glsl */ `
    uniform sampler2D frameA, frameB, vis, visB;
    vec3 visAt(vec2 uv); uniform float frameMix, visMix, exag, style, visOn, patchAng; uniform vec3 sunDir, east, north;
    varying vec2 vUv; varying vec3 vUp; varying vec3 vP;
    ${GLSL_IR}
    ${FLOW}
    vec3 visAt(vec2 uv){ vec2 F = flowAt(visFlow, uv, visFlowMax);
    return mix(texture2D(vis, uv - visMix * F).rgb, texture2D(visB, uv + (1.0 - visMix) * F).rgb, visFade); }
    ${GLSL_FRAME}
    void main(){
      float bt = btOf(codeAt(vUv));
      float a = cloudOf(bt);
      a *= 1.0 - smoothstep(0.40, 0.47, max(abs(vUv.x - 0.5), abs(vUv.y - 0.5)));
      if (a < 0.01) discard;
      float du = 2.5 / 768.0;
      float k = exag / (2.0 * du * 2048.0);
      float hx = (hAt(vUv + vec2(du, 0.)) - hAt(vUv - vec2(du, 0.))) * k;
      float hy = (hAt(vUv + vec2(0., du)) - hAt(vUv - vec2(0., du))) * k;
      vec3 N = normalize(vUp - hx * east - hy * north);
      vec3 L = normalize(sunDir), V = normalize(cameraPosition - vP);
      // self-shadowing: march the height field toward the sun
      vec3 Lt = L - vUp * dot(L, vUp);
      vec2 dirUv = vec2(dot(Lt, east), dot(Lt, north));
      float rise = max(dot(L, vUp), 0.02);
      float h0 = hAt(vUv), shadow = 0.0;
      float tanE = rise / max(length(Lt), 1e-3);
      for (int i = 1; i <= 24; i++) {
        float d = float(i) * float(i) * 0.00035;
        vec2 q = vUv + normalize(dirUv + 1e-5) * d;
        float diff = hAt(q) - (h0 + d * 2048.0 * tanE / exag);
        shadow = max(shadow, smoothstep(0.0, 1.2, diff));
      }
      float ndl = max(dot(N, L), 0.0);
      float wrap = max((dot(N, L) + 0.35) / 1.35, 0.0);
      // Visible drapes the real true-colour picture over the lifted tops
      vec3 base = style < 0.5 ? mix(vec3(0.95), visAt(vUv), visOn) : styleIR(bt, style) * 1.15;
      vec3 sunCol = vec3(1.0, 0.95, 0.88) * 1.8;
      vec3 sky = vec3(0.22, 0.36, 0.62) * (0.35 + 0.65 * max(dot(N, vUp), 0.0));
      vec3 col = base * (sunCol * mix(ndl, wrap, 0.35) * (1.0 - shadow * 0.85) + sky);
      // silver lining on edges facing away
      float rim = pow(1.0 - max(dot(N, V), 0.0), 3.0) * max(dot(-V, L), 0.0);
      col += vec3(1.0, 0.9, 0.8) * rim * 1.5;
      gl_FragColor = vec4(col, a);
    }`,
};

/**
 * Mode 2: a volumetric cloud ray-marched from the same frame. Density sits between a cloud base and the IR cloud top,
 * measured against the true sphere, with noise sheared around the eye.
 */
export const VOLUME = {
    vertex: /* glsl */ `varying vec3 vW; void main(){ vec4 w = modelMatrix * vec4(position, 1.0); vW = w.xyz; gl_Position = projectionMatrix * viewMatrix * w; }`,
    fragment: /* glsl */ `
    uniform sampler2D frameA, frameB, vis, visB;
    vec3 visAt(vec2 uv); uniform float frameMix, visMix, exag, style, visOn, time; uniform vec3 sunDir;
    uniform vec3 eye, east, north; uniform float patchAng, rmw;
    uniform mat4 modelMatrix; varying vec3 vW;
    ${GLSL_IR}
    // explicit level 0: these are read inside a loop with early exits, where the GPU can't work out a mip level by itself
    // (on real GPUs that gave a blurred, cloudless frame; software renderers happened to cope)
    // (the motion lookup is explicit level 0 too: an implicit-gradient read inside this loop fails to compile on some
    // Windows/Direct3D drivers, and the 3D storm then draws nothing)
    ${FLOW.replace("texture2D(m, uv)", "textureLod(m, uv, 0.0)")}
    vec3 visAt(vec2 uv){ vec2 F = flowAt(visFlow, uv, visFlowMax);
      return mix(textureLod(vis, uv - visMix * F, 0.0).rgb, textureLod(visB, uv + (1.0 - visMix) * F, 0.0).rgb, visFade); }
    float codeAt(vec2 uv){ vec2 F = flowAt(frameFlow, uv, frameFlowMax);
      return mix(textureLod(frameA, uv - frameMix * F, 0.0).r, textureLod(frameB, uv + (1.0 - frameMix) * F, 0.0).r, frameFade); }
    float hash(vec3 p){ p = fract(p * 0.3183099 + 0.1); p *= 17.0; return fract(p.x * p.y * p.z * (p.x + p.y + p.z)); }
    float noise(vec3 x){ vec3 i = floor(x), f = fract(x); f = f*f*(3.0-2.0*f);
      return mix(mix(mix(hash(i), hash(i+vec3(1,0,0)), f.x), mix(hash(i+vec3(0,1,0)), hash(i+vec3(1,1,0)), f.x), f.y),
                 mix(mix(hash(i+vec3(0,0,1)), hash(i+vec3(1,0,1)), f.x), mix(hash(i+vec3(0,1,1)), hash(i+vec3(1,1,1)), f.x), f.y), f.z); }
    float fbm(vec3 p){ float s = 0.0, a = 0.5; for (int i = 0; i < 3; i++){ s += a * noise(p); p *= 2.03; a *= 0.5; } return s; }
    // world point -> (km east, km north, km above the sea), using the true sphere
    vec3 kmOf(vec3 w){ float rr = length(w); vec3 dir = w / rr; vec3 dd = dir - eye;
      return vec3(dot(dd, east) / patchAng * 2048.0, dot(dd, north) / patchAng * 2048.0, (rr - 1.0) * 6371.0 / exag); }
    float density(vec3 km, out float bt){
      vec2 uv = km.xy / 2048.0 + 0.5;
      if (any(lessThan(uv, vec2(0.02))) || any(greaterThan(uv, vec2(0.98)))) { bt = 300.0; return 0.0; }
      bt = btOf(codeAt(uv));
      float r = length(km.xy), z = km.z;
      float top = topKm(bt);
      float base = mix(0.8, 5.0, smoothstep(160.0, 600.0, r)) * smoothstep(225.0, 250.0, bt);
      float d = smoothstep(top, top - 1.6, z) * smoothstep(base * 0.4, base * 0.4 + 0.8, z) * cloudOf(bt);
      d *= 1.0 - smoothstep(0.40, 0.48, max(abs(uv.x - 0.5), abs(uv.y - 0.5)));
      if (d <= 0.0) return 0.0;
      // cloud texture, sheared around the eye like the real winds
      float ang = atan(km.y, km.x) + time * 1.2 / max(r, 30.0);
      vec3 q = vec3(cos(ang) * r, sin(ang) * r, z * 12.0) * 0.045;
      d *= smoothstep(0.25, 0.75, fbm(q) + 0.25 * d);
      return d;
    }
    void main(){
      mat4 inv = inverse(modelMatrix);
      vec3 ro = (inv * vec4(cameraPosition, 1.0)).xyz;
      vec3 rd = normalize((inv * vec4(vW, 1.0)).xyz - ro);
      vec3 bmin = vec3(-1.0, -1.0, -0.45), bmax = vec3(1.0, 1.0, 1.0);
      vec3 t0 = (bmin - ro) / rd, t1 = (bmax - ro) / rd;
      vec3 tmin = min(t0, t1), tmax = max(t0, t1);
      float tn = max(max(tmin.x, tmin.y), max(tmin.z, 0.0)), tf = min(min(tmax.x, tmax.y), tmax.z);
      if (tf <= tn) discard;
      vec3 Lw = normalize(sunDir);
      const int STEPS = 96;
      float dt = (tf - tn) / float(STEPS);
      // interleaved gradient noise: the start offset varies smoothly across neighbouring pixels, so the banding it hides
      // turns into a fine even dither instead of coarse grain
      float jitter = fract(52.9829189 * fract(dot(gl_FragCoord.xy, vec2(0.06711056, 0.00583715))));
      vec3 acc = vec3(0.0); float trans = 1.0;
      vec3 sunCol = vec3(1.0, 0.95, 0.88) * 1.5, skyCol = vec3(0.22, 0.38, 0.7) * 0.35;
      for (int i = 0; i < STEPS; i++) {
        float t = tn + (float(i) + jitter) * dt;
        vec3 w = (modelMatrix * vec4(ro + rd * t, 1.0)).xyz;
        vec3 km = kmOf(w);
        float bt; float d = density(km, bt);
        if (d > 0.01) {
          float od = 0.0;
          for (int j = 1; j <= 4; j++) { float bt2; od += density(kmOf(w + Lw * float(j * j) * 0.0022), bt2); }
          float lightT = exp(-od * 2.6);
          float powder = 1.0 - exp(-d * 4.0);
          vec2 uvk = km.xy / 2048.0 + 0.5;
          vec3 base = style < 0.5 ? mix(vec3(0.95), clamp(visAt(uvk) * 1.1, 0.0, 1.0), visOn)
                    : style < 1.5 ? mix(vec3(0.95), infra(bt), smoothstep(11.0, 14.5, km.z)) : blueIR(bt);
          vec3 L = base * (sunCol * lightT * mix(0.6, 1.0, powder) + skyCol);
          float segKm = length((modelMatrix * vec4(rd * dt, 0.0)).xyz) * 6371.0;
          float a = 1.0 - exp(-d * 0.09 * segKm);
          acc += trans * a * L;
          trans *= 1.0 - a;
          if (trans < 0.02) break;
        }
      }
      if (trans > 0.995) discard;
      gl_FragColor = vec4(acc, 1.0 - trans);
    }`,
};

/** Lays the 3D storm buffer (premultiplied colour and coverage) over the scene through a 3x3 tent filter. */
export const VOLUME_BLEND = {
  uniforms: { tDiffuse: { value: null }, tVol: { value: null }, texel: { value: new Vector2(1, 1) } },
  vertexShader: /* glsl */ `varying vec2 vUv; void main(){ vUv = uv; gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0); }`,
  fragmentShader: /* glsl */ `
    uniform sampler2D tDiffuse, tVol; uniform vec2 texel; varying vec2 vUv;
    void main(){
      vec4 v = texture2D(tVol, vUv) * 0.25;
      v += (texture2D(tVol, vUv + vec2(texel.x, 0.0)) + texture2D(tVol, vUv - vec2(texel.x, 0.0))
          + texture2D(tVol, vUv + vec2(0.0, texel.y)) + texture2D(tVol, vUv - vec2(0.0, texel.y))) * 0.125;
      v += (texture2D(tVol, vUv + texel) + texture2D(tVol, vUv - texel)
          + texture2D(tVol, vUv + vec2(texel.x, -texel.y)) + texture2D(tVol, vUv - vec2(texel.x, -texel.y))) * 0.0625;
      vec4 s = texture2D(tDiffuse, vUv);
      gl_FragColor = vec4(s.rgb * (1.0 - v.a) + v.rgb, 1.0);
    }`,
};
