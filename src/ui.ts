// The card, the storm picker, the legend, the control bar and the place labels. Everything shown is measured from the
// frame in meta.json.
import * as THREE from "three";
import { dayStr, degC, dvorak, frameDate, hhmm, isoMs, loopIndices, subsolar, topKm, type Meta, type OtherStorm, type Storm } from "./meta";
import { ll2v, type Globe, type Mode, type Style } from "./hdr/scene";
import { NATURAL_HINT, satCredit, stormSub, stormTitle, visSatellite } from "./names";
import { CATEGORIES } from "./intensity";

const CITIES: [string, number, number][] = [
  ["Manila", 14.6, 121.0], ["Guam", 13.44, 144.79], ["Tokyo", 35.68, 139.69], ["Taipei", 25.03, 121.56],
  ["Naha", 26.21, 127.68], ["Palau", 7.5, 134.6], ["Hong Kong", 22.3, 114.17], ["Honolulu", 21.31, -157.86],
  ["Miami", 25.76, -80.19], ["Havana", 23.11, -82.37], ["Acapulco", 16.85, -99.82], ["Mumbai", 19.08, 72.88],
  ["Darwin", -12.46, 130.84], ["Nouméa", -22.27, 166.44], ["Port Louis", -20.16, 57.5],
];
const POINTS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"];

const $ = <T extends HTMLElement = HTMLElement>(id: string) => document.getElementById(id) as T;
const html = (id: string, s: string) => { $(id).innerHTML = s; };
const compass = (deg: number) => POINTS[Math.round(deg / 22.5) % 16];

/** The basin and the word its warning centre uses at this strength (an unofficial estimate, so only a guide). */
function naming(st: Storm) {
  const { lat, lon } = st.eye;
  const kt = st.eyeDiameterKm ? dvorak(st.eyeK, st.ringK).kt : null;
  const strong = (word: string) => (kt === null ? "Tropical cyclone" : kt >= 64 ? word : "Tropical storm");
  if (lat < 0) return { basin: lon > 135 || lon < -120 ? "South Pacific" : "South Indian Ocean", title: kt === null ? "Tropical cyclone" : "Severe tropical cyclone" };
  if (lon >= 100) return { basin: "West Pacific", title: kt !== null && kt >= 130 ? "Super typhoon" : strong("Typhoon") };
  if (lon >= 40) return { basin: "North Indian Ocean", title: strong("Cyclone") };
  if (lon < -140) return { basin: "Central Pacific", title: strong("Hurricane") };
  if (lon >= -82 || (lon > -100 && lat > 17)) return { basin: "Atlantic", title: strong("Hurricane") };
  return { basin: "East Pacific", title: strong("Hurricane") };
}

function fillCard(meta: Meta, st: Storm) {
  const frame = frameDate(meta);
  const ns = (v: number, p: string, n: string) => `${Math.abs(v).toFixed(2)}°${v >= 0 ? p : n}`;
  const nm = naming(st), dv = st.eyeDiameterKm ? dvorak(st.eyeK, st.ringK) : null;
  $("card-eyebrow").textContent = st.agency && st.advisory
    ? `${nm.basin} · ${st.agency} advisory ${hhmm(new Date(isoMs(st.advisory)))}` : `${nm.basin} · from satellite only`;
  $("card-title").textContent = st.name ? stormTitle(st) : nm.title;
  html("fix", `${ns(st.eye.lat, "N", "S")} ${ns(st.eye.lon, "E", "W")}<br><small>${st.track.length < 2 && st.agency ? "advisory position, carried to" : "from IR,"} ${hhmm(frame)} ${dayStr(frame)}</small>`);
  html("motion", `${compass(st.motion.bearing)} at ≈ ${Math.round(st.motion.kt)} kt<br><small>from ${st.motion.hours} h of ${st.motion.official ? "the official track" : "IR fixes"}</small>`);
  $("eyed").textContent = st.eyeDiameterKm ? `${st.eyeDiameterKm} km wide · ${degC(st.eyeK)}` : "Not clear in this frame";
  $("tops").textContent = `${degC(st.ringK)} · ≈ ${topKm(st.ringK).toFixed(0)} km`;
  $("cdo").textContent = `≈ ${st.cdoRadiusKm} km radius below −63 °C`;
  const tr = st.track, span = tr.length > 1 ? Math.round((Date.parse(tr[tr.length - 1][0]) - Date.parse(tr[0][0])) / 36e5) : 0;
  $("trackinfo").textContent = (span ? `${span} h of fixes from ${st.satellite}, every 3 h` : `No eye ${st.satellite} could follow; centred on the advisory`)
    + satCredit(st.satellite) + (st.visKind === "natural" ? " · natural colour" : "");
  $("source").textContent = st.agency ? `${st.agency} advisory ${hhmm(new Date(isoMs(st.advisory!)))} · ${st.agencyId ?? st.id}` : "No warning centre advisory matched; found in the IR";
  $("peek").textContent = st.windKt != null ? `${st.windKt} kt (${st.agency}) · ${compass(st.motion.bearing)} at ${Math.round(st.motion.kt)} kt`
    : `${dv ? `≈ ${dv.kt} kt (unofficial)` : "no clear eye"} · ${compass(st.motion.bearing)} at ${Math.round(st.motion.kt)} kt`;
  html("vmax", st.windKt != null ? `${st.windKt} kt · ${st.agency}${st.pressureMb ? ` · ${st.pressureMb} hPa` : ""}<br><small>official advisory${dv ? `; IR estimate ≈ ${dv.kt} kt, unofficial` : ""}</small>`
    : dv ? `≈ ${dv.kt} kt (T${dv.t.toFixed(1)})<br><small>rough Dvorak-style IR estimate, not official</small>`
    : "No estimate<br><small>the IR method used here needs a clear eye</small>");
}

export function bindControls(globe: Globe, meta: Meta) {
  const frame = frameDate(meta);
  const storms = meta.storms;
  let st = storms[0], sel = 0;
  const hints = () => [
    `Infrared at 10.4 µm from five geostationary satellites, stitched at ${hhmm(frame)} ${dayStr(frame)}. Colder tops are higher.`,
    `The real 2 km ${st.satellite} frame, lifted by the height its temperature implies (≈ 6.3 K per km). Drag to turn around the storm, right-drag (or two fingers) to slide across it, scroll out to spin the globe.`,
    "A 3D cloud built from the same satellite frame: each cloud stands as tall as its top temperature implies. Drag to turn, right-drag (or two fingers) to slide.",
  ];
  const press = (attr: string, v: string) =>
    document.querySelectorAll<HTMLElement>(`[data-${attr}]`).forEach((b) => b.setAttribute("aria-pressed", String(b.dataset[attr] === v)));

  let mode: Mode = 0;
  const setMode = (m: Mode) => {
    mode = m;
    press("mode", String(m));
    globe.setMode(m);
    $("exagwrap").hidden = m === 0;
    $("modehint").textContent = hints()[m];
  };
  document.querySelectorAll<HTMLElement>("[data-mode]").forEach((b) => b.addEventListener("click", () => setMode(Number(b.dataset.mode) as Mode)));
  // imagery style, with a legend to match
  const RAMPS: Record<Style, { ramp: string; ticks: string[]; hint: () => string }> = {
    0: { ramp: "linear-gradient(90deg, #3a3f46, #e9ecef)", ticks: ["warm, low", "cold, high"],
      hint: () => (!st.visible ? "No daylight picture of this storm in this frame, so it shows infrared in grey."
        : st.visKind === "natural" ? `${NATURAL_HINT}.`
        : `True colour from ${visSatellite(st)}'s visible channels (1 km) around the storm.`)
        + (meta.day ? ` The day side is the satellites' own true-colour picture (about 10 km), clouds and all; at night, and where no satellite saw daylight, infrared in grey over Blue Marble.`
          : " Elsewhere, and at night, infrared in grey.") },
    1: { ramp: "linear-gradient(90deg, #2a2a2a 0%, #d9d9d9 30%, #1a44f2 38%, #08a6d9 48%, #1bb320 58%, #f2cc0d 67%, #f25a05 76%, #b30d05 85%, #4d0202 100%)",
      ticks: ["+20", "−33", "−43", "−53", "−63", "−73 °C"], hint: () => "Infrared coloured by cloud-top temperature. Red and deep red are the tallest, strongest thunderstorms." },
    2: { ramp: "linear-gradient(90deg, #08184f, #e6edf8 45%, #6b7078)", ticks: ["sea", "low cloud", "cold core"],
      hint: () => "Infrared clouds over a blue sea. The tall, cold core shows soft grey; thinner and lower cloud is bright." },
  };
  let style: Style = meta.day || storms.some((s) => s.visible) ? 0 : 1;
  const setStyle = (s: Style) => {
    style = s; globe.setStyle(s); press("style", String(s));
    const r = RAMPS[s];
    $("ramp").style.background = r.ramp;
    html("ticks", r.ticks.map((t) => `<span>${t}</span>`).join(""));
    $("legend-title").textContent = s === 0 ? "True colour" : s === 1 ? "Cloud-top temperature" : "Infrared over blue sea";
    $("stylehint").textContent = r.hint();
  };
  document.querySelectorAll<HTMLElement>("[data-style]").forEach((b) => b.addEventListener("click", () => setStyle(Number(b.dataset.style) as Style)));
  // local solar time at the storm, so it's clear why the real sun can look like the studio light
  const sunHint = () => {
    const h = (((12 + (st.eye.lon - subsolar(frame).lon) / 15) % 24) + 24) % 24;
    const pad = (n: number) => String(Math.floor(n)).padStart(2, "0");
    return `Real sun: lit as at ${hhmm(frame)}. It's about ${pad(h)}:${pad((h % 1) * 60)} local solar time at the storm, ${
      h >= 6 && h < 18 ? "so the storm is in daylight" : "so the storm is in darkness (infrared still sees it)"}.`;
  };
  let realSun = false;
  document.querySelectorAll<HTMLElement>("[data-light]").forEach((b) => b.addEventListener("click", () => {
    realSun = b.dataset.light === "1";
    globe.setRealSun(realSun); press("light", b.dataset.light!);
    $("sunhint").hidden = !realSun; $("sunhint").textContent = sunHint();
  }));
  const exag = $<HTMLInputElement>("exag");
  exag.addEventListener("input", () => { globe.setHeight(Number(exag.value)); $("exagv").textContent = exag.value; });
  const track = $("track");
  track.addEventListener("click", () => {
    const on = track.getAttribute("aria-pressed") !== "true";
    globe.setTrack(on); track.setAttribute("aria-pressed", String(on)); $("pathlegend").hidden = !on;
  });
  const hd = $("hd");
  hd.hidden = !meta.tiles;  // data from before HD existed has no tiles
  hd.addEventListener("click", () => {
    const on = hd.getAttribute("aria-pressed") !== "true";
    globe.setHD(on); hd.setAttribute("aria-pressed", String(on));
  });
  for (const c of CATEGORIES) {
    const row = $("swatches").appendChild(Object.assign(document.createElement("div"), { className: "row" }));
    row.append(Object.assign(document.createElement("span"), { className: "dot" }), c.label);
    (row.firstChild as HTMLElement).style.background = c.color;
  }

  // the loop: the last 6, 12 or 24 hours (10-minute frames for 6 h, 30-minute frames beyond); paused shows the latest
  // frame at full resolution. Frames load the first time a range is played.
  let playing = false, hours = 6, frames: number[] = [], loopT = 0, loading = 0, busy = false;
  const playBtn = $("play"), playIcon = document.getElementById("playicon")!, timeEl = $("time");
  const setPlaying = async (on: boolean) => {
    const ticket = ++loading;
    playing = false;
    if (on) {
      frames = loopIndices(st, hours); busy = true;
      timeEl.innerHTML = `<b>Loading ${hours} h</b> · ${frames.length} frames`;
      try { await globe.prepareLoop(frames); } catch { if (ticket === loading) { busy = false; timeEl.textContent = "Couldn't load the loop"; } return; }
      if (ticket !== loading) return;
      loopT = 0;
    }
    busy = false; shown = "";
    playing = on && frames.length > 1;
    playIcon.setAttribute("d", playing ? "M6 4.5h4v15H6zM14 4.5h4v15h-4z" : "M7 4.5v15l12-7.5z");
    playBtn.setAttribute("aria-label", playing ? "Pause the loop" : `Play the last ${hours} hours`);
  };
  playBtn.addEventListener("click", () => setPlaying(!playing));
  document.querySelectorAll<HTMLElement>("[data-range]").forEach((b) => b.addEventListener("click", () => {
    hours = Number(b.dataset.range); press("range", b.dataset.range!);
    if (playing || loading) setPlaying(true);
  }));

  // place labels on the globe view; storm labels are buttons that select the storm
  const root = $("labels");
  type Label = { el: HTMLElement; p: THREE.Vector3; dx: number; dy: number };
  const labels: Label[] = CITIES.map(([n, la, lo]) => ({
    el: root.appendChild(Object.assign(document.createElement("div"), { className: "label", textContent: n })), p: ll2v(la, lo, 1.004), dx: 8, dy: -6,
  }));
  const stormLabels = storms.map((s, i) => {
    const el = Object.assign(document.createElement("button"), { type: "button", className: "label storm", textContent: s.name ? stormTitle(s) : storms.length > 1 ? `${i + 1} · ${naming(s).title}` : `EYE · ${hhmm(frame)}` });
    el.addEventListener("click", () => select(i));
    root.appendChild(el);
    const l = { el, p: ll2v(s.eye.lat, s.eye.lon, 1.02), dx: 34, dy: -34 };
    labels.push(l);
    return el;
  });

  // storms with a warning but no imagery here: a dimmed label that turns the globe to them, back in the Satellite view
  const lookAt = (o: OtherStorm) => { if (mode !== 0) setMode(0); globe.lookAt(o.lat, o.lon); };
  for (const o of meta.others) {
    const el = Object.assign(document.createElement("button"), { type: "button", className: "label storm off", textContent: stormTitle(o) });
    el.title = `${stormSub(o)} · no satellite view here`;
    el.addEventListener("click", () => lookAt(o));
    root.appendChild(el);
    labels.push({ el, p: ll2v(o.lat, o.lon, 1.02), dx: 34, dy: -34 });
  }

  // the storm list: every storm by name, imaged ones first; one without imagery turns the globe to it
  const pick = $("stormpick");
  const row = (title: string, sub: string) => {
    const b = document.createElement("button");
    b.type = "button";
    b.append(Object.assign(document.createElement("b"), { textContent: title }), Object.assign(document.createElement("small"), { textContent: sub }));
    pick.appendChild(b);
    return b;
  };
  storms.forEach((s, i) => {
    const dv = s.eyeDiameterKm ? dvorak(s.eyeK, s.ringK) : null;
    const b = row(s.name ? stormTitle(s) : `${i + 1} · ${naming(s).title}`, s.agency ? stormSub(s) : `${naming(s).basin} · ${dv ? `≈${dv.kt} kt, unofficial` : "from satellite only"}`);
    b.dataset.storm = String(i);
    b.addEventListener("click", () => select(i));
  });
  for (const o of meta.others) {
    const b = row(stormTitle(o), `${stormSub(o)} · no satellite view`);
    b.className = "off";
    b.addEventListener("click", () => lookAt(o));
  }
  pick.hidden = storms.length + meta.others.length < 2;

  function select(i: number) {
    sel = i; st = storms[i];
    press("storm", String(i));
    stormLabels.forEach((el, j) => el.classList.toggle("on", j === i));
    fillCard(meta, st);
    globe.selectStorm(i);
    setPlaying(false);
    $("play").hidden = $("rangewrap").hidden = st.loopFrames < 2;
    $("modehint").textContent = hints()[mode];
    $("sunhint").textContent = sunHint();
    $("stylehint").textContent = RAMPS[style].hint();
  }

  const canvas = $("gl"), tmp = new THREE.Vector3(), toCam = new THREE.Vector3();
  let shown = "";
  globe.onFrame((m, dt) => {
    // about 6 frames a second, holding a beat on the latest frame before it starts again
    const n = frames.length;
    if (playing) loopT = (loopT + dt * 6) % (n + 2);
    globe.setLoop(playing ? Math.min(loopT, n - 1) : null);
    const idx = playing ? frames[Math.min(Math.round(loopT), n - 1)] : -1;
    const key = `${sel}:${idx}:${playing}`;
    if (key !== shown && !busy) {
      shown = key;
      const t = playing ? new Date(isoMs(st.loopTimes[idx])) : frame;
      timeEl.innerHTML = `<b>${hhmm(t)}</b> · ${dayStr(t)}${playing ? ` · last ${hours} h` : ""}`;
    }
    const w = canvas.clientWidth, h = canvas.clientHeight, cam = globe.camera;
    for (const l of labels) {
      const facing = toCam.copy(cam.position).sub(l.p).normalize().dot(tmp.copy(l.p).normalize());
      tmp.copy(l.p).project(cam);
      const show = m === 0 && facing > 0.15 && tmp.z < 1;
      l.el.style.display = show ? "block" : "none";
      if (show) l.el.style.transform = `translate(${((tmp.x + 1) / 2) * w + l.dx}px, ${((1 - tmp.y) / 2) * h + l.dy}px)`;
    }
  });

  // the card and the legend are drawers: hidden at first, a tab on the right edge slides each in, and each hides itself
  // again a few seconds after the pointer leaves it (or straight away when you go back to the globe)
  const drawers: { close: () => void }[] = [["card", "cardtoggle"], ["legend", "legendtoggle"]].map(([id, tab]) => {
    const el = $(id), btn = $(tab);
    let timer = 0;
    const open = () => {
      clearTimeout(timer); el.removeAttribute("data-closing");
      el.dataset.open = "true"; btn.setAttribute("aria-expanded", "true");
      later(6000);
    };
    const close = () => {
      clearTimeout(timer);
      if (el.dataset.open !== "true" || el.hasAttribute("data-closing")) return;
      btn.setAttribute("aria-expanded", "false");
      if (matchMedia("(prefers-reduced-motion: reduce)").matches) { el.dataset.open = "false"; return; }
      el.setAttribute("data-closing", "");
      setTimeout(() => { if (el.hasAttribute("data-closing")) { el.removeAttribute("data-closing"); el.dataset.open = "false"; } }, 240);
    };
    const later = (ms: number) => { clearTimeout(timer); timer = window.setTimeout(close, ms); };
    btn.addEventListener("click", () => {
      if (btn.getAttribute("aria-expanded") === "true") return close();
      // on a phone there is room for one drawer at a time
      if (matchMedia("(max-width: 900px)").matches) drawers.forEach((d) => d.close());
      open();
    });
    el.addEventListener("pointerenter", () => clearTimeout(timer));
    el.addEventListener("pointerleave", () => { if (el.dataset.open === "true") later(2500); });
    el.addEventListener("focusin", () => clearTimeout(timer));
    return { close };
  });
  canvas.addEventListener("pointerdown", () => drawers.forEach((d) => d.close()));
  addEventListener("keydown", (e) => { if (e.key === "Escape") drawers.forEach((d) => d.close()); });

  fillCard(meta, st);
  press("storm", "0");
  stormLabels[0]?.classList.add("on");
  $("play").hidden = $("rangewrap").hidden = st.loopFrames < 2;
  setMode(0);
  setStyle(style);
}
