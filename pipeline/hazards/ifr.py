"""
pipeline/hazards/ifr.py
------------------------
Real IFR (ceiling/visibility) hazard polygon generation from NBM
probabilistic guidance -- the first hazard-specific module built on top
of the hazard-agnostic pipeline.polygons + pipeline.regrid +
pipeline.fetch_nbm + pipeline.smoothing.

NWSI 10-811 defines IFR (for AIRMET/G-AIRMET purposes) as:
    "Ceiling less than 1,000 feet and/or visibility less than 3 SM"

We get there using two real NBM probability fields, identified from an
actual NBM inventory during development (see pipeline/inspect_nbm.py):

    ceiling < 1000 ft   -> CEIL:cloud ceiling:...:prob <304.8   (304.8m = 1000ft)
    visibility < 3 SM   -> VIS:surface:...:prob <4828.03        (4828.03m = 3SM)

"ceiling<1000 OR visibility<3SM" is combined per-gridcell by taking the
MAX of the two probabilities -- a standard, practical approximation for
an OR condition (not mathematically exact unless the two fields'
correlation happens to work out that way, but this was a deliberate,
discussed simplification, not an oversight).

RAW NBM RESOLUTION vs. FORECASTER-DRAWN LOOK: NBM's ~2.5km resolution
produces far more small-scale detail than a real G-AIRMET forecaster
draws by hand in N-AWIPS -- lots of tiny, separate polygons and jagged
edges. Getting there is a POLYGON-level pipeline (contour close to
native resolution -> merge nearby polygons -> geodesic area filter ->
mitre-jointed boundary smoothing + simplify), not a grid-blurring one --
see merge_nearby_polygons()'s docstring in pipeline/polygons.py for why
that distinction matters.

TWO-PHASE DESIGN (important for the web app's live parameter
adjustment): this module is deliberately split into an EXPENSIVE,
NBM-dependent phase (prepare_ifr_grid -- fetch + regrid + combine +
Gaussian smooth) and a CHEAP, NBM-independent phase (polygonize_ifr_grid
-- threshold + merge + area filter + boundary smoothing, using the
three forecaster-adjustable parameters). The pipeline (GitHub Actions)
calls both via generate_ifr_polygons(); the web app calls ONLY
polygonize_ifr_grid() against a cached copy of an already-prepared grid,
so a forecaster can adjust threshold/radius/min-area and see results in
about a second, without re-fetching from NBM each time.

This is also why fetch_probability_grid()'s imports of xarray and
pipeline.fetch_nbm are deferred to INSIDE the function rather than at
module level: Railway's web app imports this module for
polygonize_ifr_grid() alone, and doesn't have (and doesn't need)
xarray/cfgrib/eccodes installed. A module-level `import xarray` would
crash the web app on import before it ever got the chance to not use it.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np

from pipeline.polygons import (
    filter_polygons_by_area,
    grid_to_polygons,
    merge_nearby_polygons,
    polygons_to_feature_collection,
    smooth_polygon_boundary,
)
from pipeline.regrid import regrid_to_regular_latlon
from pipeline.smoothing import gaussian_smooth

# Exact filters confirmed against a real NBM inventory (see
# pipeline/inspect_nbm.py's output) -- these substrings must ALL appear
# in a message's raw .idx line for find_message() to select it, and it
# raises if that's not exactly one message, so a subtle NBM format
# change would fail loudly here rather than silently grab the wrong field.
CEILING_PROB_FILTER = {"variable": "CEIL", "level": "cloud ceiling", "extra": "prob <304.8"}
VISIBILITY_PROB_FILTER = {"variable": "VIS", "level": "surface", "extra": "prob <4828.03"}

# Fixed (not forecaster-exposed) cosmetic parameters -- these affect HOW
# things look, not what counts as a hazard, so unlike
# threshold/neighborhood-radius/min-area they're not exposed as
# forecaster-adjustable knobs. Easy to promote to parameters later.
#
# GAUSSIAN_SIGMA_CELLS is deliberately light -- just enough to knock
# down single-pixel grid noise, NOT enough to blur away a real sharp
# transition like a marine layer's coastal edge. A heavier touch was
# tried and reduced (this used to be 1.5) after real output showed
# rounded bulges; most of that problem turned out to be a (since
# removed) grid-level neighborhood-max filter, but keeping this light
# too errs on the side of preserving real sharp gradients.
GAUSSIAN_SIGMA_CELLS = 0.6
BOUNDARY_SMOOTHING_DEG = 0.02
FINAL_SIMPLIFY_TOLERANCE_DEG = 0.05


def fetch_probability_grid(date: datetime, fxx: int, filters: dict) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    Fetches one probability field's message from NBM and returns
    (values, native_lats, native_lons) as decoded by cfgrib/eccodes
    from that message's own grid definition.

    NOTE: imports xarray and pipeline.fetch_nbm locally (not at module
    level) -- see this module's docstring for why that matters for the
    web app's lightweight footprint.
    """
    import xarray as xr

    from pipeline.fetch_nbm import fetch_idx, fetch_message_bytes, find_message, parse_idx, save_message_to_tempfile

    raw_idx, grib_url = fetch_idx(date, fxx)
    rows = parse_idx(raw_idx)
    message = find_message(rows, **filters)
    raw_bytes = fetch_message_bytes(grib_url, rows, message)
    path = save_message_to_tempfile(raw_bytes)

    ds = xr.open_dataset(path, engine="cfgrib")
    # NBM probability fields often don't have a friendly cfgrib
    # variable name (frequently shows up as "unknown" or similar,
    # since cfgrib doesn't have a name mapped for every GRIB2
    # parameter) -- this tiny single-message dataset only has the one
    # variable we asked for, so just grab whichever it is.
    varname = list(ds.data_vars)[0]
    da = ds[varname]
    return da.values, da.latitude.values, da.longitude.values


def prepare_ifr_grid(date: datetime, fxx: int, target_resolution_deg: float = 0.025):
    """
    THE EXPENSIVE, NBM-DEPENDENT PHASE: fetches real NBM ceiling +
    visibility probability data, regrids to a common regular lon/lat
    grid, combines via max(), and applies the fixed (non-adjustable)
    Gaussian smoothing pass. Needs real internet access to NOAA's
    servers and the heavy cfgrib/xarray/eccodes stack -- this is what
    runs in GitHub Actions, never in the web app.

    Returns (combined_grid, grid_spec) -- pass straight into
    polygonize_ifr_grid(), or cache it (see pipeline.polygons.
    save_grid_cache) for later fast re-processing with different
    forecaster-adjustable parameters.
    """
    ceil_values, ceil_lats, ceil_lons = fetch_probability_grid(date, fxx, CEILING_PROB_FILTER)
    vis_values, vis_lats, vis_lons = fetch_probability_grid(date, fxx, VISIBILITY_PROB_FILTER)

    ceil_regridded, grid_spec = regrid_to_regular_latlon(
        ceil_values, ceil_lats, ceil_lons, target_resolution_deg=target_resolution_deg
    )
    # NOTE: assumes both fields share the same native grid (true for
    # NBM's CONUS core file -- all fields in one file are on one grid),
    # so we reuse ceiling's grid_spec rather than recomputing it.
    vis_regridded, _ = regrid_to_regular_latlon(
        vis_values, vis_lats, vis_lons, target_resolution_deg=target_resolution_deg
    )

    # nan_to_num BEFORE combining/smoothing: regridding can leave NaN
    # just outside the native grid's convex hull, and Gaussian
    # smoothing would otherwise spread that NaN into a larger
    # surrounding area than the original gap.
    combined = np.maximum(np.nan_to_num(ceil_regridded), np.nan_to_num(vis_regridded))
    combined = gaussian_smooth(combined, sigma_cells=GAUSSIAN_SIGMA_CELLS)
    return combined, grid_spec


def polygonize_ifr_grid(
    combined: np.ndarray,
    grid_spec,
    date: datetime,
    fxx: int,
    threshold_pct: float = 50.0,
    neighborhood_radius_nm: float = 50.0,
    min_area_sq_mi: float = 3000.0,
) -> dict:
    """
    THE CHEAP, NBM-INDEPENDENT PHASE: given an already-prepared
    probability grid (see prepare_ifr_grid()), applies the three
    forecaster-adjustable parameters and returns a GeoJSON
    FeatureCollection shaped to resemble a forecaster-drawn product.

    Safe to call repeatedly against the SAME cached grid with different
    parameter values -- no NBM access, no heavy geospatial parsing, just
    numpy/shapely/scipy/pyproj math. This is what the web app's live
    parameter-adjustment endpoint calls.

    Parameters
    ----------
    combined : 2D array
        Prepared probability grid from prepare_ifr_grid().
    grid_spec : pipeline.polygons.GridSpec
        Matching grid_spec from prepare_ifr_grid().
    date : datetime
        Model cycle initialization time (naive, UTC).
    fxx : int
        Forecast hour (used to compute valid_time and for the output's
        "forecast_hour" property -- doesn't affect the math at all).
    threshold_pct : float
        Probability (0-100) above which a grid cell counts as "IFR
        hazard present." Forecaster-adjustable -- 50% is the project's
        starting default, not a fixed rule.
    neighborhood_radius_nm : float
        Real-world radius (nautical miles) used to merge nearby smaller
        hazard polygons into larger ones. 0 disables this.
        Forecaster-adjustable.
    min_area_sq_mi : float
        Polygons smaller than this (true geodesic area, checked AFTER
        merging) are dropped. Matches AIRMET/G-AIRMET's historical
        3,000 sq mi "widespread" criterion by default.
        Forecaster-adjustable.

    Returns
    -------
    dict (GeoJSON FeatureCollection)
    """
    # Contour close to native resolution first -- preserves real sharp
    # features (e.g. a coastline) instead of blurring them away. Only a
    # tiny area filter here, just to drop single-pixel-scale noise; the
    # REAL area filter happens after merging, below.
    polygons = grid_to_polygons(combined, grid_spec, threshold=threshold_pct, min_area_deg2=0.001)

    polygons = merge_nearby_polygons(polygons, radius_nm=neighborhood_radius_nm)
    polygons = filter_polygons_by_area(polygons, min_area_sq_mi=min_area_sq_mi)
    polygons = [
        smooth_polygon_boundary(p, smoothing_deg=BOUNDARY_SMOOTHING_DEG, join_style=2)  # mitre, not round
        for p in polygons
    ]
    polygons = [p.simplify(FINAL_SIMPLIFY_TOLERANCE_DEG, preserve_topology=True) for p in polygons]
    polygons = [p for p in polygons if not p.is_empty]

    valid_time = date + timedelta(hours=fxx)
    return polygons_to_feature_collection(
        polygons,
        properties={
            "hazard": "IFR",
            "threshold_pct": threshold_pct,
            "neighborhood_radius_nm": neighborhood_radius_nm,
            "min_area_sq_mi": min_area_sq_mi,
            "valid_time": valid_time.isoformat() + "Z",
            "model_cycle": date.isoformat() + "Z",
            "forecast_hour": fxx,
        },
    )


def generate_ifr_polygons(
    date: datetime,
    fxx: int,
    threshold_pct: float = 50.0,
    neighborhood_radius_nm: float = 50.0,
    min_area_sq_mi: float = 3000.0,
    target_resolution_deg: float = 0.025,
) -> dict:
    """
    Full pipeline in one call: fetch + prepare + polygonize. Thin
    wrapper around prepare_ifr_grid() + polygonize_ifr_grid(), kept for
    existing callers (pipeline/generate_latest_ifr.py,
    pipeline/test_live_ifr_fetch.py) that just want a one-shot result
    without caring about the two-phase split.
    """
    combined, grid_spec = prepare_ifr_grid(date, fxx, target_resolution_deg=target_resolution_deg)
    return polygonize_ifr_grid(
        combined, grid_spec, date, fxx,
        threshold_pct=threshold_pct,
        neighborhood_radius_nm=neighborhood_radius_nm,
        min_area_sq_mi=min_area_sq_mi,
    )
