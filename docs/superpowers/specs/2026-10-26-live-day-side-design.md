# The live day side — design

**Status:** built · 2026-10-26 · asked for by omr (item 3 of the 2026-10-26 list: "live day-side true-colour world map")

## 1. What we're building, and why

In True colour, everything on the globe outside the selected storm's close-up was NASA's Blue Marble (a cloudless
composite from 2004) with the infrared clouds drawn over it in grey. Hurrix's rule is real satellite pictures, not
drawn ones, so the day side should be the satellites' own photo of the Earth at the frame time.

**Done means:**
- In True colour, the whole sunlit globe that a geostationary satellite sees is a true-colour photo from the frame
  time, clouds and all, with no visible seams between satellites.
- Night, the polar caps and the gaps fall back to what was there before (Blue Marble under the infrared clouds).
- The infrared styles (Temperature, Soft blue) are unchanged.
- If the day map can't be made, the run still finishes and the page looks as it did.

## 2. Approach

Three ways were weighed:

| | Approach | Verdict |
|---|---|---|
| A | **One stitched true-colour map per frame** in the pipeline (`day_global.jpg` + a mask), laid on the Earth in the shader | **Chosen.** One 1.2 MB image, the same grid and stitching as the infrared map, simple shader change |
| B | True-colour HD tiles like the infrared ones | Later, if wanted: 16x the pixels and downloads for zoomed-in views only |
| C | Hourly true-colour globe frames so the day side moves in the replay | Later: 24x the visible downloads every hour |

## 3. Pipeline: `pipeline/daymap.py`

- **Sources.** GOES-19/18 bands 1-3 from the 2 km multi-band file (`ABI-L2-MCMIPF`, 350 MB, cheaper than the 540 MB of
  the three single-band files); Himawari-9 bands 1-3 from only the tiles in daylight; Meteosat-12's rendered true
  colour (`mtg_fd:rgb_truecolour`) from EUMETView, asked for with `transparent=TRUE` because it otherwise paints the
  night side white. Meteosat-9 only has natural colour (false colour), so it isn't used.
- **Colour.** GOES and Himawari use the close-ups' recipe, factored out of `visible.py` as `tone()` and
  `goes_green()`. Meteosat-12 is matched to GOES-19 per channel by quantiles where both see well in good daylight;
  when they share too little, a saved match in `meteosat_cal.json` (`python3 daymap.py calibrate DATE`) is used.
- **Quality and stitching.** Each source's quality = view-angle term × daylight term (× an 8° fade inside
  Meteosat-12's 70° box). Sources rank by quality, then view; the best is blended with the runner-up near seams. The
  blended quality is the mask. Meteosat-12's picture goes dark at dusk and hazy white at dawn, so its daylight term
  starts later (5-13° sun in the evening, 17-33° in the morning) than GOES and Himawari (2-9°).
- **Output.** `day_global.jpg` (4096 x 2048, q90, about 1.2 MB), `day_mask.png` (2048 x 1024, about 40 KB),
  `meta.json` `day.satellites`. Run from `run.sh` after `visible.py`, as an optional step like the tiles.

## 4. Page

- `EARTH`: `albedo = mix(blueMarble, dayTC, live)`, where `live = dayOn × (style == True colour) × mask`; the infrared
  cloud shadow is scaled by `1 - live` (the photo has its own shadows).
- `CLOUDS`: the infrared cloud shell's opacity is scaled by `1 - live`, so clouds aren't drawn twice.
- `dayOn` eases to 0 while the replay plays (the day map is one moment; the infrared clouds move) and back after.
- Without `meta.day` the mask is a black pixel and nothing changes. True colour becomes the starting style whenever
  there is a day map.

## 5. Testing

- `pipeline/tests/test_daymap.py`: block averaging with gaps, daylight and morning terms, the box fade, the colour
  match (including the white end and too little overlap), stitch hand-over, one-source stitch, transparent colour
  requests, the saved match's shape.
- Real runs at 2026-10-21 15:30 UTC (the saved frame: Americas, Atlantic, Africa in daylight) and 2026-10-26 03:00 UTC
  (Asia, Australia, Pacific), checked as flat maps and in the page with Even light, Real sun and zoomed in.

## 6. Limits

About 10 km per pixel; static during the replay; Blue Marble near sunrise and sunset, beyond about 70°, and where only
a disc's far edge sees; Meteosat-12's area fades out earlier, by morning especially.
