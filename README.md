# Hurrix

**Tropical cyclones on a 3D globe, from real satellite imagery.**

![Satellite view: the satellite's true-colour picture of a typhoon east of the Philippines, draped on the globe](docs/media/imagery.png)

Hurrix takes the pictures that weather satellites send down every ten minutes and puts them on a globe you can spin. It takes the list of storms the official warning centres are tracking right now (the US National Hurricane Center and the Joint Typhoon Warning Center), names each one on the globe, and lets you fly in and see any of them in 3D.

Nothing on the globe is drawn by hand. Every cloud you see is a pixel the satellite measured. The only modelled parts are the heights in the 3D views, which come from the cloud temperatures (see [how the 3D views work](docs/spec.md#8-the-3d-views)).

For the full story (every data source, the pipeline step by step, how the 3D views are built, and the libraries), read **[How Hurrix works](docs/spec.md)**. It's written for learners and developers, with a glossary for the jargon.

## What you see

Every active storm is listed by its official name in the Storm panel and labelled over its position on the globe. Pick one from the list or click its label to fly to it.

There are three views, picked under **View** in the bar at the bottom of the screen.

| View | What it shows |
| --- | --- |
| **Satellite** | The whole Earth as the satellites see it right now: in True colour, the day side is their real photo, clouds and all. Around the selected storm, a sharper close-up is laid on top: a true-colour photo (1 km per pixel) if it was daytime there, otherwise an infrared picture (2 km per pixel). |
| **Cloud heights** | The storm's close-up with every pixel lifted to the height of its cloud top, so the tall thunderstorms around the eye stand up like mountains. |
| **3D storm** | The same storm as a soft, see-through 3D cloud you can fly around, lit by the sun. |

![Cloud heights: each pixel lifted to its cloud-top height](docs/media/cloud-tops.png)
![3D storm: the cloud built up in 3D from the satellite frame](docs/media/storm.png)

There are also three ways to colour the clouds, under **Colours**:

- **True colour** shows real colour, the way your eyes would see it from space. The whole day side is the satellites' own true-colour picture at the frame time (about 10 km per pixel), sharper around the storm. It only works in daylight, so the night side, the poles and the gaps fall back to infrared in grey over NASA's Blue Marble.
- **Temperature** colours the clouds by temperature, like the weather maps on TV. Warm, low clouds are grey. Below −30 °C they run from blue through green, yellow and red to deep red at −75 °C. The colder the top, the higher and stronger the storm.
- **Soft blue** shows infrared clouds over a deep blue sea, which makes the storm's shape easy to read.

A few more controls:

- **Light:** *Even* uses a fixed light that shows the 3D shapes clearly. *Real sun* puts the sun where it really was at the frame time, so half the globe is in night.
- **Paths:** draws each storm's official past track. Each checkpoint is coloured by the official wind at that time, on the Saffir–Simpson scale (blue for a depression up to red for category 5), with a key in the Legend. It's off at first.
- **HD:** when you zoom in, loads sharper cloud pictures (about 2.4 km per pixel instead of 10 km) for the area you're looking at. It's off at first, and it rests while the replay plays.
- **Replay**, ▶ with **6 h / 12 h / 24 h:** replays the real satellite frames for the last 6 hours (every 10 minutes), 12 hours or 24 hours (every 30 minutes). The clouds move across the whole globe too, from hourly world frames. The clouds slide from one frame to the next along their real motion, measured between the frames, instead of fading. The frames load the first time you play a length. While the replay plays, the true-colour day side gives way to the moving infrared clouds, because it's a single moment.
- **Height ×** (in the two 3D views) stretches the cloud heights.
- **Storm, Legend and About:** three tabs on the right edge. Storm and Legend each open a panel (the storm's details, or what the colours mean) that slides back out of the way by itself. About says what the page shows and credits every data source; it stays open until you close it.

On a phone the same page fits a portrait screen, with the panels as drawers on the right edge:

![On a phone: the storm card and legend are drawers on the right edge](docs/media/phone.png)

## How it works, in short

```
NOAA satellite files ──► pipeline/ (Python) ──► public/data/ (images + JSON) ──► the page (three.js)
```

- **Data:** infrared and visible pictures from five weather satellites: GOES-19, GOES-18 and Himawari-9 from [NOAA's open data on AWS](https://registry.opendata.aws/noaa-goes/), and Meteosat-12 and Meteosat-9 over Europe, Africa and the Indian Ocean from EUMETSAT's public map service, [EUMETView](https://view.eumetsat.int) (© EUMETSAT). No account or key needed. Where no satellite saw daylight, the Earth underneath is NASA's Blue Marble.
- **Storms:** the active storms, their names, official winds and past tracks come from the NHC's [`CurrentStorms.json`](https://www.nhc.noaa.gov/CurrentStorms.json) and best-track files and from the JTWC's [warning feed](https://www.metoc.navy.mil/jtwc/rss/jtwc.rss). A storm both centres warn on is shown once, with the NHC data.
- **Pipeline:** Python scripts stitch the satellites into one infrared world map and one true-colour map of the day side, take the official storms the satellites can see (or, if the feeds are down, find storms by their cold cloud cores), and cut a sharp close-up and a 24-hour loop for each, plus an hourly loop of the whole globe.
- **Page:** a static TypeScript site that draws everything with [three.js](https://threejs.org) and WebGL shaders, built with [Vite](https://vite.dev).
- **3D:** cloud heights come from cloud-top temperatures (the air cools about 6.3 °C per km), so the 3D shapes are modelled from real measurements.

Hurrix isn't an official forecast. Names and winds come from the latest official advisory; it doesn't show forecasts. Meteosat comes as pictures rather than measurements, so its temperatures are calibrated against GOES-19 and Himawari-9 where they overlap and the coldest tops over the Indian Ocean look a little flat. Only the polar caps beyond about 70° still show archive clouds. [Section 11 of the spec](docs/spec.md#11-limits) has the details.

## Run it

You need Node.js 20.19 or newer.

```sh
npm install
npm run dev        # http://localhost:5173
npm run build      # type-check and build the static site into dist/
```

## Refresh the satellite frame

`public/data` holds one saved frame. To replace it with the latest one you need Python 3:

```sh
python3 -m pip install -r pipeline/requirements.txt
npm run data                          # the slot 40 minutes ago
sh pipeline/run.sh 2026-10-20T05:40   # or a given 10-minute slot (UTC)
```

The downloads are large (a GOES full-disk visible file is about 200 MB), so the first run takes a while. Downloaded files are cached in `pipeline/frames`.

Every hour, and on every push to main, a GitHub Actions workflow (`.github/workflows/refresh.yml`) runs `scripts/refresh-live.sh`, which does the same into a fresh folder and builds the site with it, then uploads that site to the Cloudflare Pages project `hurrix` with Wrangler. It needs two repository secrets: `CLOUDFLARE_API_TOKEN` (a token with *Account → Cloudflare Pages → Edit*) and `CLOUDFLARE_ACCOUNT_ID`. Add a custom domain under **Workers & Pages → hurrix → Custom domains**. Run it by hand from the Actions tab with **Run workflow**.

## Project layout

```
src/main.ts        start-up: loads the data, builds the globe, binds the controls
src/meta.ts        the frame and its storms, formatting, the intensity estimate, where the sun is
src/hdr/scene.ts   the three.js scene: HDR pipeline, Earth, clouds, atmosphere, track, imagery patch, 3D views, camera
src/hdr/glsl.ts    the shaders
src/hdr/tiles.ts   which HD tiles surround the view
src/hdr/loop.ts    how the replay moves from one frame to the next
src/ui.ts          buttons, panels, legend, loop player and labels
src/style.css      the page's look
pipeline/          the Python data refresh (see above)
public/data/       the saved frame the page loads
public/textures/   Earth and sky textures (three-globe examples, MIT; NASA Blue and Black Marble)
```

The full reference is [docs/spec.md](docs/spec.md); early design notes are in `docs/superpowers/specs/`. Licences are in `public/third-party-notices.txt` (`npm run notices` regenerates it).
