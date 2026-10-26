"""The whole-globe infrared loop: the stitched IR map once an hour over the last 24 hours, at 2048 x 1024, so the clouds
move everywhere while the page plays, not only in the storm close-up. Written as globe/gNN.jpg (oldest first, the same
code as ir_global.jpg); meta.json lists their times in globeTimes. The page plays the frame itself from ir_global.jpg.
GOES-19, GOES-18 and Himawari-9 come from NOAA's buckets, Meteosat-12 and Meteosat-9 from EUMETSAT's map service; an
hour without a Meteosat picture keeps archival clouds in its part of the gap instead of being dropped.

Usage: FRAME=YYYY-MM-DDTHH:MM python3 globe.py OUT_DIR      (after export.py and storms.py)
"""
import json, os, shutil, sys, tempfile, warnings
import concurrent.futures as cf
from datetime import datetime, timedelta
import numpy as np
import netCDF4 as nc
from PIL import Image
from scipy import ndimage

import meteosat as M
import storms as S

W, H = 2048, 1024
BLOCK = 4  # average the 2 km satellite pixels in 4 x 4 blocks first: 8 km, still finer than the 20 km map
lon = np.linspace(-180, 180, W, endpoint=False) + 180 / W
lat = np.linspace(90, -90, H, endpoint=False) - 90 / H
LON, LAT = np.meshgrid(lon, lat)


def block_mean(img):
    h, w = img.shape[0] // BLOCK * BLOCK, img.shape[1] // BLOCK * BLOCK
    img = np.where(img > 150, img, np.nan)  # Himawari marks space and missing lines with 0, not a fill value
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)  # all-NaN blocks off the disc edge
        return np.nanmean(img[:h, :w].reshape(h // BLOCK, BLOCK, w // BLOCK, BLOCK), axis=(1, 3))


def to_map(disc):
    """One satellite image (block-averaged) resampled onto the map; NaN where it can't see."""
    if isinstance(disc, M.GridDisc):  # a Meteosat picture already comes at the map's pixel size
        return disc.sample(LAT, LON)
    small = S.Disc(block_mean(disc.img), disc.x0 + disc.dx * (BLOCK - 1) / 2, disc.dx * BLOCK,
                   disc.y0 + disc.dy * (BLOCK - 1) / 2, disc.dy * BLOCK, disc.p)
    return small.sample(LAT, LON)


def goes(sat, when):
    """GOES full-disk band 13: from the close-up cache when storms.py already fetched it, else downloaded and dropped."""
    num = sat[-2:]
    if os.path.exists(os.path.join(S.CACHE, sat, f"{when:%Y%m%d%H%M}.npz")):
        return S.goes_disc(sat, when)
    keys = [k for k in S.listing(f"noaa-goes{num}", f"ABI-L2-CMIPF/{when:%Y/%j/%H}/") if f"C13_G{num}" in k and f"_s{when:%Y%j%H%M}" in k]
    if not keys:
        raise FileNotFoundError(f"no GOES-{num} band 13 at {when:%Y-%m-%d %H:%M}")
    with tempfile.TemporaryDirectory() as tmp:
        d = nc.Dataset(S.download(f"noaa-goes{num}", keys[0], os.path.join(tmp, "c13.nc"))); p = d["goes_imager_projection"]
        x, y = d["x"][:], d["y"][:]
        proj = {"a": p.semi_major_axis, "b": p.semi_minor_axis, "h": p.perspective_point_height + p.semi_major_axis,
                "lon0": p.longitude_of_projection_origin, "sweep": p.sweep_angle_axis}
        disc = S.Disc(np.ma.filled(d["CMI"][:].astype(np.float32), np.nan), float(x[0]), float(x[1] - x[0]), float(y[0]), float(y[1] - y[0]), proj)
        d.close()
    return disc


def himawari(when):
    """All 88 Himawari-9 band-13 tiles as one full disk (cached next to the close-up tiles)."""
    h = S.HIMA = S.HIMA or S.Himawari()
    keys = [k for k in S.listing("noaa-himawari9", f"AHI-L2-FLDK-ISatSS/{when:%Y/%m/%d/%H%M}/") if "M1C13" in k]
    if len(keys) < 80:
        raise FileNotFoundError(f"only {len(keys)} Himawari band-13 tiles at {when:%Y-%m-%d %H:%M}")
    folder = os.path.join(S.CACHE, "himawari9", f"{when:%Y%m%d%H%M}")
    with cf.ThreadPoolExecutor(16) as ex:
        paths = list(ex.map(lambda k: S.download("noaa-himawari9", k, os.path.join(folder, os.path.basename(k))), keys))
    img = np.full((5500, 5500), np.nan, np.float32)
    for path in paths:
        d = nc.Dataset(path); r, c = int(d.tile_row_offset), int(d.tile_column_offset)
        v = np.ma.filled(d["Sectorized_CMI"][:].astype(np.float32), np.nan)
        img[r:r + v.shape[0], c:c + v.shape[1]] = v
        d.close()
    return S.Disc(img, h.x0, h.dx, h.y0, h.dy, h.p)


def near(fetch, when, step=10):
    """A missing slot falls back to its neighbour one scan away, the earlier one first (Himawari skips 02:40 and 14:40
    every day; the newest Meteosat scan may not be out yet)."""
    for dm in (0, step, -step):
        try:
            return to_map(fetch(when - timedelta(minutes=dm)))
        except (FileNotFoundError, OSError) as e:
            err = e
    raise FileNotFoundError(err)


# the satellite with the best view wins, blended with the runner-up near the seams (as reproject.py does)
COS = np.stack([S.view_cos(LAT, LON, S.SATS[s]) for s in ("goes19", "goes18", "himawari9", "meteosat12", "meteosat9")])


FRAME = datetime.strptime(os.environ.get("FRAME", "2000-01-01T00:00"), "%Y-%m-%dT%H:%M")


def stitch(h):
    """The stitched brightness temperature h hours before the frame (each in its own process: netCDF isn't thread-safe)."""
    when = FRAME - timedelta(hours=h)
    try:
        bts = [near(lambda t: goes("goes19", t), when), near(lambda t: goes("goes18", t), when), near(himawari, when)]
    except (FileNotFoundError, OSError) as e:
        print("  globe: skipped", when, e); return when, None
    for sat in ("meteosat12", "meteosat9"):
        try:
            bts.append(near(lambda t: M.world(sat, t, 360 / W), M.slot(sat, when), M.SATS[sat]["step"]))
        except Exception as e:  # the map service failed: archival clouds in that part of the gap, as before
            print(f"  {sat}: skipped ({when}: {e})"); bts.append(np.full(LAT.shape, np.nan, np.float32))
    bts = np.stack(bts)
    cos = np.where(np.isfinite(bts), COS, -1)
    order = np.argsort(-cos, axis=0)
    take = lambda arr, idx: np.take_along_axis(arr, idx[None], 0)[0]
    c1, c2 = take(cos, order[0]), take(cos, order[1])
    b1, b2 = take(bts, order[0]), take(bts, order[1])
    w = np.clip((c1 - c2) / 0.08, 0, 1) * 0.5 + 0.5
    bt = np.where(np.isfinite(b2) & (c2 > 0.05), w * b1 + (1 - w) * b2, b1)
    return when, np.where(c1 > 0.05, bt, np.nan)


def main():
    out = sys.argv[1]
    # live where a satellite sees well, archival clouds in the gap: the same mix as export.py, at this size
    live = np.nan_to_num(np.load(os.path.join(S.HERE, "stitched_4096.npz"))["cov"]).reshape(H, 2, W, 2).mean(axis=(1, 3))
    arch = np.asarray(Image.open(os.path.join(S.HERE, "cache", "clouds.png")).convert("RGBA").getchannel("A").resize((W, H), Image.BILINEAR), np.float32) / 255
    archbt = 292 - arch * 87
    folder = os.path.join(out, "globe")
    shutil.rmtree(folder, ignore_errors=True); os.makedirs(folder)

    times = []
    with cf.ProcessPoolExecutor(3) as ex:
        for when, bt in ex.map(stitch, range(24, 0, -1)):
            if bt is None:
                continue
            blend = live * np.where(np.isfinite(bt), bt, archbt) + (1 - live) * archbt
            Image.fromarray((S.code(blend) * 255 + 0.5).astype(np.uint8)).save(os.path.join(folder, f"g{len(times):02d}.jpg"), quality=88)
            times.append(when.strftime("%Y-%m-%dT%H:%MZ"))
            print("globe", times[-1])
    path = os.path.join(out, "meta.json")
    meta = json.load(open(path)); meta["globeTimes"] = times
    json.dump(meta, open(path, "w"))


if __name__ == "__main__":
    main()
