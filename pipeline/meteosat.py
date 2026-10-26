"""Meteosat-12 (0°) and Meteosat-9 (45.5° E) from EUMETSAT's public map service (EUMETView WMS, no account), so the
gap between the GOES and Himawari discs, Europe, Africa and the Indian Ocean, gets live imagery too. © EUMETSAT.

The service hands out pictures, not data: Meteosat-12 IR as raw 8-bit counts (an identity colour ramp), Meteosat-9 IR as
its default grey, cloud-top height as a colour picture. meteosat_cal.json turns them back into kelvin and metres; it is
fitted against GOES-19 and Himawari-9 where the discs overlap (python3 meteosat.py calibrate DATE).

The rest of the pipeline gets a GridDisc: a regular lat/lon raster with the same sample(lat, lon) as storms.Disc.
"""
import concurrent.futures as cf
import hashlib, http.client, io, json, math, os, sys, time, urllib.parse, urllib.request
from datetime import datetime, timedelta
import numpy as np
from PIL import Image
from scipy import ndimage

URL = "https://view.eumetsat.int/geoserver/ows"
MAX_PX = 2048  # the service is quick up to 2048 px a side and refuses much more

SATS = {
    "meteosat12": {"name": "Meteosat-12", "lon0": 0.0, "step": 10, "ir": "mtg_fd:ir105_hrfi", "raw": True,
                   "cth": "msg_fes:cth", "true": "mtg_fd:rgb_truecolour", "natural": None},
    "meteosat9": {"name": "Meteosat-9", "lon0": 45.5, "step": 15, "ir": "msg_iodc:ir108", "raw": False,
                  "cth": "msg_iodc:cth", "true": "mtg_fd:rgb_truecolour", "natural": "msg_iodc:rgb_natural"},
}

# the raw 8-bit value of each pixel: a ramp from black at 0 to white at 255
import storms as S  # noqa: E402  (after SATS: storms reads it while it loads)

# each layer's extent (lat0, lat1, lon0, lon1) from the capabilities: outside it the service paints white
EXTENT = {"mtg_fd:ir105_hrfi": (-70.0, 70.0, -70.0, 70.0), "mtg_fd:rgb_truecolour": (-70.0, 70.0, -70.0, 70.0),
          "msg_fes:cth": (-77.0, 77.0, -77.0, 77.0), "msg_iodc:ir108": (-77.0, 77.0, -35.5, 118.5),
          "msg_iodc:cth": (-77.0, 77.0, -35.5, 118.5), "msg_iodc:rgb_natural": (-77.0, 77.0, -35.5, 118.5)}

# the rendered colour pictures; asked for with a transparent background
COLOUR = {"mtg_fd:rgb_truecolour", "msg_iodc:rgb_natural"}

CAL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "meteosat_cal.json")
CAL = json.load(open(CAL_PATH)) if os.path.exists(CAL_PATH) else {}

RAW_SLD = ('<StyledLayerDescriptor version="1.0.0" xmlns="http://www.opengis.net/sld" xmlns:ogc="http://www.opengis.net/ogc">'
           '<NamedLayer><Name>{layer}</Name><UserStyle><FeatureTypeStyle><Rule><RasterSymbolizer><ColorMap type="ramp">'
           '<ColorMapEntry color="#000000" quantity="0"/><ColorMapEntry color="#ffffff" quantity="255"/>'
           '</ColorMap></RasterSymbolizer></Rule></FeatureTypeStyle></UserStyle></NamedLayer></StyledLayerDescriptor>')


def slot(sat, when):
    """The latest scan at or before when (every 10 minutes for Meteosat-12, 15 for Meteosat-9)."""
    step = SATS[sat]["step"]
    when = when.replace(second=0, microsecond=0)
    return when - timedelta(minutes=when.minute % step)


def wms_params(layer, when, box, w, h, raw):
    """A GetMap request for box = (lat0, lat1, lon0, lon1); WMS 1.3.0 with EPSG:4326 wants the bbox in lat,lon order."""
    lat0, lat1, lon0, lon1 = box
    p = {"service": "WMS", "version": "1.3.0", "request": "GetMap", "layers": layer, "styles": "", "crs": "EPSG:4326",
         "bbox": ",".join(f"{v:g}" for v in (lat0, lon0, lat1, lon1)), "width": str(w), "height": str(h),
         "format": "image/png", "time": f"{when:%Y-%m-%dT%H:%M}:00.000Z"}
    if raw:
        p["SLD_BODY"] = RAW_SLD.format(layer=layer)
    if layer in COLOUR:  # otherwise it paints the night side white, like cloud
        p["transparent"] = "TRUE"
    return p


def pieces(box, w, h, max_px=MAX_PX):
    """Split a w x h request into pieces of at most max_px a side: (box, w, h, row, col) with each piece's pixel offset."""
    lat0, lat1, lon0, lon1 = box
    nc, nr = math.ceil(w / max_px), math.ceil(h / max_px)
    cols = [w * i // nc for i in range(nc + 1)]; rows = [h * i // nr for i in range(nr + 1)]
    out = []
    for r0, r1 in zip(rows, rows[1:]):
        for c0, c1 in zip(cols, cols[1:]):
            sub = (lat1 - (lat1 - lat0) * r1 / h, lat1 - (lat1 - lat0) * r0 / h, lon0 + (lon1 - lon0) * c0 / w, lon0 + (lon1 - lon0) * c1 / w)
            out.append((sub, c1 - c0, r1 - r0, r0, c0))
    return out


NO_DATA = (0, 1, 0)  # what image() paints where the service's picture is transparent; not a colour any layer uses


def levels(sat, rgb):
    """The count or grey level of each pixel and where it holds one. Meteosat-12's raw counts read 0 for no data;
    Meteosat-9's grey draws its warm end (hot land) as (0, 0, 1), which is level 0, and anything else coloured isn't data."""
    rgb = np.asarray(rgb)
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    if SATS[sat]["raw"]:
        return r, (r > 0) & (g == r)
    return r, ((r == g) & (g == b)) | ((r == 0) & (g == 0) & (b == 1))


def to_bt(sat, rgb, cal):
    """Brightness temperature (K) from a picture: a count or grey level looked up in the calibration table."""
    table = np.asarray([np.nan if v is None else v for v in cal["ir"][sat]], np.float32)
    v, ok = levels(sat, rgb)
    return np.where(ok, table[v], np.nan).astype(np.float32)


def decode_cth(rgb, cal):
    """Cloud-top height (m) from the colour picture: each 4-bit colour bucket has a height (null = clear sky)."""
    lut = np.full(16 ** 3, np.nan, np.float32)
    for r4, g4, b4, m in cal["cth"]:
        if m is not None:
            lut[(r4 * 16 + g4) * 16 + b4] = m
    rgb = np.asarray(rgb).astype(np.int32)
    q = rgb >> 4
    h = lut[(q[..., 0] * 16 + q[..., 1]) * 16 + q[..., 2]]
    return np.where((rgb == NO_DATA).all(-1), np.nan, h).astype(np.float32)


def _interp(img, fr, fc):
    """Bilinear lookup at fractional rows and columns of any shape (map_coordinates wants at least one dimension)."""
    return ndimage.map_coordinates(img, [fr.ravel(), fc.ravel()], order=1).reshape(fr.shape)


class GridDisc:
    """One satellite picture on a lat/lon grid: img[row, col], row 0 at lat1, pixel centres spread evenly over the box.
    extent (lat0, lat1, lon0, lon1), when given, is where the picture holds data at all."""
    def __init__(self, img, lat0, lat1, lon0, lon1, lon_sub, extent=None):
        self.img, self.lat0, self.lat1, self.lon0, self.lon1, self.lon_sub = img, lat0, lat1, lon0, lon1, lon_sub
        self.extent = extent

    def _rows_cols(self, lat, lon):
        lat, lon = np.broadcast_arrays(np.asarray(lat, np.float64), np.asarray(lon, np.float64))
        lon = (lon - self.lon0) % 360 + self.lon0
        h, w = self.img.shape[:2]
        fr = (self.lat1 - lat) / (self.lat1 - self.lat0) * h - 0.5
        fc = (lon - self.lon0) / (self.lon1 - self.lon0) * w - 0.5
        ok = (lat >= self.lat0) & (lat <= self.lat1) & (lon <= self.lon1) & (S.view_cos(lat, lon, self.lon_sub) >= 0.05)
        if self.extent:
            e = self.extent
            ok &= (lat >= e[0]) & (lat <= e[1]) & ((lon - e[2]) % 360 <= e[3] - e[2])
        return np.clip(fr, 0, h - 1), np.clip(fc, 0, w - 1), ok

    def _prepared(self):
        """The picture with NaN as 0, and where the NaNs were, made once (the HD tiles sample the same grid many times)."""
        if getattr(self, "_prep", None) is None or self._prep[0] is not self.img:
            img = self.img.astype(np.float32)
            self._prep = (self.img, np.nan_to_num(img, nan=0.0), np.isnan(img).astype(np.float32))
        return self._prep[1:]

    def sample(self, lat, lon):
        fr, fc, ok = self._rows_cols(lat, lon)
        filled, holes = self._prepared()
        out = _interp(filled, fr, fc)
        bad = _interp(holes, fr, fc) > 0  # any NaN neighbour
        return np.where(ok & ~bad, out, np.nan).astype(np.float32)


class GridRGB(GridDisc):
    """A colour picture on the same grid; sample() gives uint8 (..., 3), black outside the box or off the disc."""
    def sample(self, lat, lon):
        fr, fc, ok = self._rows_cols(lat, lon)
        chans = [_interp(self.img[..., k].astype(np.float32), fr, fc) for k in range(3)]
        out = np.stack(chans, -1) * ok[..., None]
        return (out + 0.5).clip(0, 255).astype(np.uint8)


def box_for(lat, lon, km):
    """The lat/lon bounds around a storm close-up of km across, with 10% to spare."""
    la, lo = S.grid_latlon(lat, lon, 32, km * 1.1)
    return float(la.min()), float(la.max()), float(lo.min()), float(lo.max())


# ---------- the service ----------
UNREACHABLE_FOR = 45 * 60  # after STRIKES failed requests in a row, the rest of the run (every script) leaves the service alone
STRIKES = 3


def _open(url, timeout):
    return urllib.request.urlopen(url, timeout=timeout).read()


def _get(params):
    """One request, tried three times. Only after STRIKES requests in a row have failed for good (timeout, refused, cut
    off, server error) does the service count as unreachable for the rest of the run, so a slow or broken EUMETView
    costs a few minutes, not the hourly run's whole time limit, and one passing hiccup doesn't cost the rest of the run."""
    folder = os.path.join(S.CACHE, "eumetview")
    mark, strikes = os.path.join(folder, "unreachable"), os.path.join(folder, "strikes")
    if os.path.exists(mark) and time.time() - os.path.getmtime(mark) < UNREACHABLE_FOR:
        raise OSError("EUMETView was unreachable earlier in this run")
    for wait in (5, 20, 0):
        try:
            data = _open(URL + "?" + urllib.parse.urlencode(params), 60)
        except (OSError, http.client.HTTPException) as e:
            err = e
            time.sleep(wait)
            continue
        try:
            os.remove(strikes)
        except FileNotFoundError:
            pass
        return data
    os.makedirs(folder, exist_ok=True)
    try:
        n = int(open(strikes).read()) + 1
    except (OSError, ValueError):
        n = 1
    open(strikes, "w").write(f"{n}\n")
    if n >= STRIKES:
        open(mark, "w").write(f"{err}\n")
    raise OSError(f"EUMETView unreachable: {err}")


def cache_key(layer, when, box, w, h, raw):
    return hashlib.sha1(repr((layer, f"{when:%Y%m%d%H%M}", tuple(float(v) for v in box), w, h, bool(raw))).encode()).hexdigest()


def _piece(layer, when, box, w, h, raw):
    data = _get(wms_params(layer, when, box, w, h, raw))
    if not data.startswith(b"\x89PNG"):  # a ServiceException: no scan at that time, or a layer it can't draw
        raise FileNotFoundError(f"{layer} at {when:%Y-%m-%d %H:%M}: {data[:160]!r}")
    im = Image.open(io.BytesIO(data))
    rgba = np.asarray(im.convert("RGBA"))
    rgb = rgba[..., :3].copy()
    rgb[rgba[..., 3] == 0] = NO_DATA
    return rgb


def image(layer, when, box, w, h, raw=False):
    """One picture of box at w x h pixels, uint8 (h, w, 3), fetched in pieces 4 at a time and cached."""
    path = os.path.join(S.CACHE, "eumetview", cache_key(layer, when, box, w, h, raw) + ".png")
    if os.path.exists(path):
        return np.asarray(Image.open(path).convert("RGB"))
    out = np.zeros((h, w, 3), np.uint8)
    ps = pieces(box, w, h)
    with cf.ThreadPoolExecutor(4) as ex:
        for (sub, pw, ph, r, c), rgb in zip(ps, ex.map(lambda p: _piece(layer, when, p[0], p[1], p[2], raw), ps)):
            out[r:r + ph, c:c + pw] = rgb
    os.makedirs(os.path.dirname(path), exist_ok=True)
    part = f"{path}.{os.getpid()}.part.png"  # two processes writing the same picture never share a temp file
    Image.fromarray(out).save(part); os.replace(part, path)
    return out


def _size(box, deg, lat=0.0):
    """Pixels for box at deg degrees a pixel north-south (and deg / cos(lat) east-west, so pixels stay square)."""
    lat0, lat1, lon0, lon1 = box
    return max(1, round((lon1 - lon0) * max(math.cos(math.radians(lat)), 0.2) / deg)), max(1, round((lat1 - lat0) / deg))


def disc(sat, when, lat, lon, n=1024, km=2048.0):
    """IR brightness temperature around a storm, a little finer than the close-up grid it will be sampled onto."""
    t, box = slot(sat, when), box_for(lat, lon, km)
    w, h = _size(box, km / n * 0.9 / 111.2, lat)
    rgb = image(SATS[sat]["ir"], t, box, w, h, SATS[sat]["raw"])
    return GridDisc(to_bt(sat, rgb, CAL), *box, SATS[sat]["lon0"], EXTENT[SATS[sat]["ir"]])


def world_box(sat):
    """The IR layer's whole extent: Meteosat-12 to 70° each way, Meteosat-9 from 35.5° W to 118.5° E."""
    return EXTENT[SATS[sat]["ir"]]


def world(sat, when, deg):
    """IR brightness temperature over the satellite's whole disc at deg degrees a pixel."""
    box = world_box(sat); w, h = _size(box, deg)
    return GridDisc(to_bt(sat, image(SATS[sat]["ir"], slot(sat, when), box, w, h, SATS[sat]["raw"]), CAL), *box, SATS[sat]["lon0"], box)


def cth_world(sat, when, deg):
    """Cloud-top height (m) over the satellite's disc; the height products come every 15 minutes."""
    box = world_box(sat); w, h = _size(box, deg)
    t = when.replace(second=0, microsecond=0) - timedelta(minutes=when.minute % 15)
    return GridDisc(decode_cth(image(SATS[sat]["cth"], t, box, w, h), CAL), *box, SATS[sat]["lon0"], EXTENT[SATS[sat]["cth"]])


def rgb_disc(layer, sat, when, lat, lon, km=2048.0, px_km=1.0):
    """A colour picture (true or natural colour) around a storm."""
    box = box_for(lat, lon, km); w, h = _size(box, px_km / 111.2, lat)
    return GridRGB(image(layer, slot(sat, when), box, w, h), *box, SATS[sat]["lon0"], EXTENT[layer])



# ---------- calibration against GOES-19 and Himawari-9 where the discs overlap ----------
CAL_BOX = {"meteosat12": (-30.0, 30.0, -55.0, -25.0), "meteosat9": (-30.0, 30.0, 80.0, 110.0)}
CAL_DEG = 0.03


def _cal_grid(box):
    w, h = _size(box, CAL_DEG)
    lat0, lat1, lon0, lon1 = box
    lon = lon0 + (np.arange(w) + 0.5) * (lon1 - lon0) / w; lat = lat1 - (np.arange(h) + 0.5) * (lat1 - lat0) / h
    LON, LAT = np.meshgrid(lon, lat)
    return w, h, LAT, LON


def fit_table(levels, bt, reverse=False):
    """BT for every level 0..255 by quantile matching (the k-th level gets the BT at its own quantile), made monotonic and
    extended linearly past the levels seen. reverse: the level falls as BT rises (Meteosat-9's grey: dark is warm)."""
    n = np.bincount(levels, minlength=256).astype(np.float64)
    seen = np.flatnonzero(n)
    q = (np.cumsum(n) - n / 2) / n.sum()
    vals = np.quantile(bt, 1 - q[seen] if reverse else q[seen])
    k = np.arange(256)
    table = np.interp(k, seen, vals)
    for end in (slice(0, 8), slice(-8, None)):  # straight lines past both ends, from the nearest levels seen
        a, b = np.polyfit(seen[end], vals[end], 1)
        out = (k < seen[0]) if end.start == 0 else (k > seen[-1])
        table[out] = a * k[out] + b
    return (np.minimum.accumulate(table) if reverse else np.maximum.accumulate(table)).astype(np.float64)


def _acha(t):
    """GOES-19 cloud-top height (m) at slot t, sampled by nearest pixel (NaN = clear, no blending into the clear sky)."""
    import netCDF4 as nc
    keys = [k for k in S.listing("noaa-goes19", f"ABI-L2-ACHAF/{t:%Y/%j/%H}/") if "ACHAF-M6_G19" in k and f"_s{t:%Y%j%H%M}" in k]
    if not keys:
        raise FileNotFoundError(f"no GOES-19 ACHA at {t}")
    path = S.download("noaa-goes19", keys[0], os.path.join(S.CACHE, "goes19", f"acha_{t:%Y%m%d%H%M}.nc"))
    d = nc.Dataset(path); p = d["goes_imager_projection"]
    proj = {"a": p.semi_major_axis, "b": p.semi_minor_axis, "h": p.perspective_point_height + p.semi_major_axis,
            "lon0": p.longitude_of_projection_origin, "sweep": p.sweep_angle_axis}
    img, x, y = np.ma.filled(d["HT"][:].astype(np.float32), np.nan), np.asarray(d["x"][:]), np.asarray(d["y"][:])
    d.close()

    def sample(lat, lon):
        sx, sy = S.scan_angles(lat, lon, proj)
        ok = np.isfinite(sx)
        c = np.rint((np.where(ok, sx, x[0]) - x[0]) / (x[1] - x[0])).astype(int); r = np.rint((np.where(ok, sy, y[0]) - y[0]) / (y[1] - y[0])).astype(int)
        ok &= (c >= 0) & (c < len(x)) & (r >= 0) & (r < len(y))
        return np.where(ok, img[np.clip(r, 0, len(y) - 1), np.clip(c, 0, len(x) - 1)], np.nan)
    return sample


def calibrate(date):
    """Fit meteosat_cal.json from the 00, 06, 12 and 18 UTC scans of date (YYYY-MM-DD)."""
    import globe as G
    day = datetime.strptime(date, "%Y-%m-%d")
    slots = [day + timedelta(hours=hh) for hh in (0, 6, 12, 18)]
    out = {"date": date, "ir": {}, "cth": [], "residual": {}}
    ref = {"meteosat12": (-75.2, lambda t: S.goes_disc("goes19", t).sample), "meteosat9": (140.7, lambda t: G.himawari(t).sample)}
    for sat, (lon_ref, fetch) in ref.items():
        box = CAL_BOX[sat]; w, h, LAT, LON = _cal_grid(box)
        both = (S.view_cos(LAT, LON, SATS[sat]["lon0"]) > 0.3) & (S.view_cos(LAT, LON, lon_ref) > 0.3)
        lv, bt = [], []
        for t in slots:
            rgb = image(SATS[sat]["ir"], t, box, w, h, SATS[sat]["raw"])
            ref_bt = fetch(t)(LAT, LON)
            v, has = levels(sat, rgb)
            ok = both & np.isfinite(ref_bt) & has
            lv.append(v[ok]); bt.append(ref_bt[ok])
            print(f"  {sat} {t:%H:%M}: {ok.sum()} pixels", flush=True)
        lv, bt = np.concatenate(lv).astype(np.int64), np.concatenate(bt)
        table = fit_table(lv, bt, reverse=not SATS[sat]["raw"])
        res = float(np.median(np.abs(table[lv] - bt)))
        out["ir"][sat] = [None if (SATS[sat]["raw"] and k == 0) else round(float(v), 2) for k, v in enumerate(table)]
        out["residual"][sat] = round(res, 2)
        print(f"{sat}: median abs residual {res:.2f} K over {len(lv)} pixels (levels {lv.min()}..{lv.max()})")

    # cloud-top height: the colour picture against GOES-19 ACHA over the same box as Meteosat-12's IR
    box = CAL_BOX["meteosat12"]; w, h, LAT, LON = _cal_grid(box)
    both = (S.view_cos(LAT, LON, 0.0) > 0.3) & (S.view_cos(LAT, LON, -75.2) > 0.3)
    keys, hts = [], []
    for t in slots:
        q = image(SATS["meteosat12"]["cth"], t, box, w, h).astype(np.int64) >> 4
        ht = _acha(t)(LAT, LON)
        keys.append(((q[..., 0] * 16 + q[..., 1]) * 16 + q[..., 2])[both]); hts.append(ht[both])
    keys, hts = np.concatenate(keys), np.concatenate(hts)
    dec = np.full(len(keys), np.nan)
    for key in np.unique(keys):
        m = keys == key
        if m.sum() < 50:
            continue
        # white is the picture's no-cloud background (ACHA finds thin low cloud in about half of it, at ~500 m)
        clear = key == 0xFFF or np.isnan(hts[m]).mean() > 0.8
        val = None if clear else round(float(np.nanmedian(hts[m])), 1)
        out["cth"].append([int(key >> 8), int(key >> 4 & 15), int(key & 15), val])
        if val is not None:
            dec[m] = val
    ok = np.isfinite(dec) & np.isfinite(hts)
    res = float(np.median(np.abs(dec[ok] - hts[ok])))
    out["residual"]["cth"] = round(res, 1)
    print(f"cth: {len(out['cth'])} colours, median abs difference {res:.0f} m over {ok.sum()} pixels")
    json.dump(out, open(CAL_PATH, "w"))


if __name__ == "__main__":
    if sys.argv[1:2] == ["calibrate"]:
        calibrate(sys.argv[2])
