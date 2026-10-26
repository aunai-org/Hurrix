"""Stitch GOES-19, GOES-18, Himawari-9, Meteosat-12 and Meteosat-9 IR into one equirectangular brightness-temperature
map, plus a cloud-top height map (GOES ACHA and Meteosat's height product where available, IR-derived elsewhere).

load_sats() reads each satellite once; stitch() maps them onto any lon/lat grid (the 4096 world map here, the HD tiles
in tiles.py). The Meteosats come from EUMETSAT's map service (meteosat.py) as lat/lon grids at the map's own pixel size;
if the service fails, the map goes without them, as before. Usage: FRAME=YYYY-MM-DDTHH:MM python3 reproject.py [WIDTH]"""
import glob, os, sys
from datetime import datetime, timedelta
import numpy as np
import netCDF4 as nc

import meteosat as M

GEO = (42164160.0, 6378137.0, 6356752.31414, "y")  # a Meteosat's height from the Earth's centre, WGS84 axes, sweep


def grid(lon_deg, lat_deg):
    """Pixel-centre longitudes and latitudes (degrees, 1-D) to the radian grids stitch() takes: a row and a column that
    broadcast to the full map, so terms that depend on latitude or longitude alone are computed once per row or column."""
    return np.radians(lon_deg)[None, :], np.radians(lat_deg)[:, None]


def scan_angles(LON, LAT, lon0, hsat, a, b, sweep):
    e2 = 1 - b * b / (a * a)
    latc = np.arctan((b * b) / (a * a) * np.tan(LAT))
    rc = b / np.sqrt(1 - e2 * np.cos(latc) ** 2)
    dl = LON - np.radians(lon0)
    sx = hsat - rc * np.cos(latc) * np.cos(dl)
    sy = -rc * np.cos(latc) * np.sin(dl)
    sz = rc * np.sin(latc)
    vis = hsat * (hsat - sx) >= sy * sy + (a * a) / (b * b) * sz * sz
    if sweep == "x":
        x = np.arcsin(-sy / np.sqrt(sx * sx + sy * sy + sz * sz)); y = np.arctan(sz / sx)
    else:
        x = np.arctan(-sy / sx); y = np.arcsin(sz / np.sqrt(sx * sx + sy * sy + sz * sz))
    # view angle weight: cosine of angle between local vertical and satellite direction
    nx, ny, nz = np.cos(LAT) * np.cos(dl), np.cos(LAT) * np.sin(dl), np.sin(LAT)
    px, py, pz = hsat - rc * np.cos(latc) * np.cos(dl), -rc * np.cos(latc) * np.sin(dl), -rc * np.sin(latc)
    cosz = (nx * px - ny * py + nz * pz) / np.sqrt(px * px + py * py + pz * pz)
    return x, y, vis, cosz


def sample(img, xs, ys, x, y):
    """Nearest-neighbour lookup of img (rows=ys, cols=xs, both monotonic) at scan angles x, y."""
    xi = np.rint((x - xs[0]) / (xs[1] - xs[0])).astype(np.int64)
    yi = np.rint((y - ys[0]) / (ys[1] - ys[0])).astype(np.int64)
    ok = (xi >= 0) & (xi < len(xs)) & (yi >= 0) & (yi < len(ys))
    out = np.full(x.shape, np.nan, np.float32)
    out[ok] = img[yi[ok], xi[ok]]
    return out


def load_goes(c13, acha):
    d = nc.Dataset(c13); p = d["goes_imager_projection"]; e = nc.Dataset(acha)
    a, b = p.semi_major_axis, p.semi_minor_axis
    return {"proj": (p.longitude_of_projection_origin, p.perspective_point_height + a, a, b, p.sweep_angle_axis),
            "bt": (np.ma.filled(d["CMI"][:].astype(np.float32), np.nan), d["x"][:], d["y"][:]),
            "ht": (np.ma.filled(e["HT"][:].astype(np.float32), np.nan), e["x"][:], e["y"][:])}


def load_himawari(folder):
    full = np.full((5500, 5500), np.nan, np.float32)
    for f in sorted(glob.glob(os.path.join(folder, "*.nc"))):
        d = nc.Dataset(f)
        r, c = int(d.tile_row_offset), int(d.tile_column_offset)
        v = np.ma.filled(d["Sectorized_CMI"][:].astype(np.float32), np.nan)
        full[r:r + v.shape[0], c:c + v.shape[1]] = v
        known = (c, float(d["x"][0]) * 1e-6, float(d["x"][1] - d["x"][0]) * 1e-6, r, float(d["y"][0]) * 1e-6, float(d["y"][1] - d["y"][0]) * 1e-6)
        proj = d["fixedgrid_projection"]
        a, b, lon0, hp, sweep = proj.semi_major, proj.semi_minor, proj.longitude_of_projection_origin, proj.perspective_point_height, proj.sweep_angle_axis
    c0, x0, dx, r0, y0, dy = known
    xs = x0 + (np.arange(5500) - c0) * dx; ys = y0 + (np.arange(5500) - r0) * dy
    return {"proj": (lon0, hp + a, a, b, sweep), "bt": (full, xs, ys), "ht": None}


def _base_sats(here):
    """GOES-19, GOES-18 and Himawari-9 for the frame fetch.sh downloaded last."""
    return [load_goes(f"{here}/g19_c13.nc", f"{here}/g19_acha.nc"), load_goes(f"{here}/g18_c13.nc", f"{here}/g18_acha.nc"),
            load_himawari(f"{here}/hima/" + open(f"{here}/hima/current").read().strip())]


def load_sats(here, when=None, deg=None):
    """The three S3 satellites, plus Meteosat-12 and Meteosat-9 at the frame time when and deg degrees a pixel."""
    sats = _base_sats(here)
    if when is None or deg is None:
        return sats
    for sat in M.SATS:
        t = M.slot(sat, when)
        try:
            try:
                grid = M.world(sat, t, deg)
            except FileNotFoundError:  # the map service runs 20-30 minutes behind: the scan before
                t -= timedelta(minutes=M.SATS[sat]["step"]); grid = M.world(sat, t, deg)
        except Exception as e:  # the service is slow, down or has neither scan: the map goes without this satellite
            print(f"{sat}: skipped ({e})"); continue
        try:
            try:
                ht = M.cth_world(sat, t, max(deg, 360 / 4096))  # the height product is 3 km at best; the 4096 map's size
            except FileNotFoundError:  # not out yet: the one 15 minutes before
                ht = M.cth_world(sat, t - timedelta(minutes=15), max(deg, 360 / 4096))
        except Exception as e:
            print(f"{sat} cloud-top height: skipped ({e})"); ht = None
        sats.append({"grid": grid, "ht": ht, "lon0": M.SATS[sat]["lon0"], "name": sat})
    return sats


def view(sat, LON, LAT):
    """One satellite on the grid: brightness temperature, cloud-top height (NaN without ACHA) and view cosine (-1 unseen)."""
    if "grid" in sat:  # a Meteosat: sampled on lat/lon, viewing geometry from its sub-satellite longitude
        _, _, vis, cosz = scan_angles(LON, LAT, sat["lon0"], *GEO)
        la, lo = np.degrees(LAT), np.degrees(LON)
        bt = sat["grid"].sample(la, lo); bt[~vis] = np.nan
        ht = sat["ht"].sample(la, lo) if sat["ht"] else np.full_like(bt, np.nan)
        ht[~vis] = np.nan
        return bt, ht, np.where(vis, cosz, -1)
    x, y, vis, cosz = scan_angles(LON, LAT, *sat["proj"])
    bt = sample(*sat["bt"], x, y); bt[~vis] = np.nan
    if sat["ht"] is None:
        ht = np.full_like(bt, np.nan)
    else:
        ht = sample(*sat["ht"], x, y); ht[~vis] = np.nan
    return bt, ht, np.where(vis, cosz, -1)


def stitch(LON, LAT, sats):
    """Pick the satellite with the best view; feather seams by blending the two best within a small margin."""
    cands = [view(s, LON, LAT) for s in sats]
    cos = np.stack([np.where(np.isfinite(c[0]), c[2], -1) for c in cands])
    bts = np.stack([c[0] for c in cands]); hts = np.stack([c[1] for c in cands])
    order = np.argsort(-cos, axis=0)
    best, second = order[0], order[1]
    take = lambda arr, idx: np.take_along_axis(arr, idx[None], 0)[0]
    c1, c2 = take(cos, best), take(cos, second)
    w = np.clip((c1 - c2) / 0.08, 0, 1) * 0.5 + 0.5
    b1, b2 = take(bts, best), take(bts, second)
    bt = np.where(np.isfinite(b2) & (c2 > 0.05), w * b1 + (1 - w) * b2, b1)
    bt = np.where(c1 > 0.05, bt, np.nan)
    h1, h2 = take(hts, best), take(hts, second)
    ht = np.where(np.isnan(h1) & np.isfinite(h2) & (c1 - c2 < 0.08), h2, h1)  # near a seam, the neighbour's height
    coverage = np.clip((c1 - 0.05) / 0.15, 0, 1)  # fades out near the disc edges
    return bt, ht, coverage


def overlap_bias(bt_a, bt_b, cos_a, cos_b):
    """Median a - b where both satellites see well (None if they share too little): a drift check on the calibration."""
    ok = np.isfinite(bt_a) & np.isfinite(bt_b) & (cos_a > 0.3) & (cos_b > 0.3)
    return float(np.median(bt_a[ok] - bt_b[ok])) if ok.sum() >= 1000 else None


def report_bias(sats):
    """Print each Meteosat's bias against its neighbour on a 1024 x 512 map; a big one means the colours changed."""
    LON, LAT = grid(np.linspace(-180, 180, 1024, endpoint=False) + 180 / 1024, np.linspace(90, -90, 512, endpoint=False) - 90 / 512)
    views = {s.get("name"): view(s, LON, LAT) for s in sats if "grid" in s}
    views["goes19"], views["himawari9"] = view(sats[0], LON, LAT), view(sats[2], LON, LAT)
    for a, b in (("meteosat12", "goes19"), ("meteosat9", "himawari9")):
        if a in views:
            bias = overlap_bias(views[a][0], views[b][0], views[a][2], views[b][2])
            if bias is not None:
                print(f"{a} vs {b} overlap bias {bias:.1f} K" + (" WARNING: recalibrate (meteosat.py calibrate)" if abs(bias) > 5 else ""))


if __name__ == "__main__":
    W = int(sys.argv[1]) if len(sys.argv) > 1 else 4096; H = W // 2
    here = os.path.dirname(os.path.abspath(__file__))
    LON, LAT = grid(np.linspace(-180, 180, W, endpoint=False) + 180 / W, np.linspace(90, -90, H, endpoint=False) - 90 / H)
    frame = datetime.strptime(os.environ["FRAME"], "%Y-%m-%dT%H:%M") if os.environ.get("FRAME") else None
    sats = load_sats(here, frame, 360 / W)
    report_bias(sats)
    bt, ht, coverage = stitch(LON, LAT, sats)
    np.savez_compressed(f"{here}/stitched_{W}.npz", bt=bt.astype(np.float32), ht=ht.astype(np.float32), cov=coverage.astype(np.float32))
    print("bt range", np.nanmin(bt), np.nanmax(bt), "coverage", np.mean(coverage > 0), "acha", np.mean(np.isfinite(ht)))
