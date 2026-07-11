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
      draws by hand, so three steps close that gap: neighborhood-maximum
      smoothing (`pipeline/smoothing.py`, forecaster-adjustable radius,
      default 50nm) pulls nearby smaller areas into larger ones; Gaussian
      smoothing rounds off small-scale grid noise before contouring; true
      geodesic area filtering (`pipeline/polygons.py`, forecaster-
      adjustable, default matches AIRMET/G-AIRMET's historical 3,000 sq mi
      "widespread" criterion) plus boundary buffer-smoothing rounds off
      remaining jagged edges
- [x] **Production driver** (`pipeline/generate_latest_ifr.py`) — finds the
      most recently available NBM cycle and generates real polygons
- [x] **Scheduled generation** (`.github/workflows/generate_ifr.yml`) —
      runs every 6 hours, commits `output/ifr_latest.geojson` back to the
      repo, which triggers Railway to redeploy with fresh data
- [x] Web app serves real data at `/api/hazards/ifr`, with graceful
      fallback to demo data if the pipeline hasn't produced output yet
- [ ] Terrain/DEM sourcing for Mountain Obscuration
- [ ] Mountain Obscuration hazard definition (`pipeline/hazards/mtn_obsc.py`)
- [ ] Full 0/3/6/9/12-hour G-AIRMET-style forecast set (currently single
      near-term snapshot, F003, only)

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

Writes `output/ifr_latest.geojson`. Requires real internet access to
NOAA's servers (won't work from a sandboxed dev environment without
egress).

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
GitHub Action commits fresh `output/ifr_latest.geojson` every 6 hours,
which triggers Railway to redeploy with updated data automatically.

## Why there are two requirements files

- `requirements.txt` — just `fastapi` + `uvicorn`. What Railway installs
  to run the web app. The web app never touches NBM data directly, it
  only reads whatever GeoJSON is already on disk.
- `requirements-pipeline.txt` — the heavy stuff (`cfgrib`, `eccodes`,
  `xarray`, `pyproj`, `scipy`, etc.) needed to actually fetch and process
  NBM grib2 data. Only installed by GitHub Actions workflows, never by
  Railway.

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
