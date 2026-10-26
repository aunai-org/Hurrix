import pathlib
import sys
from datetime import datetime

import numpy as np
import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

import daymap as D  # noqa: E402
import meteosat as M  # noqa: E402


def test_block_mean_ignores_missing_pixels():
    img = np.array([[1, 3, np.nan, np.nan], [np.nan, 2, np.nan, np.nan]], np.float32)
    out = D.block_mean(img, 2)
    assert out[0, 0] == pytest.approx(2.0) and np.isnan(out[0, 1])


def test_daylight_fades_in_with_the_sun():
    mu = np.array([0.0, 0.03, 0.09, 0.15, 0.6])
    d = D.daylight(mu)
    assert d[0] == 0 and d[1] == 0 and 0 < d[2] < 1 and d[3] == pytest.approx(1) and d[4] == 1


def test_meteosat_fades_out_sooner_in_the_morning_than_in_the_evening():
    mu = np.array([0.25, 0.25])
    d = D.daylight(mu, "meteosat12", np.array([True, False]))
    assert d[0] == 0 and d[1] == pytest.approx(1)


def test_morning_is_west_of_the_subsolar_point():
    noon_lon = D.V.subsolar(datetime(2026, 10, 21, 15, 30))[1]  # about 50° W
    am = D.morning(np.array([noon_lon - 30, noon_lon + 30, noon_lon - 30 + 360]), datetime(2026, 10, 21, 15, 30))
    assert am.tolist() == [True, False, True]


def test_edge_fades_inside_the_box_and_is_zero_outside():
    box = (-70.0, 70.0, -70.0, 70.0)
    e = D.edge(np.array([0.0, 0.0, 0.0, -68.0]), np.array([0.0, 66.0, 100.0, 0.0]), box)
    assert e[0] == 1 and 0 < e[1] < 1 and e[2] == 0 and 0 < e[3] < 1


def test_match_moves_colours_onto_the_reference_and_keeps_white():
    rng = np.random.default_rng(1)
    src = rng.uniform(0, 0.8, (200, 200, 3)).astype(np.float32)
    ref = src * 1.2
    luts = D.match(src, ref, np.ones(src.shape[:2], bool))
    out = D.apply(luts, src)
    assert np.median(np.abs(out - ref)) < 0.02
    assert all(t[-1] == pytest.approx(1.0) for t in luts)


def test_match_gives_up_with_too_little_overlap():
    src = np.zeros((10, 10, 3), np.float32)
    assert D.match(src, src, np.ones((10, 10), bool)) is None


def test_apply_keeps_missing_pixels_missing():
    rgb = np.full((1, 2, 3), 0.5, np.float32); rgb[0, 1] = np.nan
    out = D.apply([np.linspace(0, 1, 256)] * 3, rgb)
    assert out[0, 0, 0] == pytest.approx(0.5, abs=0.01) and np.isnan(out[0, 1]).all()


def test_stitch_hands_over_where_a_source_fades_out():
    a = np.full((1, 3, 3), 0.2, np.float32); b = np.full((1, 3, 3), 0.8, np.float32)
    cos = np.full((1, 3), 0.5, np.float32)
    qa = np.array([[1.0, 0.0, 0.5]], np.float32); qb = np.array([[0.0, 1.0, 0.5]], np.float32)
    rgb, q = D.stitch([a, b], [cos, cos], [qa, qb])
    assert rgb[0, 0, 0] == pytest.approx(0.2) and rgb[0, 1, 0] == pytest.approx(0.8)
    assert rgb[0, 2, 0] == pytest.approx(0.5) and q.tolist()[0] == pytest.approx([1.0, 1.0, 0.5])


def test_stitch_with_one_source():
    a = np.full((1, 1, 3), 0.3, np.float32)
    rgb, q = D.stitch([a], [np.ones((1, 1), np.float32)], [np.full((1, 1), 0.7, np.float32)])
    assert rgb[0, 0, 0] == pytest.approx(0.3) and q[0, 0] == pytest.approx(0.7)


def test_colour_layers_ask_for_a_transparent_background():
    t = datetime(2026, 10, 26, 3, 0)
    assert M.wms_params("mtg_fd:rgb_truecolour", t, (-70, 70, -70, 70), 10, 10, False)["transparent"] == "TRUE"
    assert "transparent" not in M.wms_params("mtg_fd:ir105_hrfi", t, (-70, 70, -70, 70), 10, 10, True)


def test_saved_colour_match_is_three_monotonic_tables():
    luts = M.CAL["truecolour"]
    assert len(luts) == 3 and all(len(t) == 256 and np.all(np.diff(t) >= 0) for t in luts)
