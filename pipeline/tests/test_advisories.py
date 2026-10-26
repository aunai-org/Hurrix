import json
import pathlib
import sys

FIX = pathlib.Path(__file__).parent / "fixtures"
sys.path.insert(0, str(FIX.parent.parent))

import advisories as A  # noqa: E402


def fake_get(fail=()):
    """Serve the 2026-10-22 feeds by URL suffix; raise OSError for any URL containing a string in `fail`."""
    def get(url):
        if any(f in url for f in fail):
            raise OSError(f"refused {url}")
        name = url.rsplit("/", 1)[-1]
        if name == "CurrentStorms.json":
            return (FIX / "CurrentStorms.json").read_text()
        if name == "jtwc.rss":
            return (FIX / "jtwc.rss").read_text()
        path = FIX / name
        if path.exists():
            return path.read_text()
        raise OSError(f"no fixture for {url}")
    return get


def test_parse_nhc():
    storms = A.parse_nhc(json.loads((FIX / "CurrentStorms.json").read_text()))
    assert [s["id"] for s in storms] == ["AL10", "EP22"]
    jo, va = storms
    assert jo["name"] == "Josephine" and jo["kind"] == "Post-tropical cyclone"
    assert jo["windKt"] == 55 and jo["pressureMb"] == 988
    assert jo["lat"] == 40.0 and jo["lon"] == -65.8
    assert jo["agency"] == "NHC"
    assert va["kind"] == "Tropical storm"


def test_parse_jtwc_rss():
    items = A.parse_jtwc_rss((FIX / "jtwc.rss").read_text())
    assert [k for k, _ in items] == ["Typhoon", "Tropical storm", "Tropical cyclone"]
    assert [u.rsplit("/", 1)[-1] for _, u in items] == ["wp2926.tcw", "ep2226.tcw", "sh0227.tcw"]


def test_parse_tcw_champi():
    s = A.parse_tcw((FIX / "wp2926.tcw").read_text(), "Typhoon")
    assert s["id"] == "WP29" and s["name"] == "Champi" and s["kind"] == "Typhoon"
    assert s["lat"] == 21.9 and s["lon"] == 124.3 and s["windKt"] == 95
    assert s["advisory"] == "2026-10-22T18:00Z" and s["agency"] == "JTWC"


def test_parse_tcw_southern_hemisphere():
    s = A.parse_tcw((FIX / "sh0227.tcw").read_text(), "Tropical cyclone")
    assert s["id"] == "SH02" and s["name"] == "Agueda"
    assert s["lat"] == -6.7 and s["lon"] == 49.7 and s["windKt"] == 50


def test_parse_tcw_west_longitude():
    s = A.parse_tcw((FIX / "ep2226.tcw").read_text(), "Tropical storm")
    assert s["id"] == "EP22" and s["lon"] < 0


def test_active_storms_dedupes_nhc_and_jtwc():
    storms = A.active_storms(fake_get(fail=("/btk/",)))
    ids = [s["id"] for s in storms]
    assert set(ids) == {"WP29", "AL10", "EP22", "SH02"} and len(ids) == 4
    assert ids[0] == "WP29" and ids[-1] == "SH02"
    assert next(s for s in storms if s["id"] == "EP22")["agency"] == "NHC"


def test_active_storms_survives_failed_feed():
    storms = A.active_storms(fake_get(fail=("CurrentStorms.json",)))
    assert sorted(s["id"] for s in storms) == ["EP22", "SH02", "WP29"]
    assert next(s for s in storms if s["id"] == "EP22")["agency"] == "JTWC"


def test_parse_tcw_history_dedupes():
    h = A.parse_tcw((FIX / "wp2926.tcw").read_text(), "Typhoon")["history"]
    assert h[0] == ("2026-10-11T06:00Z", 9.5, 163.8, 15)
    assert h[-1] == ("2026-10-22T18:00Z", 21.9, 124.3, 95)
    assert len({t for t, *_ in h}) == len(h)


def test_parse_tcw_history_southern_hemisphere():
    h = A.parse_tcw((FIX / "sh0227.tcw").read_text(), "Tropical cyclone")["history"]
    assert h[0] == ("2026-10-18T12:00Z", -1.7, 58.7, 15)


def test_parse_bdeck():
    h = A.parse_bdeck((FIX / "bal102026.dat").read_text())
    assert h[0] == ("2026-10-18T18:00Z", 31.0, -71.4, 40)
    assert len({t for t, *_ in h}) == len(h)
    assert h[-1][3] == 55 and all(lon < 0 for _, _, lon, _ in h)


def test_active_storms_history_from_bdeck():
    storms = {s["id"]: s for s in A.active_storms(fake_get())}
    assert storms["AL10"]["history"][-1][3] == 55 and len(storms["AL10"]["history"]) > 5


def test_active_storms_history_survives_bdeck_failure():
    storms = {s["id"]: s for s in A.active_storms(fake_get(fail=("/btk/",)))}
    assert storms["AL10"]["history"] == [("2026-10-22T21:00Z", 40.0, -65.8, 55)]


def test_parse_nhc_skips_one_malformed_storm():
    doc = json.loads((FIX / "CurrentStorms.json").read_text())
    del doc["activeStorms"][0]["latitudeNumeric"]
    assert [s["id"] for s in A.parse_nhc(doc)] == ["EP22"]


def test_parse_jtwc_rss_keeps_each_kind_with_its_own_warning():
    # a warning without a JMV file must not hand its kind to the next storm's file
    xml = ("<item><description>Super Typhoon 30W (Abe) Warning #01 <a href='https://x/wp3026web.txt'>text</a> "
           "Tropical Storm 31W (Bo) Warning #02 <a href='https://x/wp3126.tcw'>JMV</a></description></item>")
    assert A.parse_jtwc_rss(xml) == [("Tropical storm", "https://x/wp3126.tcw")]


def test_active_storms_survives_html_error_pages():
    page = "<html><body><h1>503 Service Unavailable</h1></body></html>"
    assert A.active_storms(lambda url: page) == []
