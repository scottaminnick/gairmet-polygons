"""
pipeline/hazards/ifr.py
------------------------
Real IFR (ceiling/visibility) hazard polygon generation from NBM
probabilistic guidance -- the first hazard-specific module built on top
of the hazard-agnostic pipeline.polygons + pipeline.regrid +
pipeline.fetch_nbm + pipeline.smoothing.

NWSI 10-811 defines IFR (for AIRMET/G-AIRMET purposes) as:
    "Ceiling less than 1,000 feet and/or visibility less than 3 SM"

We get there using FOUR real NBM probability fields, identified from
real NBM inventories during development (see pipeline/inspect_nbm.py):

    ceiling < 1000 ft     -> CEIL:surface:...:prob <304.8    (304.8m = 1000ft)
    visibility < 3 SM     -> VIS:surface:...:prob <4828.03   (4828.03m = 3SM)
    visibility < 1 SM     -> VIS:surface:...:prob <1609.34   (1609.34m = 1SM)
    measurable precip     -> APCP:surface:...:prob >0.254    (0.254mm = 0.01in,
                             the standard US definition of "measurable"), for
                             the RECENT 1-hour accumulation window specifically
                             (e.g. "5-6 hour acc fcst" at forecast hour 6) --
                             deliberately NOT the cumulative "0-X hour" window,
                             which would flag PCPN even if it rained hours ago
                             and has since stopped.

CAUSE vs. WEATHER TYPE: every polygon carries a "cause" property (CIG,
VIS, or CIG/VIS -- which underlying criterion made this IFR) exactly as
before. Polygons whose cause includes VIS ALSO get a "weather_type"
property (PCPN, BR, FG, or combinations like "BR/FG") -- what's
actually driving the visibility restriction, per NWSI 10-811 section
7.1's weather phenomena list. This is a heuristic, NOT NDFD's or NBM's
own categorical "Predominant Weather" grid -- both of those use an
identical, genuinely complex GRIB2 "Local Use Section" encoding
(confirmed directly: NBM's own docs use the same wording as NDFD's for
this) that would have required a fragile new dependency chain
(grib2io -> libg2c -> gfortran -> iplib, confirmed to fail to build
cleanly without real effort). Building our own heuristic from simple,
already-available probability fields sidesteps that complexity
entirely. HZ/FU/BLSN are deliberately NOT covered -- per AWC practice,
these are rare enough to add manually within NMAP when finalizing a
first-guess draft.

AWC LABELING CONVENTION (confirmed directly, not assumed): BR is a
catch-all included alongside more specific descriptors whenever
visibility crosses the 3SM threshold at all -- it is NEVER replaced by
FG, e.g. a foggy area still shows "BR/FG", not just "FG" alone. Where
PCPN and FG conditions genuinely overlap in the same location, PCPN
wins (it's the more likely actual cause of reduced visibility when
precipitation is genuinely occurring there) -- FG is only attributed
where visibility<1SM crosses threshold AND precipitation does NOT.

GEOGRAPHIC SPLITTING BY CAUSE: rather than one merged "is this IFR"
polygon set with a multi-tag label bolted on afterward, polygons are
generated from THREE INDEPENDENT layers, each carrying real (not
boolean-flattened) probability values into grid_to_polygons() so
marching squares still gets genuine sub-pixel-accurate contours:
  - CIG layer: the real ceiling grid, EXCLUDING areas where visibility
    also crosses threshold (those get captured by the other two layers
    instead, with cause attribution still correctly coming out as
    "CIG/VIS" there) -- ceiling and visibility restrictions coinciding
    is common in practice (e.g. a stratus deck), not a rare edge case,
    and without this exclusion the CIG layer and a visibility layer
    would both independently generate a redundant, perfectly-
    overlapping duplicate polygon over the same area (confirmed
    directly by testing this specific overlap during development).
  - PCPN layer: the real combined max(ceiling, visibility) value,
    but ONLY where precipitation crosses threshold (elsewhere pinned
    to a sentinel value that can never cross any real 0-100 threshold).
  - Visibility-non-precip layer: the real visibility<3SM value, but
    ONLY where precipitation does NOT cross threshold.
This one BR/FG-vs-PCPN split is real geography (separate polygon
shapes), not just separate labels -- exactly per AWC's stated
preference for breaking up areas of PCPN from areas of FG, while BR
itself (not being a distinct cause) doesn't get its own dedicated
split. The union of all three layers reconstructs exactly the same
overall IFR area as the original single combined-max approach.

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
smooth, all FOUR grids kept separate) and a CHEAP, NBM-independent
phase (polygonize_ifr_grid -- combine + threshold + merge + area filter
+ boundary smoothing + cause/weather-type attribution, using the three
forecaster-adjustable parameters). The pipeline (GitHub Actions) calls
both via generate_ifr_polygons(); the web app calls ONLY
polygonize_ifr_grid() against a cached copy of the four already-
prepared grids, so a forecaster can adjust threshold/radius/min-area
and see results (with correct attribution for whatever polygons result)
in about a second, without re-fetching from NBM each time.

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

# Exact filters confirmed against real NBM inventories (see
# pipeline/inspect_nbm.py's output) -- these substrings must ALL appear
# in a message's raw .idx line for find_message() to select it, and it
# raises if that's not exactly one message, so a subtle NBM format
# change would fail loudly here rather than silently grab the wrong field.
CEILING_PROB_FILTER = {"variable": "CEIL", "level": "cloud ceiling", "extra": "prob <304.8"}
VISIBILITY_PROB_FILTER = {"variable": "VIS", "level": "surface", "extra": "prob <4828.03"}  # 3SM
VISIBILITY_1SM_PROB_FILTER = {"variable": "VIS", "level": "surface", "extra": "prob <1609.34"}  # 1SM, for FG

# 0.254mm = 0.01in, the standard US definition of "measurable precipitation".
PRECIP_PROB_THRESHOLD = "prob >0.254"


def precip_filter_recent_window(fxx: int) -> dict:
    """
    Filter for measurable-precipitation probability over the RECENT
    1-hour window ending at this forecast hour (e.g. "5-6 hour acc fcst"
    for fxx=6) -- "is it precipitating right now," not "has it
    precipitated at some point since the model started."
    """
    return {"variable": "APCP", "level": "surface", "window": f"{fxx - 1}-{fxx} hour acc fcst", "prob": PRECIP_PROB_THRESHOLD}


def precip_filter_cumulative_window(fxx: int) -> dict:
    """
    Fallback for when the recent 1-hour window doesn't exist at this
    lead time (plausible at longer forecast hours, where NBM may only
    publish coarser accumulation windows) -- the cumulative window
    since the model run started.
    """
    return {"variable": "APCP", "level": "surface", "window": f"0-{fxx} hour acc fcst", "prob": PRECIP_PROB_THRESHOLD}


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

# A sentinel value used to "switch off" a grid cell for one layer's
# polygon generation -- guaranteed to never cross any real 0-100
# threshold_pct value, so np.where(condition, real_grid, LAYER_OFF)
# cleanly excludes cells from a layer without needing a second boolean
# mask downstream.
LAYER_OFF = -1.0


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


def fetch_precip_probability_grid(date: datetime, fxx: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    Fetches the measurable-precipitation probability grid, preferring
    the RECENT 1-hour accumulation window and falling back to the
    cumulative-since-model-start window only if that specific window
    doesn't exist at this lead time. See precip_filter_recent_window()'s
    docstring for why the recent window is preferred.
    """
    try:
        return fetch_probability_grid(date, fxx, precip_filter_recent_window(fxx))
    except ValueError:
        return fetch_probability_grid(date, fxx, precip_filter_cumulative_window(fxx))


def prepare_ifr_grid(date: datetime, fxx: int, target_resolution_deg: float = 0.025):
    """
    THE EXPENSIVE, NBM-DEPENDENT PHASE: fetches all four real NBM
    probability fields, regrids each to a common regular lon/lat grid,
    and applies the fixed (non-adjustable) Gaussian smoothing pass to
    each INDIVIDUALLY. Needs real internet access to NOAA's servers and
    the heavy cfgrib/xarray/eccodes stack -- this is what runs in
    GitHub Actions, never in the web app.

    Returns (ceiling_grid, visibility_3sm_grid, visibility_1sm_grid,
    precip_grid, grid_spec) -- pass straight into polygonize_ifr_grid(),
    or cache all four (see pipeline.polygons.save_grid_cache) for later
    fast re-processing with different forecaster-adjustable parameters.
    """
    ceil_values, ceil_lats, ceil_lons = fetch_probability_grid(date, fxx, CEILING_PROB_FILTER)
    vis3_values, vis3_lats, vis3_lons = fetch_probability_grid(date, fxx, VISIBILITY_PROB_FILTER)
    vis1_values, vis1_lats, vis1_lons = fetch_probability_grid(date, fxx, VISIBILITY_1SM_PROB_FILTER)
    precip_values, precip_lats, precip_lons = fetch_precip_probability_grid(date, fxx)

    ceil_regridded, grid_spec = regrid_to_regular_latlon(
        ceil_values, ceil_lats, ceil_lons, target_resolution_deg=target_resolution_deg
    )
    # NOTE: assumes all fields share the same native grid (true for
    # NBM's CONUS core file -- all fields in one file are on one grid),
    # so we reuse ceiling's grid_spec rather than recomputing it.
    vis3_regridded, _ = regrid_to_regular_latlon(
        vis3_values, vis3_lats, vis3_lons, target_resolution_deg=target_resolution_deg
    )
    vis1_regridded, _ = regrid_to_regular_latlon(
        vis1_values, vis1_lats, vis1_lons, target_resolution_deg=target_resolution_deg
    )
    precip_regridded, _ = regrid_to_regular_latlon(
        precip_values, precip_lats, precip_lons, target_resolution_deg=target_resolution_deg
    )

    # nan_to_num BEFORE smoothing: regridding can leave NaN just
    # outside the native grid's convex hull, and Gaussian smoothing
    # would otherwise spread that NaN into a larger surrounding area
    # than the original gap. Smoothed INDIVIDUALLY (not after
    # combining) so attribution reflects the same smoothed data that
    # actually gets thresholded.
    ceil_regridded = gaussian_smooth(np.nan_to_num(ceil_regridded), sigma_cells=GAUSSIAN_SIGMA_CELLS)
    vis3_regridded = gaussian_smooth(np.nan_to_num(vis3_regridded), sigma_cells=GAUSSIAN_SIGMA_CELLS)
    vis1_regridded = gaussian_smooth(np.nan_to_num(vis1_regridded), sigma_cells=GAUSSIAN_SIGMA_CELLS)
    precip_regridded = gaussian_smooth(np.nan_to_num(precip_regridded), sigma_cells=GAUSSIAN_SIGMA_CELLS)

    return ceil_regridded, vis3_regridded, vis1_regridded, precip_regridded, grid_spec


def _lonlat_ring_to_pixel_rowcol(ring_coords, grid_spec: GridSpec):
    """
    Converts a ring's (lon, lat) coordinates to fractional (row, col)
    pixel coordinates -- the inverse of GridSpec.to_affine(). Used to
    rasterize a final polygon back onto the grids it came from, to
    check which underlying conditions actually drove it.
    """
    rows, cols = [], []
    for lon, lat in ring_coords:
        col = (lon - (grid_spec.west - grid_spec.dx / 2)) / grid_spec.dx
        row = (lat - (grid_spec.north - grid_spec.dy / 2)) / grid_spec.dy
        rows.append(row)
        cols.append(col)
    return rows, cols


def _rasterize_polygon_cells(polygon, grid_spec: GridSpec, shape: tuple):
    """
    Returns (rr, cc) pixel indices for all grid cells inside a polygon
    (handles MultiPolygon by pooling cells across all parts). Shared by
    _determine_cause() and _determine_weather_type() -- both need "which
    cells does this final polygon's footprint cover" for their own
    threshold checks against different underlying grids.
    """
    from skimage.draw import polygon as sk_polygon

    parts = list(polygon.geoms) if polygon.geom_type == "MultiPolygon" else [polygon]
    all_rr, all_cc = [], []
    for part in parts:
        rows, cols = _lonlat_ring_to_pixel_rowcol(part.exterior.coords, grid_spec)
        rr, cc = sk_polygon(rows, cols, shape=shape)
        if len(rr):
            all_rr.append(rr)
            all_cc.append(cc)
    if not all_rr:
        return np.array([], dtype=int), np.array([], dtype=int)
    return np.concatenate(all_rr), np.concatenate(all_cc)


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
    rr, cc = _rasterize_polygon_cells(polygon, grid_spec, ceil_grid.shape)
    if len(rr) == 0:
        return "UNKNOWN"

    ceil_hit = (ceil_grid[rr, cc] >= threshold_pct).any()
    vis_hit = (vis_grid[rr, cc] >= threshold_pct).any()

    if ceil_hit and vis_hit:
        return "CIG/VIS"
    if ceil_hit:
        return "CIG"
    if vis_hit:
        return "VIS"
    return "UNKNOWN"


def _determine_weather_type(
    polygon, grid_spec: GridSpec, precip_grid: np.ndarray, vis3_grid: np.ndarray, vis1_grid: np.ndarray, threshold_pct: float
) -> str | None:
    """
    Determines which weather-type label(s) apply within a polygon's
    footprint, per confirmed AWC convention:
      - "PCPN" if measurable precipitation crosses threshold anywhere.
      - "BR" if visibility<3SM crosses threshold anywhere -- a
        catch-all, included alongside PCPN and/or FG whenever true,
        NEVER replaced by the more specific ones.
      - "FG" if visibility<1SM crosses threshold anywhere -- but ONLY
        where precipitation does NOT also cross threshold in those same
        cells (PCPN wins any overlap: if it's genuinely precipitating
        and visibility drops below 1SM, the precip is the more likely
        actual cause, not fog).
    Combined with "/", e.g. "PCPN/BR" or "BR/FG". Returns None if
    nothing crosses threshold anywhere in the footprint (shouldn't
    normally happen for a polygon whose cause included VIS, but handled
    gracefully rather than assumed).
    """
    rr, cc = _rasterize_polygon_cells(polygon, grid_spec, vis3_grid.shape)
    if len(rr) == 0:
        return None

    precip_here = precip_grid[rr, cc] >= threshold_pct
    vis3_here = vis3_grid[rr, cc] >= threshold_pct
    vis1_here = vis1_grid[rr, cc] >= threshold_pct

    labels = []
    if precip_here.any():
        labels.append("PCPN")
    if vis3_here.any():
        labels.append("BR")
    if (vis1_here & ~precip_here).any():  # PCPN wins the overlap -- see docstring
        labels.append("FG")

    return "/".join(labels) if labels else None


def polygonize_ifr_grid(
    ceil_grid: np.ndarray,
    vis3_grid: np.ndarray,
    vis1_grid: np.ndarray,
    precip_grid: np.ndarray,
    grid_spec,
    date: datetime,
    fxx: int,
    threshold_pct: float = 50.0,
    neighborhood_radius_nm: float = 50.0,
    min_area_sq_mi: float = 3000.0,
) -> dict:
    """
    THE CHEAP, NBM-INDEPENDENT PHASE: given already-prepared ceiling,
    visibility (both thresholds), and precipitation probability grids
    (see prepare_ifr_grid()), applies the three forecaster-adjustable
    parameters and returns a GeoJSON FeatureCollection shaped to
    resemble a forecaster-drawn product -- with each polygon's "cause"
    (CIG/VIS/CIG/VIS) and, where cause includes VIS, "weather_type"
    (PCPN/BR/FG combinations) attributed against the ORIGINAL separate
    grids.

    Polygons are generated from THREE INDEPENDENT layers (CIG, PCPN,
    and visibility-non-precip) rather than one combined mask, so
    precip-driven and non-precip-driven visibility restriction areas
    come out as genuinely separate polygon shapes when they're
    geographically distinct -- see this module's docstring for the full
    reasoning and the real-valued (not boolean-flattened) sentinel
    trick used to preserve sub-pixel-accurate contours per layer.

    Safe to call repeatedly against the SAME cached grids with different
    parameter values -- no NBM access, no heavy geospatial parsing, just
    numpy/shapely/scipy/pyproj math. This is what the web app's live
    parameter-adjustment endpoint calls.

    Parameters
    ----------
    ceil_grid, vis3_grid, vis1_grid, precip_grid : 2D arrays
        Prepared probability grids from prepare_ifr_grid().
    grid_spec : pipeline.polygons.GridSpec
        Matching grid_spec from prepare_ifr_grid().
    date : datetime
        Model cycle initialization time (naive, UTC).
    fxx : int
        Forecast hour (used to compute valid_time and for the output's
        "forecast_hour" property -- doesn't affect the math at all).
    threshold_pct : float
        Probability (0-100) above which a grid cell counts as "hazard
        present" for whichever field is being checked. Forecaster-
        adjustable -- 50% is the project's starting default, not a
        fixed rule.
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
    precip_mask = precip_grid >= threshold_pct
    vis3_mask = vis3_grid >= threshold_pct

    # Three independently-generated layers, each fed REAL probability
    # values (not a flattened boolean grid) so grid_to_polygons()'s
    # marching-squares contouring still gets genuine sub-pixel-accurate
    # boundaries. LAYER_OFF cells can never cross any real threshold_pct
    # (0-100), cleanly excluding them from that layer without needing a
    # second mask downstream.
    #
    # CIG layer excludes areas where visibility ALSO crosses threshold:
    # ceiling and visibility restrictions coinciding (e.g. a stratus
    # deck) is common, not a rare edge case, and without this exclusion
    # the CIG layer and a visibility layer would BOTH independently
    # generate a polygon over the exact same area -- confirmed directly
    # by testing this specific overlap, which produced two redundant,
    # perfectly-overlapping duplicate polygons with identical labels.
    # Excluding here means that area is captured ONCE, by whichever
    # visibility layer applies, with cause correctly still coming out as
    # "CIG/VIS" (cause attribution checks the real ceil_grid regardless
    # of which layer produced the polygon's SHAPE).
    layers = [
        ("cig", np.where(vis3_mask, LAYER_OFF, ceil_grid)),
        ("pcpn", np.where(precip_mask, np.maximum(ceil_grid, vis3_grid), LAYER_OFF)),
        ("vis_nonprecip", np.where(precip_mask, LAYER_OFF, vis3_grid)),
    ]

    all_polygons = []
    all_per_polygon_properties = []

    for _layer_name, layer_grid in layers:
        # Contour close to native resolution first -- preserves real sharp
        # features (e.g. a coastline) instead of blurring them away. Only a
        # tiny area filter here, just to drop single-pixel-scale noise; the
        # REAL area filter happens after merging, below.
        polygons = grid_to_polygons(layer_grid, grid_spec, threshold=threshold_pct, min_area_deg2=0.001)

        polygons = merge_nearby_polygons(polygons, radius_nm=neighborhood_radius_nm)
        polygons = filter_polygons_by_area(polygons, min_area_sq_mi=min_area_sq_mi)
        polygons = [
            smooth_polygon_boundary(p, smoothing_deg=BOUNDARY_SMOOTHING_DEG, join_style=2)  # mitre, not round
            for p in polygons
        ]
        polygons = [p.simplify(FINAL_SIMPLIFY_TOLERANCE_DEG, preserve_topology=True) for p in polygons]
        polygons = [p for p in polygons if not p.is_empty]

        # Attribution computed on the FINAL polygon shapes (after all
        # smoothing/simplification), so it matches exactly what's being
        # displayed/exported rather than a slightly different
        # pre-smoothing shape. Uses the SAME cell-level precedence logic
        # (PCPN wins any overlap with FG) regardless of which layer a
        # polygon's SHAPE came from -- the layer only determines
        # geography, not labeling.
        for p in polygons:
            cause = _determine_cause(p, grid_spec, ceil_grid, vis3_grid, threshold_pct)
            props = {"cause": cause}
            if "VIS" in cause:
                weather_type = _determine_weather_type(p, grid_spec, precip_grid, vis3_grid, vis1_grid, threshold_pct)
                if weather_type:
                    props["weather_type"] = weather_type
            all_polygons.append(p)
            all_per_polygon_properties.append(props)

    valid_time = date + timedelta(hours=fxx)
    return polygons_to_feature_collection(
        all_polygons,
        properties={
            "hazard": "IFR",
            "threshold_pct": threshold_pct,
            "neighborhood_radius_nm": neighborhood_radius_nm,
            "min_area_sq_mi": min_area_sq_mi,
            "valid_time": valid_time.isoformat() + "Z",
            "model_cycle": date.isoformat() + "Z",
            "forecast_hour": fxx,
        },
        per_polygon_properties=all_per_polygon_properties,
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
    ceil_grid, vis3_grid, vis1_grid, precip_grid, grid_spec = prepare_ifr_grid(
        date, fxx, target_resolution_deg=target_resolution_deg
    )
    return polygonize_ifr_grid(
        ceil_grid, vis3_grid, vis1_grid, precip_grid, grid_spec, date, fxx,
        threshold_pct=threshold_pct,
        neighborhood_radius_nm=neighborhood_radius_nm,
        min_area_sq_mi=min_area_sq_mi,
    )
