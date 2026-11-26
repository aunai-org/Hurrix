#!/bin/sh
# refresh-live.sh OUT_DIR  -> fetch the latest satellite slot, rebuild the site, and stage it for publishing.
# Each run stages into a fresh folder OUT_DIR/run-<timestamp>/ and touches no tracked file (the pipeline writes the new
# frame into that folder's data/, and its caches under pipeline/ are git-ignored): site/ (the built page, no _headers,
# with the new data), batches/NN.json (maps of published path -> local path, at most 200 files each, so a host with a per-publish
# file limit can upload them in turns) and files.json (every published path). The last line printed is that folder.
# Used by .github/workflows/refresh.yml, which uploads site/ to Cloudflare Pages every hour.
set -e
cd "$(dirname "$0")/.."
mkdir -p "$1"
STAGE=$(cd "$1" && pwd)/run-$(date -u +%Y%m%d%H%M%S)
mkdir "$STAGE"
python3 -m pip install -q -r pipeline/requirements.txt
[ -d node_modules ] || npm ci --silent
DATA="$STAGE/data" sh pipeline/run.sh && ok=1 || rc=$?
if [ -z "$ok" ]; then
  [ "${rc:-1}" -eq 3 ] || exit "$rc"
  echo "no storms in view; the published site keeps its last frame"
  echo SKIP
  exit 0
fi
npm run build --silent -- --outDir "$STAGE/dist" --emptyOutDir false
python3 - "$STAGE" <<'EOF'
import json, os, shutil, sys
stage = sys.argv[1]; dist = os.path.join(stage, "dist"); site = os.path.join(stage, "site")
# the built page with the saved frame swapped for the new one
shutil.copytree(dist, site, ignore=lambda d, names: ["_headers", "data"] if os.path.samefile(d, dist) else [])
shutil.copytree(os.path.join(stage, "data"), os.path.join(site, "data"))
meta = json.load(open(os.path.join(site, "data", "meta.json")))
os.makedirs(os.path.join(stage, "batches"))
paths = sorted(os.path.relpath(os.path.join(d, f), site) for d, _, fs in os.walk(site) for f in fs if f != "index.html")
# data first, so the page's new code never goes live before the frames it expects
paths.sort(key=lambda p: (not p.startswith("data/"), p))
for i in range(0, len(paths), 200):
    json.dump({p: p for p in paths[i:i + 200]}, open(os.path.join(stage, "batches", f"{i // 200:02d}.json"), "w"), indent=0)
json.dump(paths, open(os.path.join(stage, "files.json"), "w"))
print(f"{len(paths)} files in {(len(paths) + 199) // 200} batch(es); frame {meta['frame']}, {len(meta['storms'])} storm(s)")
print(stage)
EOF
