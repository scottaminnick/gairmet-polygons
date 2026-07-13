# G-AIRMET Polygon Generator

Generates G-AIRMET-style hazard polygons (starting with IFR ceiling/visibility
and Mountain Obscuration) as GeoJSON, derived from NOAA's National Blend of
Models (NBM) probabilistic guidance. Intended eventually to feed both a
web map and (longer-term) an N-AWIPS-compatible workflow.

Reference: NWS Instruction 10-811, *En Route Forecasts and Advisories*
(defines official G-AIRMET criteria this project is trying to approximate).

## Status

Currently working:

- [x] `pipeline/polygons.py` — hazard-agnostic grid → polygon → GeoJSON core
- [x] Web app (`webapp/`) — FastAPI backend + Leaflet frontend, dark
      aviation-console theme, layer toggles
- [x] State boundary reference layer (`data/boundaries/us_states.json` —
      real Census-derived data)
- [x] ARTCC boundary reference layer (`data/boundaries/artcc.json` — real
      20-facility CONUS ARTCC boundaries)
- [x] Real NBM fetching (`pipeline/fetch_nbm.py`) — byte-range `.idx`-based
      fetch, deliberately not using `herbie-data` for the actual fetch (see
      that file's docstring for the internal timezone bug we hit and
      couldn't resolve)
- [x] Reprojection (`pipeline/regrid.py`) — NBM's native curvilinear grid
      resampled onto a regular lon/lat grid; also fixes NBM's 0-360
      longitude convention to the -180/180 convention GeoJSON expects
      (found and fixed after seeing real output shifted 360° — see git
      history if curious)
- [x] **IFR hazard definition** (`pipeline/hazards/ifr.py`) — real,
      validated against live NBM data: ceiling<1000ft and visibility<3SM
      probability fields identified from an actual NBM inventory, combined
      via max(), forecaster-adjustable threshold (default 50%)
- [x] **Forecaster-drawn-look post-processing** — raw NBM resolution
      (~2.5km) produces far more detail than a real G-AIRMET forecaster
      draws by hand: lots of tiny separate polygons and jagged edges, not
      the straight-segment, sharp-vertex look of an actual forecaster-drawn
      product. Contour close to native resolution first (preserves real
      sharp features, e.g. a West Coast marine layer's coastal cutoff),
      then merge nearby polygons (`pipeline/polygons.py`, forecaster-
      adjustable radius, default 50nm) — a polygon-level union, not a
      grid-level blur; an earlier grid-blur version inflated isolated
      hazard areas into literal circles (caught by comparing real output
      against an actual G-AIRMET graphic), which polygon-level merging
      avoids since an isolated shape with nothing nearby comes back out
      close to its original form. True geodesic area filtering (default
      matches AIRMET/G-AIRMET's historical 3,000 sq mi "widespread"
      criterion, applied after merging) plus mitre-jointed boundary
      smoothing and a generous simplify pass — sharp corners, not rounded
- [x] **Production driver** (`pipeline/generate_latest_ifr.py`) — finds the
      most recent NBM cycle aligned to G-AIRMET's REAL issuance schedule
      (03/09/15/21Z), then SHIFTS FORWARD one 6-hour G-AIRMET interval to
      produce the UPCOMING cycle from data that already exists, matching
      real forecaster workflow (once 09Z's NBM run posts, you're
      preparing the 15Z product from it, not another 09Z one — the tool
      now advances the same way: after 09Z it serves 15Z, after 15Z it
      serves 21Z). Concretely, requested hour F00 maps to NBM forecast
      hour 6 from the cycle found, F03 to NBM hour 9, and so on — which
      also incidentally eliminates the old F000-doesn't-exist-in-NBM
      problem entirely, since the earliest NBM hour ever requested is
      now 6. The manifest's `model_cycle` reflects the G-AIRMET label
      (e.g. 15Z); a separate `nbm_source_cycle` field (also shown in the
      UI as "NBM SRC") records which underlying NBM run actually fed it
      (e.g. 09Z), for provenance. Also caches each snapshot's
      prepared probability grid (uint8-quantized, ~70x smaller than naive
      float32 storage) so parameters can be re-applied later without
      re-fetching from NBM.
- [x] **Scheduled generation** (`.github/workflows/generate_ifr.yml`) —
      runs every 6 hours, commits `output/ifr_f00.geojson` through
      `ifr_f12.geojson`, their cached `*_grid.npz` grids, and
      `output/ifr_manifest.json` back to the repo, which triggers Railway
      to redeploy with fresh data
- [x] Web app serves real data with a full forecast-hour selector
      (`/api/hazards/ifr/manifest`, `/api/hazards/ifr/{fxx}`) — the map
      viewer's top-left panel lets you switch between F00/F03/F06/F09/F12
      without reloading the page, with graceful fallback to demo data if
      the pipeline hasn't produced output yet
- [x] **Live parameter adjustment** (`/api/hazards/ifr/{fxx}/recompute`) —
      sliders in the map viewer let you adjust threshold/neighborhood-
      radius/min-area and see results in about a second, by re-running
      just the cheap threshold→merge→filter→smooth steps
      (`pipeline.hazards.ifr.polygonize_ifr_grid`) against the cached
      grid — no NBM access, no heavy GRIB2 parsing. This is also why
      Railway's `requirements.txt` grew a few lightweight libraries
      (numpy/scipy/shapely/scikit-image/pyproj/geojson) — still never
      the heavy cfgrib/eccodes/xarray stack, which stays pipeline-only
      (see `requirements-pipeline.txt`).
- [x] **Raster-to-vector polygonization uses scikit-image, not rasterio** —
      rasterio bundles GDAL, which broke Railway deployment
      (`ImportError: libexpat.so.1`) since GDAL dynamically links
      against system libraries not guaranteed to exist on every
      deployment target. A `nixpacks.toml` fix targeting that specific
      library did NOT resolve it. `skimage.measure.find_contours()` has
      zero system dependencies and benchmarked faster besides — see
      `pipeline/polygons.py`'s module docstring for the full story,
      including how hole detection works without rasterio's built-in
      handling for it.
- [ ] Terrain/DEM sourcing for Mountain Obscuration
- [ ] Mountain Obscuration hazard definition (`pipeline/hazards/mtn_obsc.py`)

## Running the web app locally

```bash
pip install -r requirements.txt
uvicorn webapp.main:app --reload --port 8000
```

Then open http://localhost:8000 in a real browser.

## Running the data pipeline locally

```bash
pip install -r requirements-pipeline.txt
python3 pipeline/generate_latest_ifr.py
```

Writes `output/ifr_f00.geojson` through `ifr_f12.geojson` (one per real
G-AIRMET valid-time offset) plus `output/ifr_manifest.json` describing
them. Requires real internet access to NOAA's servers (won't work from a
sandboxed dev environment without egress).

## Forecaster-adjustable parameters

All three can be overridden per-run via the "Run workflow" button on the
Actions tab (`generate_ifr.yml`), without editing any code:

| Parameter | Default | What it controls |
|---|---|---|
| `threshold_pct` | 50% | Probability above which a grid cell counts as hazard |
| `neighborhood_radius_nm` | 50 nm | Real-world radius used to pull nearby smaller hazard areas into larger ones |
| `min_area_sq_mi` | 3,000 sq mi | Minimum true geodesic polygon area (matches AIRMET/G-AIRMET's historical "widespread" criterion) |

## ARTCC boundaries

`data/boundaries/artcc.json` — 20 domestic CONUS ARTCC polygons, each with
a `name` property (e.g. `ZTL` for Atlanta Center). Sourced from the
project owner's own `model-viewer` repo rather than FAA's ArcGIS Hub
(which requires their web UI or an authenticated API export, neither
reachable from a sandboxed dev environment during initial development).

## Deploying to Railway

Point a Railway project at this GitHub repo; it auto-detects the
`Procfile` and `requirements.txt` (the lightweight web-app-only one —
Railway never installs the heavy pipeline dependencies, see
`requirements-pipeline.txt` vs `requirements.txt` below). The scheduled
GitHub Action commits fresh forecast-hour snapshots every 6 hours,
which triggers Railway to redeploy with updated data automatically.

## Why there are two requirements files

- `requirements.txt` — `fastapi` + `uvicorn` plus a handful of
  lightweight geospatial libraries (`numpy`, `scipy`, `shapely`,
  `scikit-image`, `pyproj`, `geojson`) needed for live parameter
  adjustment (re-processing an already-cached grid — see
  `polygonize_ifr_grid`). What Railway installs to run the web app.
  Still deliberately excludes the heavy GRIB2 stack below — the web app
  never fetches from NBM directly, it only reads/reprocesses what's
  already on disk.
- `requirements-pipeline.txt` — the heavy stuff (`cfgrib`, `eccodes`,
  `xarray`, `herbie-data`, `requests`, etc.) needed to actually fetch and
  parse NBM grib2 data from NOAA. Only installed by GitHub Actions
  workflows, never by Railway.

## Why the code is split this way

`pipeline/polygons.py` deliberately knows NOTHING about NBM, grib2, or
aviation weather — it just converts "a grid of numbers + a threshold" into
polygons. That means:

- It's fully unit-testable with fake data (see `tests/`), no NOAA access
  needed.
- Adding a new hazard later is just "write a new grid + call this module."
- The one-time reprojection/regridding of NBM's native Lambert Conformal
  Conic grid onto a plain lon/lat grid lives elsewhere (`pipeline/regrid.py`),
  keeping this module simple.

## Development

```bash
pip install -r requirements-pipeline.txt
python3 -m pytest tests/ -v
python3 tests/demo_visualize.py   # produces docs/images/polygon_extraction_demo.png + data/sample/demo_ifr_polygons.geojson
```

## Getting this onto GitHub (historical -- already done for this repo)

For reference, if you're ever starting a similar project from scratch:

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
