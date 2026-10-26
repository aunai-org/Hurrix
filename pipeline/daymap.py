"""The live day side: one true-colour picture of the whole sunlit globe at the frame time, which the page lays over the
Earth in place of the fixed Blue Marble wherever a satellite saw it in daylight.

Writes day_global.jpg (4096 x 2048, about 10 km a pixel, the same grid as ir_global.jpg) and day_mask.png (2048 x 1024):
how much of the live picture to show, which fades out toward the night side, the edges of the discs and the poles. There
the page keeps Blue Marble. meta.json lists the satellites used in "day".

- GOES-19 and GOES-18: the 2 km multi-band file (ABI-L2-MCMIPF), bands 1-3, green made from them as visible.py does.
- Himawari-9: bands 1-3 from the tiles, only the tiles in daylight.
- Meteosat-12: EUMETSAT's rendered true colour from its map service. Its colours are matched to GOES-19's where the two
  overlap (per channel, by quantiles), so the seam doesn't show.

The satellite with the best view wins, blended with the runner-up near the seams, as reproject.py does for the IR. If a
satellite fails, the map goes without it; if all fail, nothing is written and the page keeps Blue Marble.

Usage: FRAME=YYYY-MM-DDTHH:MM python3 daymap.py OUT_DIR      (after fetch.sh and storms.py)
"""
import json, os, sys
import concurrent.futures as cf
from datetime import datetime, timedelta
import numpy as np
import netCDF4 as nc
from PIL import Image
from scipy import ndimage

import meteosat as M
import storms as S
import visible as V

W, H = 4096, 2048
KM4 = 4  # every satellite is block-averaged to about 4 km first: finer than the 10 km map, a quarter of the pixels
SATS = ("goes19", "goes18", "himawari9", "meteosat12")


def grid(w=W, h=H):
    """Pixel-centre latitudes and longitudes (degrees, full 2-D) of the w x h world map."""
    lon = np.linspace(-180, 180, w, endpoint=False) + 180 / w
    lat = np.linspace(90, -90, h, endpoint=False) - 90 / h
    LON, LAT = np.meshgrid(lon.astype(np.float32), lat.astype(np.float32))
    return LAT, LON


def block_mean(img, k):
    """Average k x k blocks (NaN-aware: a block with any data keeps the mean of what it has)."""
    h, w = img.shape[0] // k * k, img.shape[1] // k * k
    v = img[:h, :w].reshape(h // k, k, w // k, k)
    ok = np.isfinite(v)
    n = ok.sum(axis=(1, 3))
    s = np.where(ok, v, 0).sum(axis=(1, 3))
    return np.where(n > 0, s / np.maximum(n, 1), np.nan).astype(np.float32)


def shrink(disc, k):
    """A Disc block-averaged by k: pixel 0's centre moves to the middle of the first block."""
    return S.Disc(block_mean(disc.img, k), disc.x0 + disc.dx * (k - 1) / 2, disc.dx * k, disc.y0 + disc.dy * (k - 1) / 2, disc.dy * k, disc.p)


def smooth(x, a, b):
    t = np.clip((x - a) / (b - a), 0, 1)
    return t * t * (3 - 2 * t)


def daylight(mu, sat=None, morning=None):
    """How much of the live picture to show by the sun's height: none below about 2°, all above about 9°. Closer to the
    night the picture is mostly noise and haze stretched by the sun-angle correction. Meteosat-12's rendered picture
    isn't corrected the same way: it goes dark in the evening (shown from 5° to 13°) and hazy white in the morning (from
    17° to 33°)."""
    if sat != "meteosat12":
        return smooth(mu, 0.03, 0.15)
    return np.where(morning, smooth(mu, 0.3, 0.55), smooth(mu, 0.08, 0.22))


def morning(LON, when):
    """Where it is before local noon (west of the subsolar point)."""
    return (LON - V.subsolar(when)[1] + 540) % 360 - 180 < 0


def edge(LAT, LON, box, deg=8.0):
    """1 inside a lat/lon box, falling to 0 over its last deg degrees (Meteosat-12's true colour stops at 70°)."""
    lat0, lat1, lon0, lon1 = box
    dl = (LON - lon0) % 360
    d = np.minimum.reduce([LAT - lat0, lat1 - LAT, dl, (lon1 - lon0) - dl])
    return smooth(d, 0, deg)


# ---------- each satellite as (r, g, b) reflectance discs ----------
def goes(sat, when):
    """GOES bands 1-3 at 4 km from the 2 km multi-band full-disk file."""
    num = sat[-2:]
    keys = [k for k in S.listing(f"noaa-goes{num}", f"ABI-L2-MCMIPF/{when:%Y/%j/%H}/") if f"_G{num}_s{when:%Y%j%H%M}" in k]
    if not keys:
        raise FileNotFoundError(f"no GOES-{num} multi-band file at {when:%Y-%m-%d %H:%M}")
    path = S.download(f"noaa-goes{num}", keys[0], os.path.join(S.CACHE, sat, f"{when:%Y%m%d%H%M}_MCMIP.nc"))
    d = nc.Dataset(path); p = d["goes_imager_projection"]
    proj = {"a": p.semi_major_axis, "b": p.semi_minor_axis, "h": p.perspective_point_height + p.semi_major_axis,
            "lon0": p.longitude_of_projection_origin, "sweep": p.sweep_angle_axis}
    x, y = d["x"][:], d["y"][:]
    discs = [shrink(S.Disc(np.ma.filled(d[f"CMI_C0{k}"][:].astype(np.float32), np.nan), float(x[0]), float(x[1] - x[0]),
                           float(y[0]), float(y[1] - y[0]), proj), 2) for k in (1, 2, 3)]
    d.close()
    b, r, nir = discs
    return r, b, nir  # green is made after sampling (goes_green)


def himawari_tiles(when, LAT, LON, mu):
    """Himawari-9 tile grid cells (row, col of the 10 x 10 layout) that see any daylight on the map."""
    h = S.HIMA = S.HIMA or S.Himawari()
    day = (mu > 0.02) & (S.view_cos(LAT, LON, S.SATS["himawari9"]) > 0.05)
    x, y = S.scan_angles(LAT[day][::7], LON[day][::7], h.p)
    ok = np.isfinite(x)
    cols = ((x[ok] - h.x0) / h.dx).astype(int) // 550; rows = ((y[ok] - h.y0) / h.dy).astype(int) // 550
    return {(int(r), int(c)) for r, c in zip(rows, cols) if 0 <= r < 10 and 0 <= c < 10}


def himawari(when, cells):
    """Himawari-9 bands 1-3 at 4 km, from the tiles in cells (each band's tiles are 1100 or 2200 px; 275 px at 4 km)."""
    h = S.HIMA = S.HIMA or S.Himawari()
    keys = S.listing("noaa-himawari9", f"AHI-L2-FLDK-ISatSS/{when:%Y/%m/%d/%H%M}/")
    folder = os.path.join(S.CACHE, "himawari9", f"{when:%Y%m%d%H%M}")
    out = []
    for band in (1, 2, 3):
        need = [k for k in keys if f"M1C{band:02d}" in k and (rc := h.tiles.get(int(k.split("-T")[1][:3]))) and (rc[0] // 550, rc[1] // 550) in cells]
        if not need:
            raise FileNotFoundError(f"no Himawari band {band} tiles at {when:%Y-%m-%d %H:%M}")
        with cf.ThreadPoolExecutor(16) as ex:
            paths = list(ex.map(lambda k: S.download("noaa-himawari9", k, os.path.join(folder, os.path.basename(k))), need))
        img = np.full((2750, 2750), np.nan, np.float32)
        for path in paths:
            d = nc.Dataset(path)
            v = np.ma.filled(d["Sectorized_CMI"][:].astype(np.float32), np.nan)
            r, c = h.tiles[int(os.path.basename(path).split("-T")[1][:3])]
            img[r // 2:r // 2 + 275, c // 2:c // 2 + 275] = block_mean(v, v.shape[0] // 275)
            d.close()
        out.append(S.Disc(img, h.x0 + h.dx / 2, h.dx * 2, h.y0 + h.dy / 2, h.dy * 2, h.p))
    b, g, r = out
    return r, g, b


def sample(disc, LAT, LON):
    """A reflectance disc on the map, NaN where it has no data."""
    x, y = S.scan_angles(LAT, LON, disc.p)
    ok = np.isfinite(x)
    fc = np.where(ok, (x - disc.x0) / disc.dx, -1); fr = np.where(ok, (y - disc.y0) / disc.dy, -1)
    hh, ww = disc.img.shape
    ok &= (fc >= 0) & (fc <= ww - 1) & (fr >= 0) & (fr <= hh - 1)
    out = np.full(LAT.shape, np.nan, np.float32)
    ci, ri = np.rint(fc[ok]).astype(int), np.rint(fr[ok]).astype(int)
    # bilinear where all four neighbours have data, nearest otherwise (a 10 km map from 4 km pixels: nearest is fine)
    img = disc.img
    lin = ndimage.map_coordinates(np.nan_to_num(img, nan=0.0), [fr[ok], fc[ok]], order=1)
    bad = ndimage.map_coordinates(np.isnan(img).astype(np.float32), [fr[ok], fc[ok]], order=1) > 0
    out[ok] = np.where(bad, img[ri, ci], lin)
    return out


def colour(sat, when, LAT, LON, mu):
    """One satellite's display colour on the map (float32 h x w x 3, NaN where it has nothing)."""
    if sat == "meteosat12":
        t = M.slot(sat, when); box = M.EXTENT[M.SATS[sat]["true"]]; w, h = M._size(box, 360 / W)
        try:
            img = M.image(M.SATS[sat]["true"], t, box, w, h)
        except FileNotFoundError:  # the map service runs 20-30 minutes behind: the scan before
            img = M.image(M.SATS[sat]["true"], t - timedelta(minutes=M.SATS[sat]["step"]), box, w, h)
        rgb = img.astype(np.float32) / 255; rgb[(img == M.NO_DATA).all(-1)] = np.nan
        # off the disc it paints white: GridDisc cuts that off by the view angle, and the mask fades out before it
        return np.stack([M.GridDisc(rgb[..., k], *box, M.SATS[sat]["lon0"], box).sample(LAT, LON) for k in range(3)], -1)
    if sat == "himawari9":
        r, g, b = (sample(d, LAT, LON) for d in himawari(when, himawari_tiles(when, LAT, LON, mu)))
    else:
        r, b, nir = (sample(d, LAT, LON) for d in goes(sat, when))
        g = V.goes_green(b, r, nir)
    return V.tone(r, g, b, mu)


def match(src, ref, ok):
    """Per-channel lookup tables (256 levels) that move src's colours onto ref's where both see (ok), by quantiles; None
    when they share too little daylight (then the saved tables in meteosat_cal.json are used)."""
    if ok.sum() < 5000:
        return None
    luts = []
    for k in range(3):
        a, b = src[..., k][ok], ref[..., k][ok]
        q = np.linspace(0.005, 0.995, 100)
        qa, qb = np.quantile(a, q), np.quantile(b, q)
        # pinned to black and white at the ends, so the brightest cloud tops stay white
        qa = np.maximum.accumulate(qa + np.arange(100) * 1e-6)  # strictly increasing for interp
        qa = np.concatenate([[0.0], qa, [max(1.0, qa[-1] + 1e-6)]])
        qb = np.concatenate([[0.0], qb, [1.0]])
        luts.append(np.clip(np.interp(np.linspace(0, 1, 256), qa, qb), 0, 1))
    return luts


def shared(a, b, cos_a, cos_b, mu):
    """Where two satellites both see well in good daylight."""
    return (cos_a > 0.3) & (cos_b > 0.3) & (mu > 0.25) & np.isfinite(a[..., 0]) & np.isfinite(b[..., 0])


def calibrate(frame):
    """Save Meteosat-12's colour match against GOES-19 at a frame with plenty of shared daylight (about 13-17 UTC) into
    meteosat_cal.json, for the runs when the two share too little (python3 daymap.py calibrate YYYY-MM-DDTHH:MM)."""
    LAT, LON = grid(1024, 512)
    mu = V.cos_sza(LAT, LON, frame).astype(np.float32)
    a, b = colour("meteosat12", frame, LAT, LON, mu), colour("goes19", frame, LAT, LON, mu)
    luts = match(a, b, shared(a, b, S.view_cos(LAT, LON, 0.0), S.view_cos(LAT, LON, S.SATS["goes19"]), mu))
    if luts is None:
        raise SystemExit("too little shared daylight at that frame")
    cal = json.load(open(M.CAL_PATH))
    cal["truecolour"] = [[round(float(v), 4) for v in t] for t in luts]
    json.dump(cal, open(M.CAL_PATH, "w"))
    print("saved Meteosat-12 colour match from", frame)


def apply(luts, rgb):
    idx = np.clip(np.nan_to_num(rgb, nan=0) * 255 + 0.5, 0, 255).astype(int)
    out = np.stack([luts[k][idx[..., k]] for k in range(3)], -1).astype(np.float32)
    return np.where(np.isnan(rgb), np.nan, out)


def stitch(rgbs, coss, quals):
    """The best source's colour, blended with the runner-up near the seams, and how much of it to show. Sources rank by
    quality (how well they see, how high the sun is for them), then by view: where one fades out, the next takes over
    instead of leaving a hole."""
    q = np.stack([np.where(np.isfinite(c[..., 0]), v, 0) for c, v in zip(rgbs, quals)])
    key = q * (1 + np.stack(coss))
    order = np.argsort(-key, axis=0)
    take = lambda arr, idx: np.take_along_axis(arr, idx[None], 0)[0]
    k1 = take(key, order[0])
    k2 = take(key, order[1]) if len(rgbs) > 1 else np.zeros_like(k1)
    w = np.clip((k1 - k2) / 0.1, 0, 1) * 0.5 + 0.5
    stack = np.stack(rgbs)
    pick = lambda idx: np.take_along_axis(stack, idx[None, ..., None], 0)[0]
    a = pick(order[0]); b = pick(order[1]) if len(rgbs) > 1 else a
    qa = take(q, order[0]); qb = take(q, order[1]) if len(rgbs) > 1 else np.zeros_like(qa)
    blend = (qb > 0) & np.isfinite(b[..., 0])
    rgb = np.where(blend[..., None], w[..., None] * a + (1 - w[..., None]) * b, a)
    return rgb, np.where(blend, w * qa + (1 - w) * qb, qa)


def main():
    out = sys.argv[1]
    frame = datetime.strptime(os.environ["FRAME"], "%Y-%m-%dT%H:%M")
    LAT, LON = grid()
    mu = V.cos_sza(LAT, LON, frame).astype(np.float32)
    am = morning(LON, frame)
    rgbs, coss, quals, used = [], [], [], []
    for sat in SATS:
        try:
            rgb = colour(sat, frame, LAT, LON, mu)
        except Exception as e:  # a missing file or the map service down: the map goes without this satellite
            print(f"day map {sat}: skipped ({e})"); continue
        cos = S.view_cos(LAT, LON, S.SATS[sat]).astype(np.float32)
        q = np.clip((cos - 0.08) / 0.15, 0, 1) * daylight(mu, sat, am)
        if sat in M.SATS:  # a map-service picture also stops at its layer's box: fade it out before the cut
            q = q * edge(LAT, LON, M.EXTENT[M.SATS[sat]["true"]])
        rgbs.append(rgb); coss.append(cos); quals.append(q.astype(np.float32)); used.append(sat)
        print(f"day map {sat}: {np.mean(np.isfinite(rgb[..., 0]) & (mu > 0.03)):.0%} of the map in daylight")
    if not rgbs:
        print("day map: no satellite, the page keeps Blue Marble"); return
    if "meteosat12" in used:
        i, luts = used.index("meteosat12"), None
        if "goes19" in used:
            j = used.index("goes19")
            both = shared(rgbs[i], rgbs[j], coss[i], coss[j], mu)
            luts = match(rgbs[i], rgbs[j], both)
        if luts is not None:
            print(f"day map meteosat12: colours matched to goes19 over {both.sum()} pixels")
        elif "truecolour" in M.CAL:
            luts = [np.asarray(t) for t in M.CAL["truecolour"]]; print("day map meteosat12: too little shared daylight, saved colour match")
        if luts is not None:
            rgbs[i] = apply(luts, rgbs[i])
    rgb, mask = stitch(rgbs, coss, quals)
    mask = mask * np.isfinite(rgb[..., 0])
    rgb = np.nan_to_num(rgb, nan=0.0)
    Image.fromarray((np.clip(rgb, 0, 1) * 255 + 0.5).astype(np.uint8)).save(os.path.join(out, "day_global.jpg"), quality=90)
    small = mask.reshape(H // 2, 2, W // 2, 2).mean(axis=(1, 3))
    Image.fromarray((small * 255 + 0.5).astype(np.uint8)).save(os.path.join(out, "day_mask.png"), optimize=True)
    path = os.path.join(out, "meta.json")
    meta = json.load(open(path))
    meta["day"] = {"satellites": [S.SAT_NAMES[s] for s in used]}
    json.dump(meta, open(path, "w"))
    print(f"day map: {np.mean(mask > 0.5):.0%} of the globe live in daylight, from {', '.join(used)}")


if __name__ == "__main__":
    if sys.argv[1:2] == ["calibrate"]:
        calibrate(datetime.strptime(sys.argv[2], "%Y-%m-%dT%H:%M"))
    else:
        main()
