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
edges that wouldn't look like an operational product, and real
forecaster-drawn polygons have straight segments and sharp vertices,
not smooth curves. Getting there is a POLYGON-level pipeline, not a
grid-blurring one:

  1. Contour first, at close to native resolution -- this preserves
     genuinely sharp real features (e.g. a West Coast marine layer's
     abrupt land/water cutoff) instead of blurring them away.
  2. Merge nearby polygons (pipeline.polygons.merge_nearby_polygons) --
     pulls smaller areas into larger ones by unioning polygons that are
     within some real-world radius of each other. Deliberately NOT a
     grid-level circular blur: an earlier version did that and it
     inflated every isolated hazard area into a literal circle (visible
     directly on a real forecaster-comparison screenshot -- grid-level
     dilation grows EVERY point by the same disk regardless of whether
     there's anything nearby to merge with; polygon-level closing
     naturally leaves isolated shapes close to their original form).
  3. Geodesic-area filtering -- drops anything smaller than the real
     AIRMET/G-AIRMET "widespread" criterion (historically 3,000 sq mi),
     applied AFTER merging so small areas that successfully merged into
     something bigger survive.
  4. Mitre-jointed boundary smoothing + a generous simplify pass --
     knocks off small jagged noise using SHARP (not round) joins, then
     reduces vertex count enough to look like a small number of
     hand-drawn straight segments rather than a dense, pixel-following
     contour.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np
import xarray as xr

from pipeline.fetch_nbm import fetch_idx, fetch_message_bytes, find_message, parse_idx, save_message_to_tempfile
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
# rounded bulges; most of that problem turned out to be the (now
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
    """
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


def generate_ifr_polygons(
    date: datetime,
    fxx: int,
    threshold_pct: float = 50.0,
    neighborhood_radius_nm: float = 50.0,
    min_area_sq_mi: float = 3000.0,
    target_resolution_deg: float = 0.025,
) -> dict:
    """
    Fetches real NBM ceiling + visibility probability data for the
    given model cycle (date) and forecast hour (fxx), combines and
    generalizes them, and returns a GeoJSON FeatureCollection of IFR
    hazard polygons shaped to resemble a forecaster-drawn product
    rather than raw NBM-resolution detail.

    Parameters
    ----------
    date : datetime
        Model cycle initialization time (naive, UTC -- see
        pipeline/inspect_nbm.py's note on why this must NOT be
        timezone-aware).
    fxx : int
        Forecast hour.
    threshold_pct : float
        Probability (0-100) above which a grid cell counts as "IFR
        hazard present." Forecaster-adjustable -- 50% is the project's
        starting default, not a fixed rule.
    neighborhood_radius_nm : float
        Real-world radius (nautical miles) used to merge nearby smaller
        hazard polygons into larger ones (see
        pipeline.polygons.merge_nearby_polygons). 0 disables this.
        Forecaster-adjustable.
    min_area_sq_mi : float
        Polygons smaller than this (true geodesic area, checked AFTER
        merging) are dropped. Matches AIRMET/G-AIRMET's historical
        3,000 sq mi "widespread" criterion by default.
        Forecaster-adjustable.
    target_resolution_deg : float
        Resolution of the regridded lon/lat output, in degrees.

    Returns
    -------
    dict (GeoJSON FeatureCollection)
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
