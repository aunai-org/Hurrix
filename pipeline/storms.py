"""Take the storms the warning centres are tracking (advisories.py), track each one back 48 h in the IR, and cut its 2 km
close-up and 24 h loop. Storms no satellite here sees well are listed without imagery. If no official storm can be
imaged (the feeds are down, or none is in view), the storms are found in the stitched IR map instead, unnamed.

Each close-up is resampled onto the same tangent-plane grid the 3D views use (2048 km square, centred on the eye, north
up), from whichever geostationary satellite sees the storm best: Himawari-9, GOES-18, GOES-19, or Meteosat-12 and
Meteosat-9 from EUMETSAT's map service (meteosat.py) over Europe, Africa and the Indian Ocean.

Usage: FRAME=YYYY-MM-DDTHH:MM python3 storms.py OUT_DIR      (after fetch.sh and reproject.py for that frame)
"""
import glob, http.client, json, os, sys, shutil, urllib.parse, urllib.request
import concurrent.futures as cf
from datetime import datetime, timedelta
import numpy as np
import advisories
import meteosat as M
import netCDF4 as nc
from PIL import Image
from scipy import ndimage

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "frames")
N, KM = 1024, 2048.0            # close-up: 1024 px at 2 km
R_KM = 6371.0
MAX_STORMS = 8
MIN_SPAN_H = float(os.environ.get("MIN_SPAN_H", 24))  # IR fallback only: how long a centre must hold together to count
code = lambda bt: np.clip((320 - bt) / 140, 0, 1)


# ---------- geostationary projection ----------
def scan_angles(lat, lon, p):
    """lat/lon (degrees, arrays) to fixed-grid scan angles for projection p; NaN where the satellite can't see."""
    a, b, h, lon0, sweep = p["a"], p["b"], p["h"], p["lon0"], p["sweep"]
    la, dl = np.radians(lat), np.radians(lon - lon0)
    e2 = 1 - b * b / (a * a)
    latc = np.arctan(b * b / (a * a) * np.tan(la)); rc = b / np.sqrt(1 - e2 * np.cos(latc) ** 2)
    sx = h - rc * np.cos(latc) * np.cos(dl); sy = -rc * np.cos(latc) * np.sin(dl); sz = rc * np.sin(latc)
    vis = h * (h - sx) >= sy * sy + (a * a) / (b * b) * sz * sz
    if sweep == "x":
        x = np.arcsin(-sy / np.sqrt(sx * sx + sy * sy + sz * sz)); y = np.arctan(sz / sx)
    else:
        x = np.arctan(-sy / sx); y = np.arcsin(sz / np.sqrt(sx * sx + sy * sy + sz * sz))
    return np.where(vis, x, np.nan), np.where(vis, y, np.nan)


def view_cos(lat, lon, lon0):
    """Rough cosine of the satellite zenith angle at a point (1 = straight below the satellite)."""
    la, dl = np.radians(lat), np.radians(lon - lon0)
    c = np.cos(la) * np.cos(dl)
    return (6.6107 * c - 1) / np.sqrt(6.6107 ** 2 - 2 * 6.6107 * c + 1)


class Disc:
    """One satellite image on its fixed grid: img[row, col] with scan angle x = x0 + col*dx, y = y0 + row*dy."""
    def __init__(self, img, x0, dx, y0, dy, proj):
        self.img, self.x0, self.dx, self.y0, self.dy, self.p = img, x0, dx, y0, dy, proj

    def sample(self, lat, lon):
        x, y = scan_angles(lat, lon, self.p)
        ok = np.isfinite(x)
        fc = np.where(ok, (x - self.x0) / self.dx, -1); fr = np.where(ok, (y - self.y0) / self.dy, -1)
        h, w = self.img.shape
        ok &= (fc >= 0) & (fc <= w - 1) & (fr >= 0) & (fr <= h - 1)
        out = ndimage.map_coordinates(np.nan_to_num(self.img, nan=300.0), [np.where(ok, fr, 0), np.where(ok, fc, 0)], order=1)
        return np.where(ok, out, np.nan).astype(np.float32)


# ---------- the close-up grid (gnomonic, matching the shaders: dir = normalize(eye + (u*east + v*north) * ang)) ----------
def frame_axes(lat, lon):
    la, lo = np.radians(lat), np.radians(lon)
    e = np.array([np.cos(la) * np.cos(lo), np.cos(la) * np.sin(lo), np.sin(la)])
    east = np.array([-np.sin(lo), np.cos(lo), 0.0]); north = np.cross(e, east)
    return e, east, north


def grid_latlon(lat, lon, n=N, km=KM):
    e, east, north = frame_axes(lat, lon)
    s = (np.arange(n) + 0.5) / n - 0.5
    u, v = np.meshgrid(s, -s)  # row 0 is the north edge
    ang = km / R_KM
    d = e[None, None] + (u[..., None] * east + v[..., None] * north) * ang
    d /= np.linalg.norm(d, axis=-1, keepdims=True)
    return np.degrees(np.arcsin(d[..., 2])), np.degrees(np.arctan2(d[..., 1], d[..., 0]))


def pix_latlon(lat, lon, col, row, n=N, km=KM):
    e, east, north = frame_axes(lat, lon)
    u, v = (col + 0.5) / n - 0.5, 0.5 - (row + 0.5) / n
    d = e + (u * east + v * north) * km / R_KM; d /= np.linalg.norm(d)
    return float(np.degrees(np.arcsin(d[2]))), float(np.degrees(np.arctan2(d[1], d[0])))


# ---------- data access (NOAA open data on AWS) ----------
def listing(bucket, prefix):
    keys, token = [], None
    while True:
        url = f"https://{bucket}.s3.amazonaws.com/?list-type=2&prefix={prefix}&max-keys=1000" + (f"&continuation-token={urllib.parse.quote(token)}" if token else "")
        for attempt in range(3):  # S3 now and then cuts a listing off mid-reply
            try:
                x = urllib.request.urlopen(url, timeout=60).read().decode(); break
            except (OSError, http.client.HTTPException):
                if attempt == 2:
                    raise
        keys += [k.split("</Key>")[0] for k in x.split("<Key>")[1:]]
        if "<NextContinuationToken>" not in x:
            return keys
        token = x.split("<NextContinuationToken>")[1].split("<")[0]


def download(bucket, key, path):
    if not os.path.exists(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        urllib.request.urlretrieve(f"https://{bucket}.s3.amazonaws.com/{key}", path + ".part"); os.replace(path + ".part", path)
    return path


SATS = {"himawari9": 140.7, "goes18": -137.0, "goes19": -75.2} | {s: v["lon0"] for s, v in M.SATS.items()}
SAT_NAMES = {"himawari9": "Himawari-9", "goes18": "GOES-18", "goes19": "GOES-19"} | {s: v["name"] for s, v in M.SATS.items()}
STEP = {s: M.SATS[s]["step"] if s in M.SATS else 10 for s in SATS}  # minutes between scans


def goes_disc(sat, when):
    """GOES full-disk band 13 at a 10-minute slot (downloaded once, then cached as a compact .npz)."""
    num = sat[-2:]
    out = os.path.join(CACHE, sat, f"{when:%Y%m%d%H%M}.npz")
    if not os.path.exists(out):
        pre = f"ABI-L2-CMIPF/{when:%Y/%j/%H}/"
        keys = [k for k in listing(f"noaa-goes{num}", pre) if f"C13_G{num}" in k and f"_s{when:%Y%j%H%M}" in k]
        if not keys:
            raise FileNotFoundError(f"no GOES-{num} band 13 at {when:%Y-%m-%d %H:%M}")
        tmp = download(f"noaa-goes{num}", keys[0], out + ".nc")
        d = nc.Dataset(tmp); p = d["goes_imager_projection"]
        x, y = d["x"][:], d["y"][:]
        np.savez_compressed(out, img=np.ma.filled(d["CMI"][:].astype(np.float32), np.nan), x0=float(x[0]), dx=float(x[1] - x[0]),
                            y0=float(y[0]), dy=float(y[1] - y[0]), a=p.semi_major_axis, b=p.semi_minor_axis,
                            h=p.perspective_point_height + p.semi_major_axis, lon0=p.longitude_of_projection_origin, sweep=p.sweep_angle_axis)
        d.close(); os.remove(tmp)
    z = np.load(out)
    proj = {"a": float(z["a"]), "b": float(z["b"]), "h": float(z["h"]), "lon0": float(z["lon0"]), "sweep": str(z["sweep"])}
    return Disc(z["img"], float(z["x0"]), float(z["dx"]), float(z["y0"]), float(z["dy"]), proj)


class Himawari:
    """Himawari-9 band 13 comes as 88 tiles of 550 px; only the tiles around a storm are fetched for past slots."""
    def __init__(self):
        cur = os.path.join(HERE, "hima", "current")
        files = sorted(glob.glob(os.path.join(HERE, "hima", open(cur).read().strip(), "*.nc"))) if os.path.exists(cur) else []
        if not files:
            raise FileNotFoundError("run fetch.sh first: Himawari tile layout is read from hima/")
        self.tiles = {}
        for f in files:
            d = nc.Dataset(f)
            self.tiles[int(f.split("-T")[1][:3])] = (int(d.tile_row_offset), int(d.tile_column_offset))
            x, y = d["x"][:] * 1e-6, d["y"][:] * 1e-6
            r, c = int(d.tile_row_offset), int(d.tile_column_offset)
            self.dx, self.dy = float(x[1] - x[0]), float(y[1] - y[0])
            self.x0, self.y0 = float(x[0]) - c * self.dx, float(y[0]) - r * self.dy  # full-disk origin
            p = d["fixedgrid_projection"]
            self.p = {"a": p.semi_major, "b": p.semi_minor, "h": p.perspective_point_height + p.semi_major,
                      "lon0": p.longitude_of_projection_origin, "sweep": p.sweep_angle_axis}
            d.close()

    def disc(self, when, lat, lon):
        la, lo = grid_latlon(lat, lon, 32)
        x, y = scan_angles(la, lo, self.p)
        cols = (x[np.isfinite(x)] - self.x0) / self.dx; rows = (y[np.isfinite(y)] - self.y0) / self.dy
        if not cols.size:
            raise ValueError("storm is outside the Himawari disc")
        r0, r1 = int(max(rows.min(), 0)) // 550, int(min(rows.max(), 5499)) // 550
        c0, c1 = int(max(cols.min(), 0)) // 550, int(min(cols.max(), 5499)) // 550
        keys = [k for k in listing("noaa-himawari9", f"AHI-L2-FLDK-ISatSS/{when:%Y/%m/%d/%H%M}/") if "M1C13" in k]
        need = [k for k in keys if (rc := self.tiles.get(int(k.split("-T")[1][:3]))) and r0 <= rc[0] // 550 <= r1 and c0 <= rc[1] // 550 <= c1]
        if not need:
            raise FileNotFoundError(f"no Himawari band-13 tiles at {when:%Y-%m-%d %H:%M}")  # e.g. the daily 02:40 / 14:40 gaps
        folder = os.path.join(CACHE, "himawari9", f"{when:%Y%m%d%H%M}")
        with cf.ThreadPoolExecutor(16) as ex:
            paths = list(ex.map(lambda k: download("noaa-himawari9", k, os.path.join(folder, os.path.basename(k))), need))
        img = np.full(((r1 - r0 + 1) * 550, (c1 - c0 + 1) * 550), np.nan, np.float32)
        for path in paths:
            d = nc.Dataset(path); r, c = int(d.tile_row_offset), int(d.tile_column_offset)
            v = np.ma.filled(d["Sectorized_CMI"][:].astype(np.float32), np.nan)
            img[r - r0 * 550:r - r0 * 550 + v.shape[0], c - c0 * 550:c - c0 * 550 + v.shape[1]] = v
            d.close()
        return Disc(img, self.x0 + c0 * 550 * self.dx, self.dx, self.y0 + r0 * 550 * self.dy, self.dy, self.p)


HIMA = None


def closeup(sat, when, lat, lon, n=N):
    """The storm's close-up at one time: BT on the tangent grid, NaN outside the satellite's view."""
    global HIMA
    if sat in M.SATS:
        disc = M.disc(sat, when, lat, lon, n)
    else:
        disc = (HIMA := HIMA or Himawari()).disc(when, lat, lon) if sat == "himawari9" else goes_disc(sat, when)
    la, lo = grid_latlon(lat, lon, n)
    return disc.sample(la, lo)


def closeup_near(sat, when, lat, lon, n=N):
    """A missing slot falls back to its neighbour 10 minutes away, the earlier one first (the latest may not be out yet)."""
    for dm in (0, 10, -10):
        try:
            return closeup(sat, when - timedelta(minutes=dm), lat, lon, n), when - timedelta(minutes=dm)
        except (FileNotFoundError, OSError) as e:
            print("  close-up: skipped", sat, when - timedelta(minutes=dm), e)
    return None, None


# ---------- finding and tracking storms ----------
def find_eye(f, guess, search):
    """The eye: the warmest small spot ringed by very cold tops (2 km pixels). Returns col, row, eye K, ring K, score."""
    cx, cy = guess
    y0, y1 = max(cy - search, 0), min(cy + search, f.shape[0]); x0, x1 = max(cx - search, 0), min(cx + search, f.shape[1])
    sub = f[y0:y1, x0:x1]
    # an eye is closed off on every side: take the coldest top in each of 8 directions (10-30 km out), then the warmest of those
    near = ndimage.minimum_filter(sub, size=11)
    ring = np.max([ndimage.shift(near, (-10 * np.sin(a), -10 * np.cos(a)), order=0, mode="nearest")
                   for a in np.arange(8) * np.pi / 4], axis=0)
    local = ndimage.uniform_filter(sub, 3)
    score = local - ring; score[ring > 240] = -1
    ey, ex = np.unravel_index(np.argmax(score), sub.shape)
    return x0 + ex, y0 + ey, float(local[ey, ex]), float(ring[ey, ex]), float(score[ey, ex])


def candidates(stitched):
    """Places in the global map with a large, solid cold core: at least 85 % of tops colder than −53 °C within 150 km."""
    d = np.load(stitched); bt, cov = d["bt"], np.nan_to_num(d["cov"])
    H, W = bt.shape
    f = np.where(np.isfinite(bt), bt, 300.0)
    lat = 90 - (np.arange(H) + 0.5) * 180 / H
    r = int(round(150 / (40075 / W)))
    yy, xx = np.mgrid[-r:r + 1, -r:r + 1]; disk = (xx ** 2 + yy ** 2 <= r * r).astype(np.float32); disk /= disk.sum()
    core = ndimage.convolve((f < 220).astype(np.float32), disk, mode="wrap")
    band = ((np.abs(lat) > 4) & (np.abs(lat) < 38))[:, None]
    core = np.where(band & (cov > 0.5), core, 0)
    peaks = (core == ndimage.maximum_filter(core, size=4 * r + 1, mode="wrap")) & (core > 0.85)
    out = []
    for y, x in zip(*np.where(peaks)):
        la, lo = float(lat[y]), float(x * 360 / W - 180 + 180 / W)
        if all(np.hypot(la - o[0], (lo - o[1]) * np.cos(np.radians(la))) > 4 for o in out):  # one peak per system
            out.append((la, lo, float(core[y, x])))
    return out


def loop_times(frame, step=10):
    """The loop's frames, oldest first: every 30 minutes from 24 h back to 6 h back, then every step minutes (each scan) to
    the frame. The page plays 12 and 24 hours from the 30-minute frames and 6 hours from all of them."""
    n = 360 // step
    return [frame - timedelta(minutes=30 * k) for k in range(48, 12, -1)] + [frame - timedelta(minutes=step * k) for k in range(n, -1, -1)]


def best_sat(lat, lon):
    return max(SATS, key=lambda s: view_cos(lat, lon, SATS[s]))


def track(sat, t_end, lat, lon):
    """Eye fixes every 3 h back to 48 h. Stops when the centre loses its deep convection or jumps implausibly."""
    fixes, pts = [], []
    c = N // 2
    for h in range(0, 49, 3):
        when = t_end - timedelta(hours=h)
        if pts and len(pts) >= 2:  # extrapolate the last two fixes for the guess
            (la1, lo1), (la2, lo2) = pts[-1], pts[-2]
            lat_g, lon_g = 2 * la1 - la2, 2 * lo1 - lo2
        elif pts:
            lat_g, lon_g = pts[-1]
        else:
            lat_g, lon_g = lat, lon
        img, got = closeup_near(sat, when, lat_g, lon_g, 512)  # 4 km grid is plenty for finding the centre
        if img is None:
            continue
        z = ndimage.zoom(img, 2, order=1)
        if np.isnan(z[c - 100:c + 100, c - 100:c + 100]).mean() > 0.05:
            print("  stop", got, "centre is outside the satellite's view"); break
        f = np.nan_to_num(z, nan=300.0)
        ex, ey, teye, tring, score = find_eye(f, (c, c), 120 if not pts else 60)
        if tring > 240:
            print("  stop", got, "no deep convection near the centre"); break
        clear = teye > 260 and score > 50
        if not clear:  # no eye: the centre of the coldest cloud mass near the guess
            sub = ndimage.gaussian_filter(f, 12)[c - 80:c + 80, c - 80:c + 80]
            dy, dx = np.unravel_index(np.argmin(sub), sub.shape); ex, ey = c - 80 + dx, c - 80 + dy
        la, lo = pix_latlon(lat_g, lon_g, ex, ey)
        if pts and np.hypot(la - pts[-1][0], (lo - pts[-1][1]) * np.cos(np.radians(la))) * 60 / 3 > 35:
            print("  stop", got, "fix jumped faster than 35 kt"); break
        pts.append((la, lo))
        fixes.append({"t": got.strftime("%Y-%m-%dT%H:%MZ"), "lat": round(la, 2), "lon": round(lo, 2),
                      "eyeK": round(teye, 1), "ringK": round(tring, 1), "eyeClear": bool(clear)})
        print("  fix", fixes[-1])
    return fixes[::-1]


def measure(crop):
    half = N // 2
    lab, _ = ndimage.label(crop > 250)
    eye_d = 2 * np.sqrt((lab == lab[half, half]).sum() * 4.0 / np.pi) if crop[half, half] > 250 else None
    if eye_d and eye_d > 150:  # the warm spot isn't closed off by the eyewall
        eye_d = None
    r = np.hypot(*np.mgrid[-half:half, -half:half]) * 2
    cdo_r = float(np.sqrt(((crop < 210) & (r < 400)).sum() * 4 / np.pi))
    return (float(crop[half - 2:half + 3, half - 2:half + 3].max()), float(ndimage.minimum_filter(crop, 31)[half, half]),
            round(eye_d) if eye_d else None, round(cdo_r))


def dlon(a, b):
    """b - a in degrees of longitude, the short way round (across the dateline too)."""
    return (b - a + 180) % 360 - 180


def motion(fixes):
    clear = [t for t in fixes if t["eyeClear"]] or fixes
    a, b = clear[max(len(clear) - 8, 0)], clear[-1]
    hrs = max((datetime.strptime(b["t"], "%Y-%m-%dT%H:%MZ") - datetime.strptime(a["t"], "%Y-%m-%dT%H:%MZ")).total_seconds() / 3600, 1)
    dy = (b["lat"] - a["lat"]) * 60; dx = dlon(a["lon"], b["lon"]) * 60 * np.cos(np.radians((a["lat"] + b["lat"]) / 2))
    return {"kt": round(float(np.hypot(dx, dy) / hrs), 1), "bearing": round(float((np.degrees(np.arctan2(dx, dy)) + 360) % 360)), "hours": round(hrs)}


def seed_fixes(off, fixes, frame):
    """The IR track, or when the IR can't follow the centre (no deep convection left), the advisory position alone."""
    return fixes or [{"t": frame.strftime("%Y-%m-%dT%H:%MZ"), "lat": off["lat"], "lon": off["lon"],
                      "eyeK": 0.0, "ringK": 0.0, "eyeClear": False}]


def official_now(off, frame):
    """The advisory position carried on to the frame time along the last 6 h of the official track (at most 12 h on)."""
    h = off.get("history") or []
    if len(h) < 2:
        return off["lat"], off["lon"]
    (t1, la1, lo1, _), (t2, la2, lo2, _) = h[-2], h[-1]
    p = lambda t: datetime.strptime(t, "%Y-%m-%dT%H:%MZ")
    k = min((frame - p(t2)).total_seconds(), 12 * 3600) / max((p(t2) - p(t1)).total_seconds(), 1)
    return round(la2 + (la2 - la1) * k, 2), round(dlon(0, lo2 + dlon(lo1, lo2) * k), 2)


def trusted(now, fixes):
    """The IR track only when its newest fix is a clear eye within 60 km of the official position; the centre of the
    coldest cloud is often well off a storm's real centre, and then the advisory position is the better one."""
    if not fixes or not fixes[-1]["eyeClear"]:
        return []
    la, lo = now; f = fixes[-1]
    return fixes if np.hypot(f["lat"] - la, dlon(lo, f["lon"]) * np.cos(np.radians(la))) * 111.2 <= 60 else []


def storm_motion(fixes, off=None):
    """Motion from the IR fixes, or from the last 6 h of the official track when the IR gave only the seeded position."""
    h = (off or {}).get("history") or []
    if len(fixes) > 1 or len(h) < 2:
        return motion(fixes)
    return motion([{"t": t, "lat": la, "lon": lo, "eyeClear": False} for t, la, lo, _ in h[-2:]]) | {"official": True}


def choose(officials, sees):
    """Split official storms into those to image (a satellite here sees them; at most MAX_STORMS) and the rest."""
    seen = [o for o in officials if sees(o["lat"], o["lon"])]
    imaged = seen[:MAX_STORMS]
    return imaged, [o for o in officials if o not in imaged]


def end_time(sat, frame):
    """The storm's latest scan: the frame itself, or the last Meteosat scan before it."""
    return M.slot(sat, frame) if sat in M.SATS else frame


def sat_for(lat, lon):
    s = best_sat(lat, lon)
    return s if view_cos(lat, lon, SATS[s]) >= 0.3 else None  # too close to the edge of every disc (e.g. the Meteosat gap)


OFFICIAL_KEYS = ("name", "kind", "agency", "windKt", "pressureMb", "advisory", "history")


def main():
    out = sys.argv[1]
    frame = datetime.strptime(os.environ["FRAME"], "%Y-%m-%dT%H:%M")
    imaged, others = choose(advisories.active_storms(), sat_for)
    for o in imaged:
        print(f"official {o['id']} {o['name']} {o['lat']} {o['lon']}")
    storms = [{"sat": sat_for(o["lat"], o["lon"]), "fixes": None, "off": o} for o in imaged]
    for s in storms:
        la, lo = official_now(s["off"], frame)
        try:
            fixes = trusted((la, lo), track(s["sat"], end_time(s["sat"], frame), la, lo))
        except Exception as e:  # e.g. the centre left the disc; the advisory position still gets its close-up
            print(f"  {s['off']['id']}: no IR track ({e})"); fixes = []
        s["fixes"] = seed_fixes({"lat": la, "lon": lo}, fixes, frame)
    for la, lo, core in ([] if storms else candidates(os.path.join(HERE, "stitched_4096.npz"))):
        sat = best_sat(la, lo)
        print(f"candidate {la:.1f} {lo:.1f} core {core:.2f} via {sat}")
        if view_cos(la, lo, SATS[sat]) < 0.3:  # too close to the edge of every disc (e.g. the Meteosat gap)
            print("  dropped: no satellite here sees it well enough"); continue
        try:
            fixes = track(sat, end_time(sat, frame), la, lo)
        except Exception as e:
            print(f"  dropped: no IR track ({e})"); continue
        # a storm, not a passing thunderstorm cluster: a centre that holds together for at least a day
        span = (datetime.strptime(fixes[-1]["t"], "%Y-%m-%dT%H:%MZ") - datetime.strptime(fixes[0]["t"], "%Y-%m-%dT%H:%MZ")).total_seconds() / 3600 if fixes else 0
        latest = datetime.strptime(fixes[-1]["t"], "%Y-%m-%dT%H:%MZ") if fixes else None
        if span < MIN_SPAN_H or abs((latest - frame).total_seconds()) > 1200:
            print(f"  dropped: centre held for {span:.0f} h"); continue
        storms.append({"sat": sat, "fixes": fixes})
    # official storms come strongest first; IR ones by the coldest eyewall ring, at most MAX_STORMS
    if not imaged:
        storms.sort(key=lambda s: s["fixes"][-1]["ringK"] - (15 if s["fixes"][-1]["eyeClear"] else 0))
        storms = storms[:MAX_STORMS]

    shutil.rmtree(os.path.join(out, "storms"), ignore_errors=True)
    meta_storms = []
    for s in storms:
        sid = f"s{len(meta_storms) + 1}"
        eye = dict(s["fixes"][-1])
        end = end_time(s["sat"], frame)
        try:
            crop, _ = closeup_near(s["sat"], end, eye["lat"], eye["lon"])
            if crop is None:
                raise FileNotFoundError(f"no {SAT_NAMES[s['sat']]} picture near {end:%H:%M}")
        except Exception as e:  # one storm that can't be cut never stops the run; an official one is still listed
            print(f"  no close-up at {eye['lat']} {eye['lon']}: skipped ({e})")
            others += [s["off"]] if s.get("off") else []
            continue
        folder = os.path.join(out, "storms", sid); os.makedirs(os.path.join(folder, "loop"))
        first, fix = crop, (eye["lat"], eye["lon"])
        if eye["eyeClear"]:  # the track works on a 4 km grid; centre the 2 km close-up on the warmest pixel of the eye
            c = N // 2; sub = ndimage.uniform_filter(np.nan_to_num(crop[c - 10:c + 11, c - 10:c + 11], nan=0.0), 3)
            dy, dx = np.unravel_index(np.argmax(sub), sub.shape)
            eye["lat"], eye["lon"] = (round(v, 2) for v in pix_latlon(eye["lat"], eye["lon"], c - 10 + dx, c - 10 + dy))
            s["fixes"][-1].update(lat=eye["lat"], lon=eye["lon"])
            crop = closeup_near(s["sat"], end, eye["lat"], eye["lon"])[0]
            if crop is None:  # gone between the two requests: keep the first close-up, centred on the track's fix
                crop, (eye["lat"], eye["lon"]) = first, fix
                s["fixes"][-1].update(lat=eye["lat"], lon=eye["lon"])
        crop = np.nan_to_num(crop, nan=290.0)
        Image.fromarray((code(crop) * 255 + 0.5).astype(np.uint8)).save(os.path.join(folder, "storm_ir.png"), optimize=True)
        eye_k, ring_k, eye_d, cdo_r = measure(crop)
        times, n = [], 0

        def loop_frame(when):  # on the same grid as the close-up
            try:
                return closeup(s["sat"], when, eye["lat"], eye["lon"])
            except Exception as e:  # e.g. a scan that isn't out, or the map service gave up
                print("  loop skipped", when, e)
        whens = loop_times(end, STEP[s["sat"]])
        # the map service answers one picture at a time, so ask it four at once; the S3 satellites go one by one as before
        with cf.ThreadPoolExecutor(4 if s["sat"] in M.SATS else 1) as ex:
            frames = list(ex.map(loop_frame, whens))
        for when, im in zip(whens, frames):
            if im is None:
                continue
            Image.fromarray((code(np.nan_to_num(im, nan=290.0)) * 255 + 0.5).astype(np.uint8)) \
                .save(os.path.join(folder, "loop", f"f{n:02d}.jpg"), quality=92)
            n += 1; times.append(when.strftime("%Y-%m-%dT%H:%MZ"))
        meta_storms.append({
            "id": sid, "satellite": SAT_NAMES[s["sat"]], "eye": {"lat": eye["lat"], "lon": eye["lon"]},
            "eyeK": round(eye_k, 1), "ringK": round(ring_k, 1), "eyeDiameterKm": eye_d, "cdoRadiusKm": cdo_r,
            "motion": storm_motion(s["fixes"], s.get("off")), "loopFrames": n, "loopTimes": times,
            "track": [[t["t"], t["lat"], t["lon"], int(t["eyeClear"])] for t in s["fixes"]],
        })
        if s.get("off"):
            o = s["off"]
            meta_storms[-1].update({k: o[k] for k in OFFICIAL_KEYS}, agencyId=o.get("agencyId", o["id"]))
        print(json.dumps({k: v for k, v in meta_storms[-1].items() if k not in ("track", "loopTimes")}))
    if not meta_storms:
        raise SystemExit("no storms found; keeping the previous data")
    path = os.path.join(out, "meta.json")
    meta = {"frame": frame.strftime("%Y-%m-%dT%H:%MZ"), "storms": meta_storms,
            "others": [{k: o[k] for k in OFFICIAL_KEYS + ("lat", "lon")} | {"agencyId": o.get("agencyId", o["id"])} for o in others]}
    json.dump(meta, open(path, "w"))


if __name__ == "__main__":
    main()
