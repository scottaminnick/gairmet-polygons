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
generates a full set of forecast-hour snapshots (matching G-AIRMET's
real 00/03/06/09/12h valid-time schedule) plus a manifest describing
them, and commits them back to the repo -- this file never needs to
change when that pipeline logic changes, it just reads whatever's on
disk at request time. Deliberately NOT in requirements.txt: the
pipeline's heavy GRIB2/geospatial dependencies (see
requirements-pipeline.txt) -- this app never touches NBM data directly.
"""

import json
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


@app.get("/api/hazards/ifr/manifest")
def get_ifr_manifest():
    """
    Describes what forecast-hour snapshots are currently available
    (see pipeline/generate_latest_ifr.py) -- the frontend fetches this
    first to build its forecast-hour selector. Registered BEFORE the
    /api/hazards/ifr/{fxx} route below: FastAPI matches path templates
    in registration order, and a generic {fxx} string parameter would
    otherwise happily (and wrongly) match the literal word "manifest".
    """
    manifest_path = OUTPUT_DIR / "ifr_manifest.json"
    if not manifest_path.exists():
        raise HTTPException(status_code=404, detail="No manifest available yet (pipeline hasn't run)")
    return FileResponse(manifest_path, media_type="application/json")


@app.get("/api/hazards/ifr/{fxx}")
def get_ifr_snapshot(fxx: str):
    """
    A specific forecast-hour snapshot, e.g. /api/hazards/ifr/06 for the
    6-hour snapshot. fxx is the REQUESTED forecast hour as it appears
    in the filename (ifr_f06.geojson) -- this may differ from the hour
    actually used internally for F00 specifically, since NBM has no
    true 0-hour file (see the manifest's "substituted" field).
    """
    path = OUTPUT_DIR / f"ifr_f{fxx}.geojson"
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"No snapshot for F{fxx}")
    return FileResponse(path, media_type="application/geo+json")


@app.get("/api/hazards/ifr")
def get_ifr_hazard():
    """
    Backward-compatible default endpoint: serves the FIRST available
    snapshot from the manifest (the shortest forecast hour). Falls back
    to the demo GeoJSON if the pipeline hasn't produced output yet
    (e.g. right after first deploy) -- so the map always shows
    SOMETHING rather than a blank error, while still preferring real
    data whenever it exists.
    """
    manifest_path = OUTPUT_DIR / "ifr_manifest.json"
    if manifest_path.exists():
        with open(manifest_path) as f:
            manifest = json.load(f)
        snapshots = manifest.get("snapshots") or []
        if snapshots:
            default_path = OUTPUT_DIR / snapshots[0]["filename"]
            if default_path.exists():
                return FileResponse(default_path, media_type="application/geo+json")
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
