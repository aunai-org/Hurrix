#!/bin/sh
# run.sh [YYYY-MM-DDTHH:MM]  -> refresh public/data from one 10-minute satellite slot (default: the slot 40 minutes ago,
# which NOAA has normally finished publishing). Needs Python 3 with the packages in requirements.txt.
# DATA=/some/folder writes the frame there instead of public/data (the hourly refresh uses a fresh folder per run).
set -e
cd "$(dirname "$0")"
D=${DATA:-../public/data}
F=${1:-$(date -u -d "@$(( $(date -u +%s) / 600 * 600 - 2400 ))" +%Y-%m-%dT%H:%M)}
# archival clouds for the part of the globe no geostationary satellite here covers (three-globe's example texture, MIT)
if [ ! -f cache/clouds.png ]; then
  mkdir -p cache
  curl -sS https://registry.npmjs.org/three-globe/-/three-globe-2.45.6.tgz | tar xz -C cache --strip-components=3 package/example/clouds/clouds.png
fi
# Himawari skips the 02:40 and 14:40 slots every day, so take the slot before
case $F in *T02:40|*T14:40) F=$(date -u -d "$F 10 minutes ago" +%Y-%m-%dT%H:%M);; esac
echo "frame $F"
./fetch.sh "$F"
FRAME=$F python3 -W ignore reproject.py 4096
python3 -W ignore export.py "$D"
FRAME=$F python3 -W ignore storms.py "$D"
FRAME=$F python3 -W ignore visible.py "$D"
FRAME=$F python3 -W ignore globe.py "$D"
# the HD tiles, the replay motion and the live day side are extras: if one fails, the page simply goes without it
FRAME=$F python3 -W ignore daymap.py "$D" || echo "day map: skipped"
python3 -W ignore tiles.py "$D" || echo "tiles: skipped"
python3 -W ignore flow.py "$D" || echo "flow: skipped"
