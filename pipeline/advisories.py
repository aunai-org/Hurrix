"""The storms the warning centres are tracking right now, with their official names, positions and winds.

NHC (CurrentStorms.json) covers the Atlantic and the East and Central Pacific; JTWC (its RSS feed, and the JMV 3.0
.tcw file each warning links to) covers the West Pacific, the Indian Ocean and the Southern Hemisphere, and repeats the
East Pacific. A storm both warn on is kept once, from NHC. A feed that fails is skipped, so the hourly run never stops
on it.

Usage: python3 advisories.py      (prints the active storms)
"""
import json
import re
import sys
import urllib.request

NHC = "https://www.nhc.noaa.gov/CurrentStorms.json"
BDECK = "https://ftp.nhc.noaa.gov/atcf/btk/b{id}.dat"  # NHC best track so far, e.g. bal102026.dat
JTWC = "https://www.metoc.navy.mil/jtwc/rss/jtwc.rss"

NHC_KIND = {"TD": "Tropical depression", "STD": "Subtropical depression", "TS": "Tropical storm",
            "STS": "Subtropical storm", "HU": "Hurricane", "PTC": "Post-tropical cyclone", "PC": "Potential tropical cyclone"}
# JTWC basin letter -> ATCF basin
JTWC_BASIN = {"W": "WP", "E": "EP", "C": "CP", "L": "AL", "A": "IO", "B": "IO", "S": "SH", "P": "SH"}


def http_get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "hurrix (github.com/aunai-org/Hurrix)"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read().decode("utf-8", "replace")


def _num(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def parse_nhc(doc):
    out = []
    for s in doc.get("activeStorms", []):
        try:
            out.append(_nhc_storm(s))
        except (KeyError, TypeError, ValueError, AttributeError) as e:  # one bad entry costs only that storm
            print(f"advisories: NHC storm {s.get('id') if isinstance(s, dict) else s!r} skipped ({e!r})")
    return out


def _nhc_storm(s):
    sid = s["id"]  # "al102026"
    wind = _num(s.get("intensity"))
    kind = NHC_KIND.get(s.get("classification"), "Tropical cyclone")
    if kind == "Hurricane" and wind is not None and wind >= 96:
        kind = "Major hurricane"
    return {"atcf": sid, "id": sid[:2].upper() + sid[2:4], "name": s["name"].title(), "kind": kind, "windKt": wind,
            "pressureMb": _num(s.get("pressure")), "lat": float(s["latitudeNumeric"]),
            "lon": float(s["longitudeNumeric"]), "agency": "NHC",
            "advisory": s["lastUpdate"][:16] + "Z"}


def parse_jtwc_rss(xml):
    """(kind, tcw url) for each storm JTWC is warning on. Items without a warning (outlooks) are skipped, and so is a
    warning without a JMV file: each warning takes only the file listed after it, before the next warning."""
    out = []
    for item in re.findall(r"<item>(.*?)</item>", xml, re.S):
        warnings = list(re.finditer(r"([A-Za-z][A-Za-z ]*?)\s+\d\d[A-Z]\s+\([^)]+\)\s+Warning", item))
        for m, nxt in zip(warnings, warnings[1:] + [None]):
            tcw = re.search(r"https://[^'\"\s]+\.tcw", item[m.end():nxt.start() if nxt else len(item)])
            if tcw:
                out.append((" ".join(m.group(1).split()).capitalize(), tcw.group(0)))
    return out


def _ll(lat, lon):
    """"219N", "1243E" (tenths of a degree) -> 21.9, 124.3, with south and west negative and longitude in -180..180."""
    la = int(lat[:-1]) / 10 * (-1 if lat[-1] == "S" else 1)
    lo = int(lon[:-1]) / 10 * (-1 if lon[-1] == "W" else 1)
    return la, round((lo + 180) % 360 - 180, 1)


def _iso(t):
    """"2026102218" -> "2026-10-22T18:00Z"."""
    return f"{t[:4]}-{t[4:6]}-{t[6:8]}T{t[8:10]}:00Z"


def _once(rows):
    """One entry per time (the files repeat each time once per wind-radius row), oldest first."""
    return sorted({r[0]: r for r in rows}.values())


def parse_bdeck(text):
    """NHC best track: (time, lat, lon, wind kt) every 6 h from the BEST rows."""
    rows = []
    for line in text.splitlines():
        f = [x.strip() for x in line.split(",")]
        if len(f) > 8 and f[4] == "BEST":
            rows.append((_iso(f[2]), *_ll(f[6], f[7]), _num(f[8])))
    return _once(rows)


HISTORY = re.compile(r"^\d{2}(\d{8}) +(\d{1,3}[NS]) *(\d{1,4}[EW]) +(\d+)\s*$")


def parse_tcw(text, kind):
    lines = text.splitlines()
    head = lines[2].split()  # "2026102218 29W CHAMPI 034 ..."
    t = head[0]
    num, letter = head[1][:-1], head[1][-1]
    t0 = next(l for l in lines if l.startswith("T000")).split()
    lat, lon = _ll(t0[1], t0[2])
    # the warning's past track: storm number, YYMMDDHH, position, wind
    history = _once((_iso("20" + m[1]), *_ll(m[2], m[3]), int(m[4])) for m in map(HISTORY.match, lines) if m)
    return {"history": history or [(_iso(t), lat, lon, _num(t0[3]))],"id": JTWC_BASIN.get(letter, "XX") + num, "agencyId": head[1], "name": head[2].title(), "kind": kind,
            "windKt": _num(t0[3]), "pressureMb": None, "lat": lat, "lon": lon, "agency": "JTWC",
            "advisory": _iso(t)}


def _feed(name, fn):
    try:
        return fn()
    except Exception as e:  # a feed that's down, slow or changed never stops the run
        print(f"advisories: {name} failed: {e}")
        return []


def active_storms(get=http_get):
    """Every storm with a current warning, once each, strongest first (no official wind last)."""
    nhc = _feed(NHC, lambda: parse_nhc(json.loads(get(NHC))))
    for s in nhc:
        url = BDECK.format(id=s.pop("atcf"))
        s["history"] = _feed(url, lambda: parse_bdeck(get(url))) or [(s["advisory"], s["lat"], s["lon"], s["windKt"])]
    jtwc = []
    for kind, url in _feed(JTWC, lambda: parse_jtwc_rss(get(JTWC))):
        jtwc += _feed(url, lambda: [parse_tcw(get(url), kind)])
    for s in nhc + jtwc:  # end the path at the advisory position when the track file lags behind it
        if s["history"][-1][0] < s["advisory"]:
            s["history"].append((s["advisory"], s["lat"], s["lon"], s["windKt"]))
    seen = {s["id"] for s in nhc}
    storms = nhc + [s for s in jtwc if s["id"] not in seen]
    return sorted(storms, key=lambda s: -(s["windKt"] if s["windKt"] is not None else -1))


if __name__ == "__main__":
    for s in active_storms():
        print(json.dumps(s))
    sys.exit(0)
