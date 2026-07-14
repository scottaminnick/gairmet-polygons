"""
webapp/main.py
--------------
Deliberately thin, with ONE deliberate exception. This app does NOT
fetch from NBM, does NOT know anything about grib2, and never installs
the heavy GRIB2 stack (cfgrib/eccodes/xarray/herbie-data -- see
requirements-pipeline.txt vs requirements.txt). Its jobs are:

    1. Serve the static frontend (index.html/style.css/map.js)
    2. Serve whatever GeoJSON files currently exist in data/ and output/
       as small JSON API endpoints
    3. Re-process an ALREADY-FETCHED, cached probability grid with
       forecaster-chosen parameters (threshold/neighborhood-radius/
       min-area) for live interactive adjustment -- see
       recompute_ifr_snapshot() below. This is the one place this app
       does real computation rather than just serving files, but it's
       cheap (numpy/shapely/scipy math against a small cached grid, no
       network access, no NBM), which is why it's safe to do inside a
       request handler.

The actual polygon generation happens in pipeline/ and runs on a
schedule via GitHub Actions (.github/workflows/generate_ifr.yml), which
generates a full set of forecast-hour snapshots (matching G-AIRMET's
real 00/03/06/09/12h valid-time schedule) plus a manifest describing
them, and commits them back to the repo -- this file never needs to
change when that pipeline logic changes, it just reads whatever's on
disk at request time.
"""

import json
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
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


def _load_manifest_and_snapshot(fxx: str):
    """Shared lookup used by both the recompute endpoint and (indirectly) get_ifr_snapshot."""
    manifest_path = OUTPUT_DIR / "ifr_manifest.json"
    if not manifest_path.exists():
        raise HTTPException(status_code=404, detail="No manifest available yet (pipeline hasn't run)")
    with open(manifest_path) as f:
        manifest = json.load(f)
    snapshot = next(
        (s for s in manifest.get("snapshots", []) if str(s["requested_forecast_hour"]).zfill(2) == fxx.zfill(2)),
        None,
    )
    if snapshot is None:
        raise HTTPException(status_code=404, detail=f"No snapshot for F{fxx}")
    return manifest, snapshot


@app.get("/api/hazards/ifr/{fxx}/recompute")
def recompute_ifr_snapshot(
    fxx: str,
    threshold_pct: float = 50.0,
    neighborhood_radius_nm: float = 50.0,
    min_area_sq_mi: float = 3000.0,
    format: str = "geojson",
):
    """
    Live parameter adjustment: re-runs ONLY the cheap, NBM-independent
    part of the pipeline (threshold -> merge -> area filter -> boundary
    smoothing, see pipeline.hazards.ifr.polygonize_ifr_grid) against an
    ALREADY-FETCHED, cached probability grid for this forecast hour.
    No network access, no NBM, no heavy GRIB2 parsing -- just numpy/
    shapely/scipy math against a small cached array, which is what
    makes this fast enough to call live from the browser as someone
    drags a slider.

    Query params: threshold_pct, neighborhood_radius_nm, min_area_sq_mi
    (all optional, matching the same forecaster-adjustable parameters
    used by the scheduled pipeline); format=xml returns a simple XML
    representation instead of GeoJSON (see pipeline/export_xml.py) --
    e.g. for handing a live-adjusted draft off to N-AWIPS conversion
    tooling, not just the as-scheduled version from /{fxx}?format=xml.
    """
    manifest, snapshot = _load_manifest_and_snapshot(fxx)

    cache_filename = snapshot.get("cache_filename")
    if not cache_filename:
        raise HTTPException(
            status_code=404,
            detail=f"No cached grid for F{fxx} (this snapshot may have been generated before caching was added)",
        )
    cache_path = OUTPUT_DIR / cache_filename
    if not cache_path.exists():
        raise HTTPException(status_code=404, detail=f"Cached grid file missing: {cache_filename}")

    # Imported here rather than at module level to keep this file's own
    # top-level imports minimal and obviously safe on Railway -- these
    # are exactly the lightweight libraries in requirements.txt (numpy/
    # shapely/scipy/scikit-image/pyproj/geojson), never the heavy GRIB2 ones.
    from pipeline.hazards.ifr import polygonize_ifr_grid
    from pipeline.polygons import load_grid_cache

    values, grid_spec = load_grid_cache(cache_path)
    model_cycle = datetime.fromisoformat(manifest["model_cycle"].rstrip("Z"))

    fc = polygonize_ifr_grid(
        values, grid_spec, model_cycle, snapshot["actual_forecast_hour"],
        threshold_pct=threshold_pct,
        neighborhood_radius_nm=neighborhood_radius_nm,
        min_area_sq_mi=min_area_sq_mi,
    )

    if format == "xml":
        from pipeline.export_xml import geojson_to_xml
        return Response(content=geojson_to_xml(fc), media_type="application/xml")
    return fc


@app.get("/api/hazards/ifr/{fxx}")
def get_ifr_snapshot(fxx: str, format: str = "geojson"):
    """
    A specific forecast-hour snapshot, e.g. /api/hazards/ifr/06 for the
    6-hour snapshot. fxx is the REQUESTED forecast hour as it appears
    in the filename (ifr_f06.geojson) -- this may differ from the hour
    actually used internally for F00 specifically, since NBM has no
    true 0-hour file (see the manifest's "substituted" field).

    format=xml returns a simple XML representation instead of GeoJSON
    (see pipeline/export_xml.py) -- for handing off to N-AWIPS
    conversion tooling. Deliberately NOT the official NWS USWX/GML
    standard (a much heavier lift); N-AWIPS' own existing software
    handles that conversion downstream from this simpler draft form.
    """
    path = OUTPUT_DIR / f"ifr_f{fxx}.geojson"
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"No snapshot for F{fxx}")

    if format == "xml":
        from pipeline.export_xml import geojson_to_xml

        with open(path) as f:
            fc = json.load(f)
        return Response(content=geojson_to_xml(fc), media_type="application/xml")

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
