"""True-colour close-ups: the visible channels of the satellite that tracks each storm, on the storm's tangent grid.

Writes data/storms/sN/storm_vis.jpg (2048 px over 2048 km, 1 km pixels): red, green and blue reflectance divided by the
cosine of the solar zenith angle, so the picture reads as surface and cloud colour whatever the sun angle. The page fades
it out where the sun was low or down at the frame time and shows infrared there instead.

Himawari-9 has real blue, green and red bands (1, 2, 3). GOES has blue and red (1, 2) plus a near-infrared "veggie" band
(3); its green is synthesised from the three the way CIMSS does it.

Meteosat storms take EUMETSAT's own rendered pictures from its map service (meteosat.py), as they come: Meteosat-12's
true colour, also for Meteosat-9 storms it sees well (west of about 65° E), and Meteosat-9's natural colour further east,
a false-colour mix (land green, ice clouds cyan) that meta.json marks with "visKind": "natural".

It also cuts true-colour loop frames (1024 px, loop/vNN.jpg) every 30 minutes over the last 6 hours, where it was daylight;\nmeta.json lists their times in visTimes.

Usage: FRAME=YYYY-MM-DDTHH:MM python3 visible.py OUT_DIR      (after storms.py has written OUT_DIR/meta.json)
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

NV, KMV = 2048, 2048.0  # 1 km pixels over the same 2048 km square as the infrared close-up


def subsolar(when):
    """Subsolar latitude and longitude (degrees), good to a few tenths of a degree."""
    d = (when - datetime(2000, 1, 1, 12)).total_seconds() / 86400
    g, q = np.radians(357.529 + 0.98560028 * d), np.radians(280.459 + 0.98564736 * d)
    lam = q + np.radians(1.915) * np.sin(g) + np.radians(0.020) * np.sin(2 * g)
    eps = np.radians(23.439 - 0.00000036 * d)
    dec = np.arcsin(np.sin(eps) * np.sin(lam))
    ra = np.arctan2(np.cos(eps) * np.sin(lam), np.cos(lam))
    gmst = (280.46061837 + 360.98564736629 * d) % 360
    return np.degrees(dec), (np.degrees(ra) - gmst + 540) % 360 - 180


def cos_sza(lat, lon, when):
    dec, lon_s = subsolar(when)
    la, de = np.radians(lat), np.radians(dec)
    return np.sin(la) * np.sin(de) + np.cos(la) * np.cos(de) * np.cos(np.radians(lon - lon_s))


def hima_band(band, when, lat, lon):
    """One Himawari visible band around the storm. Tiles are 550 px at 2 km, so 1100 px at 1 km and 2200 px at 0.5 km."""
    hima = S.HIMA = S.HIMA or S.Himawari()
    la, lo = S.grid_latlon(lat, lon, 32, KMV)
    x, y = S.scan_angles(la, lo, hima.p)
    cols = (x[np.isfinite(x)] - hima.x0) / hima.dx; rows = (y[np.isfinite(y)] - hima.y0) / hima.dy
    if not cols.size:
        raise ValueError("storm is outside the Himawari disc")
    r0, r1 = int(max(rows.min(), 0)) // 550, int(min(rows.max(), 5499)) // 550
    c0, c1 = int(max(cols.min(), 0)) // 550, int(min(cols.max(), 5499)) // 550
    keys = [k for k in S.listing("noaa-himawari9", f"AHI-L2-FLDK-ISatSS/{when:%Y/%m/%d/%H%M}/") if f"M1C{band:02d}" in k]
    need = [k for k in keys if (rc := hima.tiles.get(int(k.split("-T")[1][:3]))) and r0 <= rc[0] // 550 <= r1 and c0 <= rc[1] // 550 <= c1]
    if not need:
        raise FileNotFoundError(f"no Himawari band {band} tiles at {when:%Y-%m-%d %H:%M}")
    folder = os.path.join(S.CACHE, "himawari9", f"{when:%Y%m%d%H%M}")
    with cf.ThreadPoolExecutor(16) as ex:
        paths = list(ex.map(lambda k: S.download("noaa-himawari9", k, os.path.join(folder, os.path.basename(k))), need))
    first = nc.Dataset(paths[0]); t = first["Sectorized_CMI"].shape[0]; f = t // 550
    xs, ys = first["x"][:] * 1e-6, first["y"][:] * 1e-6
    dx, dy = float(xs[1] - xs[0]), float(ys[1] - ys[0])
    x0 = float(xs[0]) - int(first.tile_column_offset) * dx; y0 = float(ys[0]) - int(first.tile_row_offset) * dy
    first.close()
    img = np.full(((r1 - r0 + 1) * t, (c1 - c0 + 1) * t), np.nan, np.float32)
    for path in paths:
        d = nc.Dataset(path); r, c = int(d.tile_row_offset), int(d.tile_column_offset)
        v = np.ma.filled(d["Sectorized_CMI"][:].astype(np.float32), np.nan)
        img[r - r0 * t:r - r0 * t + v.shape[0], c - c0 * t:c - c0 * t + v.shape[1]] = v
        d.close()
    if f == 4:  # 0.5 km red band: average 2x2 down to 1 km so it matches the other two
        img = img[: img.shape[0] // 2 * 2, : img.shape[1] // 2 * 2].reshape(img.shape[0] // 2, 2, img.shape[1] // 2, 2).mean((1, 3))
        x0, dx, y0, dy = x0 + dx / 2, dx * 2, y0 + dy / 2, dy * 2
    tw, th = img.shape[1] // (c1 - c0 + 1), img.shape[0] // (r1 - r0 + 1)  # tile size at the final resolution
    return S.Disc(img, x0 + c0 * tw * dx, dx, y0 + r0 * th * dy, dy, hima.p)


def goes_band(sat, band, when, lat, lon):
    """One GOES visible band around the storm, read as a window from the full-disk file (band 2 is 0.5 km)."""
    num = sat[-2:]
    keys = [k for k in S.listing(f"noaa-goes{num}", f"ABI-L2-CMIPF/{when:%Y/%j/%H}/") if f"C{band:02d}_G{num}" in k and f"_s{when:%Y%j%H%M}" in k]
    if not keys:
        raise FileNotFoundError(f"no GOES-{num} band {band} at {when:%Y-%m-%d %H:%M}")
    path = S.download(f"noaa-goes{num}", keys[0], os.path.join(S.CACHE, sat, f"{when:%Y%m%d%H%M}_C{band:02d}.nc"))
    d = nc.Dataset(path); p = d["goes_imager_projection"]
    proj = {"a": p.semi_major_axis, "b": p.semi_minor_axis, "h": p.perspective_point_height + p.semi_major_axis,
            "lon0": p.longitude_of_projection_origin, "sweep": p.sweep_angle_axis}
    xs, ys = d["x"][:], d["y"][:]
    dx, dy = float(xs[1] - xs[0]), float(ys[1] - ys[0])
    la, lo = S.grid_latlon(lat, lon, 32, KMV)
    x, y = S.scan_angles(la, lo, proj)
    cols = (x[np.isfinite(x)] - float(xs[0])) / dx; rows = (y[np.isfinite(y)] - float(ys[0])) / dy
    c0, c1 = int(max(cols.min() - 4, 0)), int(min(cols.max() + 4, len(xs) - 1))
    r0, r1 = int(max(rows.min() - 4, 0)), int(min(rows.max() + 4, len(ys) - 1))
    img = np.ma.filled(d["CMI"][r0:r1 + 1, c0:c1 + 1].astype(np.float32), np.nan)
    d.close()
    x0, y0 = float(xs[0]) + c0 * dx, float(ys[0]) + r0 * dy
    if band == 2:
        img = img[: img.shape[0] // 2 * 2, : img.shape[1] // 2 * 2].reshape(img.shape[0] // 2, 2, img.shape[1] // 2, 2).mean((1, 3))
        x0, dx, y0, dy = x0 + dx / 2, dx * 2, y0 + dy / 2, dy * 2
    return S.Disc(img, x0, dx, y0, dy, proj)


def sample(disc, la, lo):
    """Like Disc.sample, but reflectance (fill 0, not 300 K)."""
    x, y = S.scan_angles(la, lo, disc.p)
    ok = np.isfinite(x)
    fc = np.where(ok, (x - disc.x0) / disc.dx, -1); fr = np.where(ok, (y - disc.y0) / disc.dy, -1)
    h, w = disc.img.shape
    ok &= (fc >= 0) & (fc <= w - 1) & (fr >= 0) & (fr <= h - 1)
    out = ndimage.map_coordinates(np.nan_to_num(disc.img, nan=0.0), [np.where(ok, fr, 0), np.where(ok, fc, 0)], order=1)
    return np.where(ok, out, 0.0).astype(np.float32)


def daylit(when, lat, lon):
    """Share of the close-up in daylight, from a coarse grid, so night frames are skipped before any download."""
    la, lo = S.grid_latlon(lat, lon, 64, KMV)
    return float(np.mean(cos_sza(la, lo, when) > 0.1))


def vis_source(sat, lat, lon):
    """Where a Meteosat storm's colour picture comes from: (layer, satellite whose scan times it follows, kind), or None
    for the GOES and Himawari storms, which are built from their own bands."""
    if sat not in M.SATS:
        return None
    lon1 = M.box_for(lat, lon, KMV)[3]  # Meteosat-12's true colour stops at 70° E: the whole close-up must fit
    if sat == "meteosat12" or (S.view_cos(lat, lon, M.SATS["meteosat12"]["lon0"]) >= 0.3 and lon1 <= M.EXTENT[M.SATS["meteosat12"]["true"]][3]):
        return M.SATS["meteosat12"]["true"], "meteosat12", "true"
    return M.SATS["meteosat9"]["natural"], "meteosat9", "natural"


def true_colour(sat, when, lat, lon):
    la, lo = S.grid_latlon(lat, lon, NV, KMV)
    src = vis_source(sat, lat, lon)
    if src:  # already rendered by EUMETSAT: no sun or haze correction
        layer, csat, _ = src
        rgb = M.rgb_disc(layer, csat, M.slot(csat, when), lat, lon, KMV, KMV / NV).sample(la, lo)
        return rgb, float(np.mean(cos_sza(la, lo, when) > 0.1))
    if sat == "himawari9":
        b, g, r = (sample(hima_band(k, when, lat, lon), la, lo) for k in (1, 2, 3))
    else:
        b, r, nir = (sample(goes_band(sat, k, when, lat, lon), la, lo) for k in (1, 2, 3))
        g = goes_green(b, r, nir)
    mu = cos_sza(la, lo, when)
    return (tone(r, g, b, mu) * 255 + 0.5).astype(np.uint8), float(np.mean(mu > 0.1))


def goes_green(b, r, nir):
    """GOES has no green band: make one from blue, red and the near-infrared "veggie" band, the way CIMSS does."""
    return np.clip(0.45 * r + 0.1 * nir + 0.45 * b, 0, 1)


def tone(r, g, b, mu):
    """Reflectances (0..~1.2) and the cosine of the solar zenith angle to display colour (0..1, float32): divided by the
    sun angle so it reads as surface and cloud colour, less blue haze (more at low sun), and a gentle tone curve."""
    norm = 1 / np.clip(mu, 0.12, 1)
    rgb = np.stack([r, g, b], -1) * norm[..., None]
    rgb -= np.array([0.02, 0.03, 0.06]) * (0.6 + 0.4 * norm[..., None]) * (1 - np.clip(rgb.mean(-1, keepdims=True) * 1.6, 0, 1))
    rgb = np.clip(rgb, 0, 1.2) / 1.2
    return np.clip(rgb ** (1 / 1.9), 0, 1).astype(np.float32)


def main():
    out = sys.argv[1]
    frame = datetime.strptime(os.environ["FRAME"], "%Y-%m-%dT%H:%M")
    path = os.path.join(out, "meta.json")
    meta = json.load(open(path))
    sat_of = {v: k for k, v in S.SAT_NAMES.items()}
    for st in meta["storms"]:
        sat = sat_of[st["satellite"]]
        img = None
        for dm in (0, -10, 10) if daylit(frame, st["eye"]["lat"], st["eye"]["lon"]) >= 0.05 else ():
            try:
                img, day = true_colour(sat, frame - timedelta(minutes=dm), st["eye"]["lat"], st["eye"]["lon"]); break
            except (FileNotFoundError, OSError) as e:
                print("  skip", st["id"], dm, e)
        st.pop("visible", None)
        if img is None or day < 0.05:
            print(st["id"], "no daylight visible image at the frame time")
        else:
            Image.fromarray(img).save(os.path.join(out, "storms", st["id"], "storm_vis.jpg"), quality=88)
            st["visible"] = True
            print(st["id"], f"visible: {day:.0%} of the close-up in daylight")
        # true colour for the last 6 hours of the loop, every 30 minutes, where the storm was in daylight; the page
        # holds each one, then dissolves to the next, while the infrared loop plays at 10-minute steps underneath
        st.pop("visLoop", None); st.pop("visTimes", None); st.pop("visKind", None); st.pop("visSatellite", None)
        vis_times = []
        last = datetime.strptime(st["loopTimes"][-1], "%Y-%m-%dT%H:%MZ") if st.get("loopTimes") else frame
        for k in range(12, -1, -1):
            t = last - timedelta(minutes=30 * k)
            if daylit(t, st["eye"]["lat"], st["eye"]["lon"]) < 0.05:
                continue
            try:
                im, day_t = true_colour(sat, t, st["eye"]["lat"], st["eye"]["lon"])
            except (FileNotFoundError, OSError) as e:
                print("  loop skip", t, e); continue
            if day_t < 0.05:
                continue
            Image.fromarray(im).save(os.path.join(out, "storms", st["id"], "loop", f"v{len(vis_times):02d}.jpg"), quality=90)
            vis_times.append(t.strftime("%Y-%m-%dT%H:%MZ"))
        if vis_times:
            st["visTimes"] = vis_times
        src = vis_source(sat, st["eye"]["lat"], st["eye"]["lon"])
        if src and src[2] == "natural" and (vis_times or st.get("visible")):
            st["visKind"] = "natural"
        if src and src[1] != sat and (vis_times or st.get("visible")):
            st["visSatellite"] = S.SAT_NAMES[src[1]]
        n = len(vis_times)
        print(st["id"], f"true-colour loop: {n} frames")
    json.dump(meta, open(path, "w"))


if __name__ == "__main__":
    main()
