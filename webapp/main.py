"""
webapp/main.py
--------------
Deliberately thin. This app does NOT talk to NBM, does NOT generate
polygons, and does NOT know anything about grib2. Its only job is:

    1. Serve the static frontend (index.html/style.css/map.js)
    2. Serve whatever GeoJSON files currently exist in data/ and output/
       as small JSON API endpoints

The actual polygon generation happens in pipeline/ and runs on a
schedule via GitHub Actions (.github/workflows/generate_ifr.yml), which
commits fresh output/ifr_latest.geojson back to the repo -- this file
never needs to change when that pipeline logic changes, it just reads
whatever's on disk at request time. Deliberately NOT in requirements.txt:
the pipeline's heavy GRIB2/geospatial dependencies (see
requirements-pipeline.txt) -- this app never touches NBM data directly.
"""

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

BASE_DIR = Path(__file__).resolve().parent.parent
BOUNDARIES_DIR = BASE_DIR / "data" / "boundaries"
OUTPUT_DIR = BASE_DIR / "output"
DEMO_GEOJSON = BASE_DIR / "data" / "sample" / "demo_ifr_polygons.geojson"
STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(title="G-AIRMET Polygon Viewer")


@app.get("/api/boundaries/states")
def get_state_boundaries():
    path = BOUNDARIES_DIR / "us_states.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail="us_states.json not found in data/boundaries/")
    return FileResponse(path, media_type="application/geo+json")


@app.get("/api/boundaries/artcc")
def get_artcc_boundaries():
    path = BOUNDARIES_DIR / "artcc.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail="artcc.json not found in data/boundaries/")
    return FileResponse(path, media_type="application/geo+json")


@app.get("/api/hazards/ifr")
def get_ifr_hazard():
    """
    Real IFR hazard polygons, regenerated on a schedule by
    .github/workflows/generate_ifr.yml (see pipeline/generate_latest_ifr.py).

    Falls back to the demo GeoJSON if the pipeline hasn't produced
    output yet (e.g. right after first deploy, before the scheduled
    workflow has run once) -- so the map always shows SOMETHING rather
    than a blank error, while still preferring real data whenever it
    exists.
    """
    live_path = OUTPUT_DIR / "ifr_latest.geojson"
    if live_path.exists():
        return FileResponse(live_path, media_type="application/geo+json")
    if DEMO_GEOJSON.exists():
        return FileResponse(DEMO_GEOJSON, media_type="application/geo+json")
    raise HTTPException(
        status_code=404,
        detail="No IFR data available yet (pipeline hasn't run, and no demo fallback found)",
    )


@app.get("/api/hazards/demo")
def get_demo_hazard():
    """Kept as a stable reference/example endpoint -- always the original synthetic demo data, never real output."""
    if not DEMO_GEOJSON.exists():
        raise HTTPException(
            status_code=404,
            detail="Demo GeoJSON not found -- run tests/demo_visualize.py to generate it",
        )
    return FileResponse(DEMO_GEOJSON, media_type="application/geo+json")


@app.get("/api/health")
def health():
    return {"status": "ok"}


# Mounted LAST and at the root path, so the explicit routes above always
# take priority over serving static files for the same path.
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
