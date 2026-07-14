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

CAUSE ATTRIBUTION: the two fields are kept SEPARATE all the way through
to the final polygons (not just combined and discarded) specifically so
each polygon can carry a "cause" property -- "CIG", "VIS", or "CIG/VIS"
-- indicating whether ceiling, visibility, or both crossed threshold
somewhere within that polygon's footprint. This matches how real
forecaster-drawn G-AIRMET graphics annotate what's actually driving an
IFR area (as opposed to just drawing an unlabeled blob). Weather-TYPE
attribution (PCPN/BR/FG/HZ/FU/BLSN -- what's causing low visibility
specifically) is a separate, larger effort requiring a different data
source (NDFD's Predominant Weather grid) and is not yet implemented.

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
NBM-dependent phase (prepare_ifr_grid -- fetch + regrid + Gaussian
smooth, kept as two SEPARATE grids) and a CHEAP, NBM-independent phase
(polygonize_ifr_grid -- combine + threshold + merge + area filter +
boundary smoothing + cause attribution, using the three forecaster-
adjustable parameters). The pipeline (GitHub Actions) calls both via
generate_ifr_polygons(); the web app calls ONLY polygonize_ifr_grid()
against a cached copy of the two already-prepared grids, so a
forecaster can adjust threshold/radius/min-area and see results (with
correct cause attribution for whatever polygons result) in about a
second, without re-fetching from NBM each time.

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
    GridSpec,
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
    visibility probability data, regrids each to a common regular
    lon/lat grid, and applies the fixed (non-adjustable) Gaussian
    smoothing pass to each INDIVIDUALLY. Needs real internet access to
    NOAA's servers and the heavy cfgrib/xarray/eccodes stack -- this is
    what runs in GitHub Actions, never in the web app.

    Deliberately keeps ceiling and visibility SEPARATE (does not
    combine via max() here) -- polygonize_ifr_grid() needs both
    individually to attribute each final polygon's cause (ceiling,
    visibility, or both).

    Returns (ceiling_grid, visibility_grid, grid_spec) -- pass straight
    into polygonize_ifr_grid(), or cache both (see
    pipeline.polygons.save_grid_cache) for later fast re-processing
    with different forecaster-adjustable parameters.
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

    # nan_to_num BEFORE smoothing: regridding can leave NaN just
    # outside the native grid's convex hull, and Gaussian smoothing
    # would otherwise spread that NaN into a larger surrounding area
    # than the original gap. Smoothed INDIVIDUALLY (not after
    # combining) so cause attribution reflects the same smoothed data
    # that actually gets thresholded.
    ceil_regridded = gaussian_smooth(np.nan_to_num(ceil_regridded), sigma_cells=GAUSSIAN_SIGMA_CELLS)
    vis_regridded = gaussian_smooth(np.nan_to_num(vis_regridded), sigma_cells=GAUSSIAN_SIGMA_CELLS)

    return ceil_regridded, vis_regridded, grid_spec


def _lonlat_ring_to_pixel_rowcol(ring_coords, grid_spec: GridSpec):
    """
    Converts a ring's (lon, lat) coordinates to fractional (row, col)
    pixel coordinates -- the inverse of GridSpec.to_affine(). Used to
    rasterize a final polygon back onto the grid it came from, to check
    which underlying (ceiling/visibility) field actually drove it.
    """
    rows, cols = [], []
    for lon, lat in ring_coords:
        col = (lon - (grid_spec.west - grid_spec.dx / 2)) / grid_spec.dx
        row = (lat - (grid_spec.north - grid_spec.dy / 2)) / grid_spec.dy
        rows.append(row)
        cols.append(col)
    return rows, cols


def _determine_cause(polygon, grid_spec: GridSpec, ceil_grid: np.ndarray, vis_grid: np.ndarray, threshold_pct: float) -> str:
    """
    Determines whether ceiling, visibility, or both crossed threshold
    somewhere within a final polygon's footprint, using the ORIGINAL
    (pre-combine) ceiling/visibility grids -- returns "CIG", "VIS",
    "CIG/VIS", or "UNKNOWN" (the last only in a degenerate edge case,
    e.g. a polygon smaller than a single grid cell after simplification).

    Uses skimage.draw.polygon() to rasterize the polygon's EXTERIOR ring
    back onto the grid (deliberately not subtracting holes -- for a
    "does this condition occur anywhere in here" check, treating a
    small hole's cells as part of the checked area is a harmless,
    negligible over-inclusion, not worth the extra complexity). Handles
    MultiPolygon (confirmed to occur in practice -- see
    pipeline/export_xml.py's docstring) by checking across all parts.
    """
    from skimage.draw import polygon as sk_polygon

    parts = list(polygon.geoms) if polygon.geom_type == "MultiPolygon" else [polygon]

    ceil_hit = False
    vis_hit = False
    for part in parts:
        rows, cols = _lonlat_ring_to_pixel_rowcol(part.exterior.coords, grid_spec)
        rr, cc = sk_polygon(rows, cols, shape=ceil_grid.shape)
        if len(rr) == 0:
            continue
        if (ceil_grid[rr, cc] >= threshold_pct).any():
            ceil_hit = True
        if (vis_grid[rr, cc] >= threshold_pct).any():
            vis_hit = True

    if ceil_hit and vis_hit:
        return "CIG/VIS"
    if ceil_hit:
        return "CIG"
    if vis_hit:
        return "VIS"
    return "UNKNOWN"


def polygonize_ifr_grid(
    ceil_grid: np.ndarray,
    vis_grid: np.ndarray,
    grid_spec,
    date: datetime,
    fxx: int,
    threshold_pct: float = 50.0,
    neighborhood_radius_nm: float = 50.0,
    min_area_sq_mi: float = 3000.0,
) -> dict:
    """
    THE CHEAP, NBM-INDEPENDENT PHASE: given already-prepared ceiling and
    visibility probability grids (see prepare_ifr_grid()), combines them,
    applies the three forecaster-adjustable parameters, and returns a
    GeoJSON FeatureCollection shaped to resemble a forecaster-drawn
    product -- with each polygon's "cause" ("CIG", "VIS", or "CIG/VIS")
    attributed against the ORIGINAL separate grids.

    Safe to call repeatedly against the SAME cached grids with different
    parameter values -- no NBM access, no heavy geospatial parsing, just
    numpy/shapely/scipy/pyproj math. This is what the web app's live
    parameter-adjustment endpoint calls.

    Parameters
    ----------
    ceil_grid, vis_grid : 2D arrays
        Prepared probability grids from prepare_ifr_grid().
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
    dict (GeoJSON FeatureCollection) -- each feature's properties
    include a per-polygon "cause" alongside the shared metadata.
    """
    combined = np.maximum(ceil_grid, vis_grid)

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

    # Cause attribution computed on the FINAL polygon shapes (after all
    # smoothing/simplification), so it matches exactly what's being
    # displayed/exported rather than a slightly different pre-smoothing
    # shape.
    per_polygon_properties = [
        {"cause": _determine_cause(p, grid_spec, ceil_grid, vis_grid, threshold_pct)} for p in polygons
    ]

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
        per_polygon_properties=per_polygon_properties,
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
    ceil_grid, vis_grid, grid_spec = prepare_ifr_grid(date, fxx, target_resolution_deg=target_resolution_deg)
    return polygonize_ifr_grid(
        ceil_grid, vis_grid, grid_spec, date, fxx,
        threshold_pct=threshold_pct,
        neighborhood_radius_nm=neighborhood_radius_nm,
        min_area_sq_mi=min_area_sq_mi,
    )
