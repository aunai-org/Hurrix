// Everything about the frame comes from data/meta.json, written by pipeline/export.py.
import type { TileGrid } from "./hdr/tiles";

/** One storm, as measured from its own 2 km close-up. */
export interface Storm {
  id: string;
  /** The satellite its close-up and track come from. */
  satellite: string;
  loopFrames: number;
  /** Time of each loop frame, oldest first: every 30 minutes from 24 h back to 6 h back, then every 10 minutes. */
  loopTimes: string[];
  eye: { lat: number; lon: number };
  /** Warmest pixel in the eye and coldest top in the ring around it, in kelvin. */
  eyeK: number;
  ringK: number;
  eyeDiameterKm: number | null;
  cdoRadiusKm: number;
  /** official: worked out from the warning centre's track, because the IR couldn't follow the centre. */
  motion: { kt: number; bearing: number; hours: number; official?: boolean };
  /** [time, lat, lon, eye visible (1) or centre from cloud mass (0)], oldest first. */
  track: [string, number, number, number][];
  /** True when the pipeline cut a 1 km true-colour close-up (storm_vis.jpg) from a daylight frame. */
  visible?: boolean;
  /** Times of the true-colour loop frames (loop/vNN.jpg): every 30 minutes over the last 6 hours, daylight only. */
  visTimes?: string[];
  /** "natural" when the daylight picture is Meteosat-9's natural colour (a false-colour mix), not true colour. */
  visKind?: "natural";
  /** The satellite the daylight pictures come from, when it isn't the one above (Meteosat-12 for some Meteosat-9 storms). */
  visSatellite?: string;
  /** Folder of its close-up and loop, relative to the site root. */
  dir: string;
  /** From the warning centre's advisory; absent when the storm was found in the IR alone. */
  name?: string;
  /** The centre's word for it at this strength, e.g. "Typhoon", "Tropical storm", "Post-tropical cyclone". */
  kind?: string;
  /** The centre's number for it, e.g. "29W" (JTWC) or "AL10" (NHC). */
  agencyId?: string;
  agency?: "NHC" | "JTWC";
  windKt?: number | null;
  pressureMb?: number | null;
  /** Advisory time, "YYYY-MM-DDTHH:MMZ". */
  advisory?: string;
  /** The warning centre's past track: [time, lat, lon, wind kt or null], every 6 h, oldest first, ending at the advisory. */
  history?: [string, number, number, number | null][];
}

/** A storm with a current warning that no satellite in this feed sees well (too near the edge of every disc): listed, not imaged. */
export interface OtherStorm {
  name: string;
  kind: string;
  agencyId: string;
  agency: string;
  windKt: number | null;
  lat: number;
  lon: number;
  history?: [string, number, number, number | null][];
}

export interface Meta {
  /** Frame time, "YYYY-MM-DDTHH:MMZ". */
  frame: string;
  /** Strongest first. */
  storms: Storm[];
  /** Times of the whole-globe infrared frames (data/globe/gNN.jpg): once an hour over the last 24 hours, oldest first. */
  globeTimes?: string[];
  /** Storms with a warning but no imagery here. */
  others: OtherStorm[];
  /** The HD cloud tiles (data/tiles/r_c.jpg) the pipeline wrote; absent before HD existed. */
  tiles?: TileGrid;
  /** The live day side (data/day_global.jpg and day_mask.png, pipeline/daymap.py): the satellites it was made from. */
  day?: { satellites: string[] };
  /** Largest motion, in frame pixels, the replay's motion files encode (pipeline/flow.py); absent without them. */
  flow?: { ir: number; vis: number; globe: number };
}

export async function loadMeta(base: string): Promise<Meta> {
  const res = await fetch(`${base}data/meta.json`);
  if (!res.ok) throw new Error(`data/meta.json: HTTP ${res.status}`);
  return loadMetaFrom(await res.json());
}

export function loadMetaFrom(raw: any): Meta {
  // the first pipeline wrote one storm at the top level, with its files directly in data/
  if (!raw.storms) return { frame: raw.frame, others: [], storms: [{ ...raw, id: "s1", satellite: "Himawari-9", dir: "data/" }] };
  return { frame: raw.frame, globeTimes: raw.globeTimes, tiles: raw.tiles, flow: raw.flow, day: raw.day, others: raw.others ?? [], storms: (raw.storms as Omit<Storm, "dir">[]).map((s) => ({ ...s, dir: `data/storms/${s.id}/` })) };
}

export const frameDate = (meta: Meta) => new Date(meta.frame.replace("Z", ":00Z"));

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const pad = (n: number) => String(n).padStart(2, "0");
export const hhmm = (d: Date) => `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())} UTC`;
export const dayStr = (d: Date) => `${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]}`;
export const degC = (k: number) => `${k - 273.15 >= 0 ? "+" : "−"}${Math.abs(k - 273.15).toFixed(1)} °C`;

/** Cloud-top height implied by a brightness temperature (≈ 6.3 K per km), the same rule the shaders use. */
export const topKm = (bt: number) => Math.min(17.5, Math.max(0, (300 - bt) / 6.3));

const KT: Record<string, number> = { "4.0": 65, "4.5": 77, "5.0": 90, "5.5": 102, "6.0": 115, "6.5": 127, "7.0": 140, "7.5": 155 };
/** A rough Dvorak-style read of the eye scene: the coldest ring around the eye, nudged up for a very warm eye. Not official. */
export function dvorak(eyeK: number, ringK: number): { t: number; kt: number } {
  const ring = ringK - 273.15, eye = eyeK - 273.15;
  let t = ring <= -76 ? 6.5 : ring <= -70 ? 6.0 : ring <= -64 ? 5.5 : ring <= -58 ? 5.0 : ring <= -42 ? 4.5 : 4.0;
  if (eye >= 9) t += 0.5;
  return { t, kt: KT[t.toFixed(1)] };
}

/** Real subsolar point at a time (low-precision solar ephemeris, good to a fraction of a degree). */
export function subsolar(date: Date): { lat: number; lon: number } {
  const D2R = Math.PI / 180;
  const d = date.getTime() / 864e5 + 2440587.5 - 2451545.0;
  const g = (357.529 + 0.98560028 * d) * D2R, q = 280.459 + 0.98564736 * d;
  const L = (q + 1.915 * Math.sin(g) + 0.02 * Math.sin(2 * g)) * D2R, e = (23.439 - 3.6e-7 * d) * D2R;
  const ra = Math.atan2(Math.cos(e) * Math.sin(L), Math.cos(L)) / D2R, dec = Math.asin(Math.sin(e) * Math.sin(L)) / D2R;
  const gmst = (18.697374558 + 24.06570982441908 * d) % 24;
  const lon = ((((ra - gmst * 15) + 540) % 360) + 360) % 360 - 180;
  return { lat: dec, lon };
}

/** "YYYY-MM-DDTHH:MMZ" → milliseconds. */
export const isoMs = (iso: string) => Date.parse(iso.replace("Z", ":00Z"));

/** The loop frames to play for a range in hours: all frames in the last 6 h; for longer ranges, the 30-minute ones. */
export function loopIndices(s: Storm, hours: number): number[] {
  const end = isoMs(s.loopTimes[s.loopTimes.length - 1]), from = end - hours * 3600e3 - 60e3;
  return s.loopTimes.map((t, i) => [isoMs(t), i] as const)
    .filter(([ms]) => ms >= from && (hours <= 6 || Math.round((end - ms) / 60e3) % 30 === 0))
    .map(([, i]) => i);
}
