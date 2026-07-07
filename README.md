# G-AIRMET Polygon Generator

Generates G-AIRMET-style hazard polygons (starting with IFR ceiling/visibility
and Mountain Obscuration) as GeoJSON, derived from NOAA's National Blend of
Models (NBM) probabilistic guidance. Intended eventually to feed both a
web map and (longer-term) an N-AWIPS-compatible workflow.

Reference: NWS Instruction 10-811, *En Route Forecasts and Advisories*
(defines official G-AIRMET criteria this project is trying to approximate).

## Status

🚧 Early scaffolding. Currently working:

- [x] `pipeline/polygons.py` — hazard-agnostic grid → polygon → GeoJSON core
- [x] `tests/test_polygons.py` — unit tests using synthetic data
- [x] Web app (`webapp/`) — FastAPI backend + Leaflet frontend, dark
      aviation-console theme, layer toggles. Currently shows placeholder/demo
      IFR polygons + real US state boundaries. Fully tested end-to-end
      locally (all routes return 200).
- [x] State boundary reference layer (`data/boundaries/us_states.json` —
      real Census-derived data, not placeholder)
- [ ] ARTCC boundary reference layer — toggle exists in the UI but is
      disabled; no data source wired up yet (see "Adding ARTCC boundaries"
      below)
- [ ] Real NBM fetching (Track B — needs a real internet connection to
      `noaa-nbm-grib2-pds` on AWS; not buildable/testable in a sandboxed dev
      environment without egress)
- [ ] Terrain/DEM sourcing for Mountain Obscuration
- [ ] IFR hazard definition (`pipeline/hazards/ifr.py`) — wire the real
      probability-based polygons into `/api/hazards/...` in place of the
      current demo file
- [ ] Mountain Obscuration hazard definition (`pipeline/hazards/mtn_obsc.py`)
- [ ] GitHub Actions scheduled job to regenerate polygons on NBM's cycle

## Running the web app locally

```bash
pip install -r requirements.txt
uvicorn webapp.main:app --reload --port 8000
```

Then open http://localhost:8000 in a real browser (Leaflet needs actual
browser JS + internet access to fetch basemap tiles -- this won't render
in a terminal or a sandboxed dev environment without a display).

## Adding ARTCC boundaries

No public, directly-downloadable GeoJSON source was found during
development (FAA publishes this via an ArcGIS Hub dataset, which requires
either their web UI or an authenticated API call to export -- see
https://hub.arcgis.com/datasets/6b6010f3a7614968a0efc3b2d4d65e26). To add
it:

1. Download the GeoJSON export from that FAA ArcGIS Hub page (or another
   authoritative ARTCC boundary source).
2. Save it as `data/boundaries/artcc.json`.
3. Add a `/api/boundaries/artcc` route to `webapp/main.py` (copy the
   pattern from `/api/boundaries/states`).
4. In `webapp/static/map.js`, add an `artcc` entry to the `layers` object
   and load it in `loadData()`.
5. Remove the `disabled` attribute and `layer-disabled` class from the
   ARTCC checkbox in `index.html`.

## Deploying to Railway

Not done yet -- deliberately. The app works locally, but deploying now
would just put an app showing demo data online. Once real NBM-derived
polygons are flowing (Track B), point a new Railway project at this
GitHub repo; it will auto-detect the `Procfile`.

## Why the code is split this way

`pipeline/polygons.py` deliberately knows NOTHING about NBM, grib2, or
aviation weather — it just converts "a grid of numbers + a threshold" into
polygons. That means:

- It's fully unit-testable with fake data (see `tests/`), no NOAA access
  needed.
- Adding a new hazard later is just "write a new grid + call this module."
- The one-time reprojection/regridding of NBM's native Lambert Conformal
  Conic grid onto a plain lon/lat grid lives elsewhere (`pipeline/regrid.py`,
  not yet written), keeping this module simple.

## Development

```bash
pip install -r requirements.txt
python3 -m pytest tests/ -v
python3 tests/demo_visualize.py   # produces tests/demo_output.png + .geojson
```

## Getting this onto GitHub (from scratch)

1. Create a new **empty** repo on github.com (no README/gitignore — we
   already have our own), e.g. `gairmet-polygons`.
2. From this folder:

   ```bash
   git init
   git add .
   git commit -m "Initial commit: core polygon-generation module + tests"
   git branch -M main
   git remote add origin https://github.com/<your-username>/gairmet-polygons.git
   git push -u origin main
   ```

Railway deployment comes later, once `webapp/` actually has something worth
deploying — no point pointing Railway at an empty app yet.
