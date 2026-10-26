import pathlib
import sys
from datetime import datetime

import numpy as np
import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

import meteosat as M  # noqa: E402


def test_slot_rounds_down_to_each_cadence():
    assert M.slot("meteosat12", datetime(2026, 10, 24, 9, 17)) == datetime(2026, 10, 24, 9, 10)
    assert M.slot("meteosat9", datetime(2026, 10, 24, 9, 14)) == datetime(2026, 10, 24, 9, 0)
    assert M.slot("meteosat9", datetime(2026, 10, 24, 9, 15)) == datetime(2026, 10, 24, 9, 15)


def test_wms_params_lat_lon_order_and_raw_style():
    p = M.wms_params("mtg_fd:ir105_hrfi", datetime(2026, 10, 24, 9, 0), (-30, 30, -55, -25), 1000, 2000, True)
    assert p["bbox"] == "-30,-55,30,-25" and p["crs"] == "EPSG:4326" and p["version"] == "1.3.0"
    assert p["time"] == "2026-10-24T09:00:00.000Z" and p["width"] == "1000" and p["height"] == "2000"
    assert "SLD_BODY" in p and '<ColorMapEntry color="#ffffff" quantity="255"/>' in p["SLD_BODY"]
    assert "SLD_BODY" not in M.wms_params("msg_iodc:ir108", datetime(2026, 10, 24, 9, 0), (-30, 30, 80, 110), 10, 10, False)


def test_pieces_cover_the_request_exactly():
    ps = M.pieces((-70, 70, -75, 75), 6827, 6370)
    assert all(w <= 2048 and h <= 2048 for _, w, h, _, _ in ps)
    assert sum(w * h for _, w, h, _, _ in ps) == 6827 * 6370
    assert {(r, c) for *_, r, c in ps} >= {(0, 0)}


def test_raw_counts_to_bt_with_table():
    cal = {"ir": {"meteosat12": [float("nan")] + [195.0 + 0.43 * k for k in range(1, 256)]}}
    rgb = np.array([[[0, 0, 0], [1, 1, 1], [255, 255, 255]]], np.uint8)
    bt = M.to_bt("meteosat12", rgb, cal)
    assert np.isnan(bt[0, 0]) and bt[0, 1] == pytest.approx(195.43) and bt[0, 2] == pytest.approx(304.65)


def test_grey_to_bt_masks_coloured_pixels():
    cal = {"ir": {"meteosat9": list(np.linspace(299, 203, 256))}}
    rgb = np.array([[[0, 0, 0], [0, 200, 1], [255, 255, 255]]], np.uint8)
    bt = M.to_bt("meteosat9", rgb, cal)
    assert bt[0, 0] == pytest.approx(299) and np.isnan(bt[0, 1]) and bt[0, 2] == pytest.approx(203)


def test_decode_cth_known_unknown_and_clear():
    cal = {"cth": [[15, 0, 3, 12000.0], [0, 0, 0, None]]}
    rgb = np.array([[[255, 1, 63], [1, 1, 1], [128, 128, 128]]], np.uint8)
    h = M.decode_cth(rgb, cal)
    assert h[0, 0] == 12000.0 and np.isnan(h[0, 1]) and np.isnan(h[0, 2])


def test_griddisc_bilinear_inside_nan_outside():
    img = np.array([[0.0, 1.0], [2.0, 3.0]], np.float32)  # row 0 north
    d = M.GridDisc(img, -1, 1, -1, 1, 0.0)
    assert d.sample(np.array(0.0), np.array(0.0)) == pytest.approx(1.5)
    assert np.isnan(d.sample(np.array(5.0), np.array(0.0)))


def test_griddisc_masks_off_disc():
    d = M.GridDisc(np.full((10, 10), 250.0, np.float32), -80, 80, 60, 179, 0.0)
    assert np.isnan(d.sample(np.array(0.0), np.array(100.0)))  # beyond the 0° disc


def test_cache_key_depends_on_every_request_field():
    t = datetime(2026, 10, 24, 9)
    k = M.cache_key("l", t, (0, 1, 2, 3), 10, 20, True)
    others = [M.cache_key("m", t, (0, 1, 2, 3), 10, 20, True), M.cache_key("l", datetime(2026, 10, 24, 9, 10), (0, 1, 2, 3), 10, 20, True),
              M.cache_key("l", t, (0, 1, 2, 4), 10, 20, True), M.cache_key("l", t, (0, 1, 2, 3), 11, 20, True),
              M.cache_key("l", t, (0, 1, 2, 3), 10, 20, False)]
    assert k not in others and len(set(others)) == 5


def test_image_raises_not_found_on_service_exception(monkeypatch, tmp_path):
    monkeypatch.setattr(M.S, "CACHE", str(tmp_path))
    monkeypatch.setattr(M, "_get", lambda params: b'<?xml version="1.0"?><ServiceExceptionReport/>')
    with pytest.raises(FileNotFoundError):
        M.image("mtg_fd:ir105_hrfi", datetime(2026, 10, 24, 9), (0, 1, 0, 1), 4, 4, True)


def test_disc_masks_pixels_beyond_the_layer_extent(monkeypatch):
    # the service paints everything outside a layer's extent white, which Meteosat-9's grey would read as a cold cloud top
    monkeypatch.setattr(M, "CAL", {"ir": {"meteosat9": list(np.linspace(299, 203, 256))}})
    monkeypatch.setattr(M, "image", lambda layer, when, box, w, h, raw=False: np.full((h, w, 3), 255, np.uint8))
    d = M.disc("meteosat9", datetime(2026, 10, 24, 9), -10.0, 117.0, n=64, km=1000.0)
    assert np.isnan(d.sample(np.array(-10.0), np.array(119.5)))
    assert d.sample(np.array(-10.0), np.array(117.0)) == pytest.approx(203)
    assert M.world_box("meteosat12") == (-70.0, 70.0, -70.0, 70.0)


def test_get_gives_up_on_the_service_for_the_rest_of_the_run(monkeypatch, tmp_path):
    import urllib.error
    monkeypatch.setattr(M.S, "CACHE", str(tmp_path))
    monkeypatch.setattr(M.time, "sleep", lambda s: None)
    calls = []

    def unreachable(url, timeout):
        calls.append(url)
        raise urllib.error.URLError("timed out")
    monkeypatch.setattr(M, "_open", unreachable)
    for k in range(M.STRIKES):
        with pytest.raises(OSError, match="EUMETView unreachable"):
            M._get({"a": str(k)})
    n = len(calls)
    with pytest.raises(OSError, match="earlier in this run"):
        M._get({"a": "next"})
    assert n == 3 * M.STRIKES and len(calls) == n  # the next request never went out


def test_get_rides_out_a_passing_server_error(monkeypatch, tmp_path):
    # one 503 in the storm loops used to turn EUMETView off for the whole run, globe frames and day map included
    import urllib.error
    monkeypatch.setattr(M.S, "CACHE", str(tmp_path))
    monkeypatch.setattr(M.time, "sleep", lambda s: None)
    replies = iter([urllib.error.HTTPError("u", 503, "Service Unavailable", {}, None)] * 3 + [b"png"] * 2)

    def flaky(url, timeout):
        r = next(replies)
        if isinstance(r, Exception):
            raise r
        return r
    monkeypatch.setattr(M, "_open", flaky)
    with pytest.raises(OSError):
        M._get({"a": "1"})  # failed for good, but only one strike
    assert M._get({"a": "2"}) == b"png" and M._get({"a": "3"}) == b"png"
    assert not (tmp_path / "eumetview" / "unreachable").exists() and not (tmp_path / "eumetview" / "strikes").exists()


def test_get_turns_a_dropped_reply_into_oserror(monkeypatch, tmp_path):
    import http.client
    monkeypatch.setattr(M.S, "CACHE", str(tmp_path))
    monkeypatch.setattr(M.time, "sleep", lambda s: None)
    monkeypatch.setattr(M, "_open", lambda url, timeout: (_ for _ in ()).throw(http.client.IncompleteRead(b"")))
    with pytest.raises(OSError):
        M._get({"a": "1"})


def test_grey_reads_0_0_1_as_the_warm_end():
    # Meteosat-9's grey draws the warmest pixels (hot land) as (0, 0, 1), not grey
    cal = {"ir": {"meteosat9": list(np.linspace(299, 203, 256))}}
    rgb = np.array([[[0, 0, 1], [200, 0, 0]]], np.uint8)
    bt = M.to_bt("meteosat9", rgb, cal)
    assert bt[0, 0] == pytest.approx(299) and np.isnan(bt[0, 1])


def test_no_data_sentinel_decodes_to_nothing():
    cal = {"ir": {"meteosat9": list(np.linspace(299, 203, 256)), "meteosat12": [None] + [250.0] * 255},
           "cth": [[0, 0, 0, 340.0]]}
    s = np.array([[M.NO_DATA]], np.uint8)
    assert np.isnan(M.decode_cth(s, cal)[0, 0]) and np.isnan(M.to_bt("meteosat9", s, cal)[0, 0])
    assert np.isnan(M.to_bt("meteosat12", s, cal)[0, 0])
