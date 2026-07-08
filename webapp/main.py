"""
webapp/main.py
--------------
Deliberately thin. This app does NOT talk to NBM, does NOT generate
polygons, and does NOT know anything about grib2. Its only job is:

    1. Serve the static frontend (index.html/style.css/map.js)
    2. Serve whatever GeoJSON files currently exist in data/ and output/
       as small JSON API endpoints

The actual polygon generation happens in pipeline/ (Track A: proven
working; Track B: real NBM fetch, to be run on a machine with real
internet access -- see project README) and, eventually, on a schedule
via GitHub Actions that writes fresh files into output/. This file
never needs to change when that pipeline logic changes -- it just reads
whatever's on disk at request time.
"""

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

BASE_DIR = Path(__file__).resolve().parent.parent
BOUNDARIES_DIR = BASE_DIR / "data" / "boundaries"
OUTPUT_DIR = BASE_DIR / "output"
DEMO_GEOJSON = BASE_DIR / "data" / "simple" / "demo_ifr_polygons.geojson"
STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(title="G-AIRMET Polygon Viewer")


@app.get("/api/boundaries/states")
def get_state_boundaries():
    path = BOUNDARIES_DIR / "us_states.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail="us_states.json not found in data/boundaries/")
    return FileResponse(path, media_type="application/geo+json")


@app.get("/api/hazards/demo")
def get_demo_hazard():
    """
    Placeholder endpoint. Once Track B (real NBM fetch) and the GitHub
    Actions scheduled job exist, this will be replaced by something like
    GET /api/hazards/{hazard_type}/{valid_time} reading from output/,
    with this demo file kept around purely as a fallback/example.
    """
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
