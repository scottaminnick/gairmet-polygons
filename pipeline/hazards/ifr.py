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

AREA OF RESPONSIBILITY: all three layers are clipped to AWC's own
20-ARTCC CONUS boundary (data/boundaries/artcc.json, via
pipeline.boundaries.get_boundary_mask) before contouring. NBM's CONUS
grid has real coverage far beyond that -- out over the Pacific, up into
Canada, out over the Atlantic -- and IFR conditions out there are real
data that this product simply isn't responsible for. Same file, same
helper, and same sentinel-pinning technique MTN OBSC already uses for
the identical problem with its generously-sized terrain grid; the ARTCC
boundary deliberately includes adjacent coastal waters per NWSI 10-811,
so unlike MTN OBSC (which also needs a real coastline to exclude water)
this is the only geographic gate IFR needs.

RAW NBM RESOLUTION vs. FORECASTER-DRAWN LOOK: NBM's ~2.5km resolution
produces far more small-scale detail than a real G-AIRMET forecaster
draws by hand in N-AWIPS -- lots of tiny, separate polygons and jagged
edges. Getting there is a POLYGON-level pipeline (contour close to
native resolution -> merge nearby polygons -> geodesic area filter ->
mitre-jointed boundary smoothing + simplify), not a grid-blurring one --
see merge_nearby_polygons()'s docstring in pipeline/polygons.py for why
that distinction matters.

TWO IMPLEMENTATIONS OF THE POLYGONIZE PHASE: polygonize_ifr_grid()
(v1) is the original vector pipeline -- three layers, each closed by
merge_nearby_polygons(), each contoured separately. It can emit
overlapping and nested polygons, which downstream FAA vendors can't
parse; the cause is structural (see the LABEL-GRID POLYGONIZATION
comment block below). polygonize_ifr_grid_v2() replaces it with a
single raster partition contoured once, which cannot overlap by
construction. USE_LABEL_GRID_POLYGONIZE picks between them and
polygonize_ifr_grid_active() is what production callers actually call,
so reverting to v1 while forecasters review v2's output is a one-line
change. v1 and its tests are deliberately left untouched.

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

import math
from datetime import datetime, timedelta

import numpy as np
from scipy.ndimage import binary_dilation, binary_fill_holes, distance_transform_edt, find_objects
from shapely.geometry import Polygon as ShapelyPolygon
from skimage import measure

from pipeline.boundaries import CONUS_BOUNDARY_PATH, get_boundary_mask
from pipeline.polygons import (
    GridSpec,
    filter_polygons_by_area,
    grid_to_polygons,
    lonlat_ring_to_pixel_rowcol,
    merge_nearby_polygons,
    polygons_to_feature_collection,
    rasterize_polygon_cells,
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


# ---------------------------------------------------------------------------
# LABEL-GRID POLYGONIZATION (v2) -- see polygonize_ifr_grid_v2() below.
# ---------------------------------------------------------------------------
# WHY THIS EXISTS: polygonize_ifr_grid() (v1, above) builds three
# cell-disjoint layers and then runs merge_nearby_polygons() -- a
# morphological closing done in VECTOR space -- on each layer
# INDEPENDENTLY. The cig layer has holes punched in it wherever
# visibility also crosses threshold; closing that layer fills those
# holes straight back in, so the cig polygon ends up covering the same
# ground as the vis_nonprecip polygon. The result is overlapping and
# nested polygons, both attributed identically (because _determine_cause
# / _determine_weather_type run on the FINAL hole-filled shapes, not on
# the cells that actually produced them). Confirmed empirically: the
# overlap count scales with the radius slider.
#
# Downstream FAA vendors cannot parse overlapping or nested rings, so
# this needs a STRUCTURAL guarantee rather than a patch. v2 moves every
# topology decision into raster space:
#   1. each cell gets exactly ONE class (see IFR_CLASS_LABELS), so two
#      regions can never claim the same ground -- overlap is impossible
#      by construction, not by post-hoc repair;
#   2. the neighborhood radius closes the UNION envelope once, not each
#      layer separately, so closing can't re-fill another layer's holes;
#   3. contours are traced once per final region from disjoint masks.
#      The 0.5 isoline between an A cell and a B cell is the same
#      geometric line whichever side you trace it from, so adjacent
#      regions share their boundary EXACTLY -- which is why v2 must not
#      smooth or simplify afterwards (see polygonize_ifr_grid_v2()).
#
# The forecaster-facing switch. Flip to False for a one-line revert to
# the v1 vector path while forecasters review v2's output -- nothing
# else needs editing, since production callers go through
# polygonize_ifr_grid_active() rather than either implementation
# directly. Both implementations take (and mean) exactly the same
# arguments.
USE_LABEL_GRID_POLYGONIZE = True

# The per-cell classes. A hazard cell carries exactly one of these, so
# "which cause/weather does this ground have" is answered ONCE, before
# any geometry exists, rather than re-derived per polygon afterwards.
#
# cause    : CIG where ceiling alone crosses, VIS where visibility alone
#            crosses, CIG/VIS where both do.
# weather  : only meaningful where visibility crosses -- PCPN if precip
#            also crosses, else FG if visibility<1SM crosses, else BR.
#            PCPN winning over FG on overlap is the existing convention
#            (see this module's docstring): if it is genuinely
#            precipitating, precipitation is the more likely cause of
#            the restriction than fog.
#
# Kept as a module-level dict (rather than inline magic numbers) so
# tests can refer to a class by what it MEANS -- IFR_CLASS_VALUES[
# ("CIG/VIS", "PCPN")] -- instead of hard-coding 5.
IFR_CLASS_NONE = 0
IFR_CLASS_LABELS = {
    1: ("CIG", None),
    2: ("VIS", "PCPN"),
    3: ("VIS", "FG"),
    4: ("VIS", "BR"),
    5: ("CIG/VIS", "PCPN"),
    6: ("CIG/VIS", "FG"),
    7: ("CIG/VIS", "BR"),
}
IFR_CLASS_VALUES = {label: value for value, label in IFR_CLASS_LABELS.items()}

# A class counts toward a cause/weather tag once it covers this much of
# a region, and a sub-region survives on its own once it covers this
# much of its component. One constant for both because they are the same
# editorial judgement: "is there enough of this here to be worth saying."
INCLUDE_FRACTION = 0.25

# If one class covers at least this much of a component, the component
# is drawn as a single area rather than split into class sub-regions --
# a forecaster wouldn't draw an internal boundary to carve off a
# minority pocket from an otherwise uniform area.
DOMINANT_FRACTION = 0.50

# Contours are traced from a coarsened copy of the region grid: NBM's
# ~2.5km cells produce far more boundary detail than a forecaster draws
# by hand (and than NMAP is comfortable editing), and coarsening the
# RASTER is how v2 gets that without any per-polygon smoothing or
# simplification -- which would break the shared-boundary guarantee.
# 0.1 deg is ~6 nm.
CONTOUR_RESOLUTION_DEG = 0.1

# Adjacent regions deliberately SHARE their boundary vertices exactly.
# If downstream tooling ever turns out to reject coincident vertices,
# setting this to ~0.001 deg shrinks each region by a hairline so the
# shared edge becomes two nearly-identical edges with a sliver between
# them. Deliberately 0.0 (inert) until something proves it's needed --
# a sliver is a real geometry defect of its own, just a less visible one.
ADJACENT_REGION_EROSION_DEG = 0.0

# Mean km per degree of latitude, used for the local-flat-earth cell
# sizing in _cell_dimensions_km() / _cell_areas_sq_mi().
KM_PER_DEG_LAT = 111.32
KM_PER_NM = 1.852
SQ_MI_PER_SQ_KM = 0.386102


def fetch_probability_grid(
    date: datetime, fxx: int, filters: dict, finder=None
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    Fetches one probability field's message from NBM and returns
    (values, native_lats, native_lons) as decoded by cfgrib/eccodes
    from that message's own grid definition.

    finder : callable(rows, **filters) -> dict, optional
        Defaults to pipeline.fetch_nbm.find_message (substring-inclusion
        matching). Overridable so callers needing exclusion logic (e.g.
        pipeline/hazards/mtn_obsc.py isolating NBM's deterministic
        ceiling field from its probability siblings, which share the
        same variable+level and differ only by the ABSENCE of "prob"
        text -- something plain substring-inclusion can't express) can
        plug in their own finder without duplicating this function's
        fetch/parse/xarray-open boilerplate.

    NOTE: imports xarray and pipeline.fetch_nbm locally (not at module
    level) -- see this module's docstring for why that matters for the
    web app's lightweight footprint.
    """
    import xarray as xr

    from pipeline.fetch_nbm import fetch_idx, fetch_message_bytes, find_message, parse_idx, save_message_to_tempfile

    raw_idx, grib_url = fetch_idx(date, fxx)
    rows = parse_idx(raw_idx)
    message = (finder or find_message)(rows, **filters)
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


# lonlat_ring_to_pixel_rowcol() and rasterize_polygon_cells() have moved
# to pipeline/polygons.py (imported above) -- see that module for why.


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
    rr, cc = rasterize_polygon_cells(polygon, grid_spec, ceil_grid.shape)
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
    rr, cc = rasterize_polygon_cells(polygon, grid_spec, vis3_grid.shape)
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

    All three layers are clipped to the ARTCC boundary first, so no
    polygon extends past AWC's area of responsibility even where NBM
    has real coverage out over the Pacific, Canada, or the Atlantic --
    see this module's docstring. The mask is computed here (memoized in
    pipeline.boundaries) rather than baked into the cached grids, so the
    cache format is untouched.

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

    # AWC's area of responsibility, applied to all three layers at once:
    # NBM's CONUS grid extends well past it (open Pacific, Canada, the
    # Atlantic), and without this gate real IFR conditions out there
    # generate real polygons -- the exact same problem MTN OBSC already
    # hit with its generously-sized terrain grid, solved the same way
    # (see CONUS_BOUNDARY_PATH's docstring in pipeline/boundaries.py).
    #
    # Pinned to LAYER_OFF rather than zeroed: zero is a real probability
    # value and would still cross a threshold_pct of 0, whereas the
    # sentinel can't cross any real 0-100 threshold -- so contours land
    # exactly on the boundary, the same way the per-layer exclusions
    # above already work.
    #
    # Computed here at recompute time rather than baked into the cached
    # grids, so the on-disk cache format doesn't change: the first
    # request after a web-process restart pays the rasterization cost
    # (several seconds, see pipeline.polygons.load_boundary_mask), and
    # every subsequent one hits get_boundary_mask's memo -- exactly what
    # MTN OBSC already does.
    inside_artcc = get_boundary_mask(grid_spec, ceil_grid.shape, CONUS_BOUNDARY_PATH)
    outside = ~inside_artcc
    layers = [(name, np.where(outside, LAYER_OFF, grid)) for name, grid in layers]

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


# ---------------------------------------------------------------------------
# LABEL-GRID POLYGONIZATION (v2) -- implementation.
#
# Helpers are private to this module for now. When MTN OBSC moves onto
# the same machinery (a separate change), the genuinely hazard-agnostic
# ones -- the closing, the cell-area math, the majority downsample --
# belong in pipeline/polygons.py, the same way the boundary mask ended
# up in pipeline/boundaries.py once a second hazard needed it.
# ---------------------------------------------------------------------------


def _build_class_grid(
    ceil_grid: np.ndarray,
    vis3_grid: np.ndarray,
    vis1_grid: np.ndarray,
    precip_grid: np.ndarray,
    threshold_pct: float,
) -> np.ndarray:
    """
    Turns the four probability grids into ONE integer class per cell
    (see IFR_CLASS_LABELS). This is where the no-overlap guarantee comes
    from: a cell has exactly one value, so every later step -- closing,
    area filtering, absorption, contouring -- is partitioning ground
    that was already assigned, never re-deciding who owns it.
    """
    cig = ceil_grid >= threshold_pct
    vis = vis3_grid >= threshold_pct
    fog = vis1_grid >= threshold_pct
    pcp = precip_grid >= threshold_pct

    # Weather offset within a cause block: 0=PCPN, 1=FG, 2=BR, matching
    # IFR_CLASS_LABELS' 2/3/4 (VIS) and 5/6/7 (CIG/VIS). PCPN wins over
    # FG where both are true -- the existing convention.
    weather_offset = np.where(pcp, 0, np.where(fog, 1, 2))

    class_grid = np.zeros(ceil_grid.shape, dtype=np.uint8)
    cig_only = cig & ~vis
    vis_only = vis & ~cig
    both = cig & vis
    class_grid[cig_only] = IFR_CLASS_VALUES[("CIG", None)]
    class_grid[vis_only] = IFR_CLASS_VALUES[("VIS", "PCPN")] + weather_offset[vis_only]
    class_grid[both] = IFR_CLASS_VALUES[("CIG/VIS", "PCPN")] + weather_offset[both]
    return class_grid


def _cell_dimensions_km(grid_spec, shape: tuple) -> tuple[float, float]:
    """
    (row spacing, column spacing) in km for one grid cell, for use as
    scipy's `sampling=` so distances come out in real km rather than in
    cells (a cell is not square in ground distance, and gets less square
    the further north you go).

    MID-DOMAIN APPROXIMATION: longitude spacing is evaluated once, at
    the domain's middle latitude, not per row. Over a CONUS-sized domain
    (~25N-50N) a degree of longitude runs from ~101 km to ~72 km, so at
    the edges this over- or under-states the east-west radius by roughly
    10-15%. That is well inside the precision of a forecaster-chosen
    "50 nm" in the first place, and the alternative -- a per-row
    sampling -- isn't something distance_transform_edt supports at all
    (it takes one sampling vector for the whole array). Cell AREAS,
    where the same error would accumulate over thousands of cells rather
    than being a one-off radius fuzz, are computed per row instead --
    see _cell_areas_sq_mi().
    """
    mid_lat = grid_spec.north + (shape[0] - 1) / 2.0 * grid_spec.dy
    dx_km = KM_PER_DEG_LAT * math.cos(math.radians(mid_lat)) * grid_spec.dx
    dy_km = KM_PER_DEG_LAT * abs(grid_spec.dy)
    return dy_km, dx_km


def _cell_areas_sq_mi(grid_spec, shape: tuple) -> np.ndarray:
    """
    Area of one grid cell in square miles, per ROW (shape (rows, 1), so
    it broadcasts across columns) -- cells shrink toward the poles, and
    an area filter that ignored that would be ~30% wrong across a CONUS
    domain's latitude range.

    Deliberately a cos(lat) sum rather than pyproj's geodesic area (what
    pipeline.polygons.geodesic_area_sq_mi uses for v1): here we're
    measuring a set of CELLS, not a ring, and summing per-cell areas is
    both the natural operation and the one that stays consistent when
    the same set is later split between regions.
    """
    lats = grid_spec.north + np.arange(shape[0]) * grid_spec.dy
    width_km = KM_PER_DEG_LAT * np.cos(np.radians(lats)) * grid_spec.dx
    height_km = KM_PER_DEG_LAT * abs(grid_spec.dy)
    return (width_km * height_km * SQ_MI_PER_SQ_KM)[:, None]


def _close_envelope(
    class_grid: np.ndarray, radius_nm: float, sampling: tuple[float, float]
) -> tuple[np.ndarray, np.ndarray]:
    """
    Applies the forecaster's neighborhood radius to the UNION of all
    hazard cells once -- the v2 replacement for running
    merge_nearby_polygons() on each layer separately, which is what let
    one layer's closing re-fill the holes another layer had punched in
    it.

    Returns (closed_mask, closed_class_grid).

    Implemented as two distance transforms (dilate: within radius of a
    hazard cell; erode: further than radius from the dilated set's
    outside) rather than scipy's binary_closing with a structuring
    element. That's not a style preference: at 100 nm on a 0.025 deg
    grid the footprint is ~67 cells across, i.e. a ~14,000-cell disc
    that binary_closing would slide over every cell of a multi-million
    cell grid. The distance transform is O(cells) regardless of radius.

    Cells the closing ADDS inherit their class from the nearest original
    hazard cell -- free from the same call via return_indices, and the
    natural answer to "what is this filled gap made of": whatever it was
    nearest to.
    """
    hazard = class_grid > 0
    if radius_nm <= 0 or not hazard.any():
        return hazard, class_grid

    radius_km = radius_nm * KM_PER_NM
    distance_to_hazard, nearest = distance_transform_edt(
        ~hazard, sampling=sampling, return_indices=True
    )
    dilated = distance_to_hazard <= radius_km

    # Erosion of the dilated set: keep cells further than the radius
    # from anything outside it. Union with the original hazard because
    # a closing must never REMOVE hazard ground -- discretisation at the
    # array border can otherwise erode a cell the dilation never added.
    distance_to_outside = distance_transform_edt(dilated, sampling=sampling)
    closed = (distance_to_outside > radius_km) | hazard

    # nearest[:, r, c] indexes the nearest hazard cell, and is the cell
    # itself where the cell is already hazard -- so this one expression
    # covers both "keep my own class" and "inherit the nearest one".
    closed_class_grid = np.where(closed, class_grid[nearest[0], nearest[1]], IFR_CLASS_NONE)
    return closed, closed_class_grid.astype(np.uint8)


def _absorb_holes(region_ids: np.ndarray, target_id: int, region_mask: np.ndarray) -> list[int]:
    """
    Returns the ids of every region sitting inside `region_mask`'s holes
    (and, as a side effect of the caller filling them, the ids that must
    be merged into target_id).

    THE ENCLOSURE RULE, and why it's a correctness requirement rather
    than cosmetics: a G-AIRMET GFA element is a simple ring and cannot
    represent a hole. If region A wraps around region B, A's own outline
    HAS an interior ring, which either can't be exported or silently
    loses the hole. So B is absorbed into A and A becomes solid.

    Generalised slightly from "fully enclosed by a single other region":
    what actually has to hold is that no region ends up with a hole, and
    a hole shared between two enclosed regions (A wraps B and C
    together) would slip through the single-neighbour test while leaving
    A just as unexportable. Testing A's own holes directly catches both,
    and is the same answer in the single-region case.
    """
    filled = binary_fill_holes(region_mask)
    hole_cells = filled & ~region_mask
    if not hole_cells.any():
        return []
    enclosed = np.unique(region_ids[hole_cells])
    return [int(rid) for rid in enclosed if rid > 0 and rid != target_id]


def _absorb_within_component(
    region_ids: np.ndarray, comp_mask: np.ndarray, cell_areas: np.ndarray
) -> None:
    """
    Absorbs sub-regions inside one component, in place, until stable:

      - any sub-region fully enclosed by another (regardless of size) --
        see _absorb_holes() for why this one is mandatory;
      - any sub-region covering less than INCLUDE_FRACTION of the
        component, into whichever neighbour it shares the most boundary
        with.

    Enclosure runs first each pass: absorbing an enclosed region can
    push its host over the inclusion floor, whereas the reverse ordering
    can dissolve a region that was about to become someone's hole
    filling anyway.
    """
    areas = np.broadcast_to(cell_areas, region_ids.shape)

    while True:
        ids = np.unique(region_ids[comp_mask])
        ids = [int(rid) for rid in ids if rid > 0]
        if len(ids) <= 1:
            return

        masks = {rid: region_ids == rid for rid in ids}
        region_areas = {rid: float(areas[masks[rid]].sum()) for rid in ids}
        component_area = sum(region_areas.values())

        merged = False
        for rid in ids:
            for enclosed_id in _absorb_holes(region_ids, rid, masks[rid]):
                region_ids[masks[enclosed_id]] = rid
                merged = True
            if merged:
                break
        if merged:
            continue

        smallest = min(ids, key=lambda rid: region_areas[rid])
        if region_areas[smallest] / component_area >= INCLUDE_FRACTION:
            return

        neighbour = _largest_shared_boundary_neighbour(region_ids, masks[smallest], comp_mask)
        if neighbour is None:
            return  # nothing to absorb into -- leave it standing rather than dropping it
        region_ids[masks[smallest]] = neighbour


def _largest_shared_boundary_neighbour(
    region_ids: np.ndarray, region_mask: np.ndarray, comp_mask: np.ndarray
) -> int | None:
    """
    The neighbouring region sharing the most boundary cells with this
    one -- "which area is this pocket most part of." Uses 8-connectivity
    to match the component labelling, so a sub-region joined to the rest
    of its component only diagonally still finds a neighbour.
    """
    ring = binary_dilation(region_mask, structure=np.ones((3, 3), dtype=bool)) & ~region_mask & comp_mask
    neighbour_ids = region_ids[ring]
    neighbour_ids = neighbour_ids[neighbour_ids > 0]
    if neighbour_ids.size == 0:
        return None
    counts = np.bincount(neighbour_ids)
    return int(counts.argmax())


def _partition_component(
    class_grid: np.ndarray,
    comp_mask: np.ndarray,
    cell_areas: np.ndarray,
    region_ids: np.ndarray,
    next_region_id: int,
) -> int:
    """
    Turns one connected component into one or more regions, writing
    their ids into `region_ids`. Returns the next free id.

    Either the component is dominated by a single class (>=
    DOMINANT_FRACTION) and is drawn whole, or it splits into class
    sub-regions which then absorb each other per
    _absorb_within_component().

    Sub-regions are the CONNECTED components of each class, not each
    class as a whole: two separate blobs of BR either side of a PCPN
    band are two areas a forecaster would draw separately, and -- more
    to the point -- one region made of two disconnected blobs would
    contour into two rings, which is exactly the thing step 5 asserts
    can't happen. 4-connectivity here (against 8 for the components
    themselves) keeps a region's own outline from pinching through a
    diagonal touch.
    """
    areas = np.broadcast_to(cell_areas, class_grid.shape)
    component_area = float(areas[comp_mask].sum())

    classes, counts = np.unique(class_grid[comp_mask], return_counts=True)
    class_areas = {
        int(cls): float(areas[comp_mask & (class_grid == cls)].sum())
        for cls in classes
        if cls > 0
    }
    if not class_areas:
        return next_region_id

    if max(class_areas.values()) / component_area >= DOMINANT_FRACTION:
        region_ids[comp_mask] = next_region_id
        return next_region_id + 1

    for cls in sorted(class_areas):
        blobs = measure.label(comp_mask & (class_grid == cls), connectivity=1)
        for blob_id in range(1, blobs.max() + 1):
            region_ids[blobs == blob_id] = next_region_id
            next_region_id += 1

    _absorb_within_component(region_ids, comp_mask, cell_areas)
    return next_region_id


def _majority_downsample(region_ids: np.ndarray, factor_y: int, factor_x: int) -> np.ndarray:
    """
    Integer-factor majority reduce: each coarse cell takes the region id
    that covers most of the fine cells under it, so the coarse grid is
    still a partition -- exactly one id per cell, no blending, no
    overlap. Any trailing rows/columns that don't fill a whole block are
    dropped (a partial block at the domain edge, and the domain edge is
    well outside the ARTCC boundary by then).

    Ties go to the lowest-numbered REGION rather than to background, so
    a coarse cell split evenly between hazard and no-hazard stays
    hazard: eroding an area on a coin-flip is worse than including a
    cell's worth of clear air.
    """
    if factor_y == 1 and factor_x == 1:
        return region_ids.copy()

    rows = region_ids.shape[0] // factor_y * factor_y
    cols = region_ids.shape[1] // factor_x * factor_x
    trimmed = region_ids[:rows, :cols]

    blocks = (
        trimmed.reshape(rows // factor_y, factor_y, cols // factor_x, factor_x)
        .transpose(0, 2, 1, 3)
        .reshape(-1, factor_y * factor_x)
    )

    # Background loses ties: park it above every real id while we take
    # the mode (which resolves ties toward the smallest value), then map
    # it back afterwards.
    background = int(region_ids.max()) + 1
    blocks = np.where(blocks == 0, background, blocks)

    ordered = np.sort(blocks, axis=1)
    n_blocks, block_size = ordered.shape
    position = np.arange(block_size)

    starts = np.ones_like(ordered, dtype=bool)
    starts[:, 1:] = ordered[:, 1:] != ordered[:, :-1]
    run_start = np.maximum.accumulate(np.where(starts, position, 0), axis=1)

    ends = np.ones_like(ordered, dtype=bool)
    ends[:, :-1] = ordered[:, :-1] != ordered[:, 1:]
    run_end = np.minimum.accumulate(np.where(ends, position, block_size - 1)[:, ::-1], axis=1)[:, ::-1]

    winner = np.argmax(run_end - run_start, axis=1)
    coarse = ordered[np.arange(n_blocks), winner].reshape(rows // factor_y, cols // factor_x)
    return np.where(coarse == background, 0, coarse)


def _fully_inside_downsample(mask: np.ndarray, factor_y: int, factor_x: int) -> np.ndarray:
    """
    Coarse cells whose fine cells are ALL inside `mask` -- an erosion, not
    a majority vote, and deliberately so.

    Used to re-apply the ARTCC boundary on the coarse grid. A contour runs
    along coarse CELL EDGES, so a coarse cell that is only PARTLY inside
    the boundary puts the part that isn't inside a polygon. Requiring the
    whole block pushes the quantization inward: the polygon edge now sits
    at the edge of a block whose every cell is inside, so it can fall
    short of the boundary by up to a coarse cell -- a forecaster edit --
    rather than claiming airspace this product has no authority over.

    Not provably zero, and worth being precise about: marching squares
    draws its isoline halfway between CELL CENTRES, so at a staircase
    corner it cuts the corner diagonally and a triangular sliver of the
    excluded block (up to half a coarse cell) can still end up inside.
    Measured over the Great Lakes at 100 nm -- the worst concavity in
    CONUS -- that currently comes to 0 outside cells covered, against 129
    with the majority rule alone. (It was 1 until the pixel/lonlat
    convention fix, which stopped the boundary mask itself sitting half a
    cell southeast of the boundary it was built from.)
    """
    if factor_y == 1 and factor_x == 1:
        return mask.copy()
    rows = mask.shape[0] // factor_y * factor_y
    cols = mask.shape[1] // factor_x * factor_x
    return mask[:rows, :cols].reshape(rows // factor_y, factor_y, cols // factor_x, factor_x).all(axis=(1, 3))


def _fill_coarse_holes(coarse_ids: np.ndarray) -> None:
    """
    Re-runs hole absorption on the COARSENED grid, in place, until no
    region has a hole left.

    Necessary because the majority reduce can manufacture a hole that
    didn't exist at full resolution: a block straddling two regions
    resolves to whichever covers more of it, which can leave a single
    coarse cell of B -- or of background -- marooned inside A. Both get
    swallowed by A here, for the same reason the fine-grid absorption
    swallows an enclosed sub-region: a GFA element is a simple ring and
    cannot carry a hole.
    """
    while True:
        merged = False
        for rid in [int(r) for r in np.unique(coarse_ids) if r > 0]:
            mask = coarse_ids == rid
            hole_cells = binary_fill_holes(mask) & ~mask
            if not hole_cells.any():
                continue
            coarse_ids[hole_cells] = rid
            merged = True
            break
        if not merged:
            return


def _coarse_grid_spec(grid_spec, factor_y: int, factor_x: int):
    """
    GridSpec for the coarsened grid. west/north are CELL CENTRES (see
    pipeline/grid_spec.py), and a coarse cell's centre sits at the mean
    of the fine centres it covers -- half a block in from the fine
    grid's own first centre.
    """
    return GridSpec(
        west=grid_spec.west + (factor_x - 1) / 2.0 * grid_spec.dx,
        north=grid_spec.north + (factor_y - 1) / 2.0 * grid_spec.dy,
        dx=grid_spec.dx * factor_x,
        dy=grid_spec.dy * factor_y,
    )


def _region_cause_and_weather(class_cells: np.ndarray, cell_weights: np.ndarray) -> tuple[str, str | None]:
    """
    The label for one region, by the same fractional logic used to
    decide its shape.

      cause   : CIG if cells whose class includes CIG cover at least
                INCLUDE_FRACTION of the region, VIS likewise, both ->
                "CIG/VIS". If neither clears the floor (a region built
                from many small slivers), whichever is larger wins, so
                every region always gets a cause.
      weather : only when VIS is in the cause. PCPN and FG each need
                INCLUDE_FRACTION.

    BR IS ALWAYS INCLUDED whenever visibility crosses anywhere in the
    region, with no fraction test -- THIS IS THE ONE INTERPRETATION IN
    v2 rather than a rule handed down. It follows the AWC convention
    already documented in this module's docstring (BR is a catch-all
    that appears ALONGSIDE more specific descriptors rather than being
    replaced by them), but if that turns out to be wrong for GFA
    elements, this line is the whole change.

    Fractions are measured against the region's classes, not against the
    raw probability grids: cells the closing added inherit a class (see
    _close_envelope) but were never above threshold in any grid, so a
    heavily-closed region measured against the raw grids could report 0%
    of everything and get an arbitrary label.
    """
    total = float(cell_weights.sum())
    if total <= 0:
        return "CIG", None

    def fraction(class_values) -> float:
        return float(cell_weights[np.isin(class_cells, class_values)].sum()) / total

    cig_classes = [v for v, (cause, _w) in IFR_CLASS_LABELS.items() if "CIG" in cause]
    vis_classes = [v for v, (cause, _w) in IFR_CLASS_LABELS.items() if "VIS" in cause]
    pcpn_classes = [v for v, (_c, weather) in IFR_CLASS_LABELS.items() if weather == "PCPN"]
    fog_classes = [v for v, (_c, weather) in IFR_CLASS_LABELS.items() if weather == "FG"]

    cig_fraction = fraction(cig_classes)
    vis_fraction = fraction(vis_classes)

    causes = []
    if cig_fraction >= INCLUDE_FRACTION:
        causes.append("CIG")
    if vis_fraction >= INCLUDE_FRACTION:
        causes.append("VIS")
    if not causes:
        causes = ["CIG"] if cig_fraction >= vis_fraction else ["VIS"]
    cause = "/".join(causes)

    if "VIS" not in cause:
        return cause, None

    weather_parts = []
    if fraction(pcpn_classes) >= INCLUDE_FRACTION:
        weather_parts.append("PCPN")
    if vis_fraction > 0:
        weather_parts.append("BR")  # the always-on catch-all -- see docstring
    if fraction(fog_classes) >= INCLUDE_FRACTION:
        weather_parts.append("FG")

    return cause, "/".join(weather_parts) if weather_parts else None


def _contour_region(region_mask: np.ndarray, coarse_spec) -> list:
    """
    Traces one region's outline and returns it as shapely polygon(s) in
    lon/lat.

    The mask is padded with a ring of background first, so a region
    touching the array edge still closes into a ring instead of coming
    back as an open curve.

    Contour coordinates are array-index space (integer = cell centre),
    so they go through GridSpec.pixel_to_lonlat() -- the shared
    conversion that owns the half-cell offset between that space and
    to_affine()'s corner-based one. v2 got this right from the start by
    doing the arithmetic inline; it now shares the method with v1, which
    did not.
    """
    padded = np.pad(region_mask, 1, mode="constant", constant_values=False)
    contours = measure.find_contours(padded.astype(np.float32), 0.5)

    # THE STRUCTURAL ASSERTION. One region, one ring: absorption has
    # already merged anything enclosed (so no interior rings) and
    # regions are single connected blobs (so no second outline). More
    # than one contour means absorption has a bug, and the user asked
    # for that to be loud rather than for the extra ring to be dropped
    # silently. Raised rather than `assert` so python -O can't strip it.
    if len(contours) != 1:
        raise AssertionError(
            f"region contoured into {len(contours)} rings, expected exactly 1 -- "
            "region absorption (enclosure/inclusion) left an interior ring or a "
            "disconnected region; see _absorb_within_component()"
        )

    # The -1 undoes the padding above; the centre/corner half-cell is
    # GridSpec.pixel_to_lonlat()'s business, not this function's.
    coords = [coarse_spec.pixel_to_lonlat(row - 1, col - 1) for row, col in contours[0]]
    polygon = ShapelyPolygon(coords)
    if not polygon.is_valid:
        # A ring that touches itself at a corner (a region pinched to a
        # single diagonal point) is a valid trace but an invalid ring.
        # buffer(0) splits it into simple parts, which stay disjoint
        # from each other and from every other region.
        polygon = polygon.buffer(0)

    parts = list(polygon.geoms) if polygon.geom_type == "MultiPolygon" else [polygon]
    parts = [p for p in parts if not p.is_empty]

    if ADJACENT_REGION_EROSION_DEG > 0:
        # Inert by default -- see the constant's comment. This is the
        # only vector-space operation on the v2 path, and it exists
        # solely to break the deliberate boundary sharing if some
        # downstream tool demands it.
        eroded = []
        for part in parts:
            shrunk = part.buffer(-ADJACENT_REGION_EROSION_DEG, join_style=2)
            if shrunk.is_empty:
                continue
            eroded.extend(list(shrunk.geoms) if shrunk.geom_type == "MultiPolygon" else [shrunk])
        parts = eroded

    for part in parts:
        if list(part.interiors):
            raise AssertionError(
                "region polygon came out with an interior ring -- absorption should have "
                "made that impossible; see _absorb_holes()"
            )
    return parts


def polygonize_ifr_grid_v2(
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
    THE CHEAP, NBM-INDEPENDENT PHASE, label-grid edition: same inputs,
    same parameters, and same output shape as polygonize_ifr_grid()
    (v1), but with a structural guarantee v1 cannot make -- no two
    polygons overlap, no polygon is nested inside another, and no
    polygon has an interior ring.

    How, in order:
      1. Every cell gets exactly ONE class (cause + primary weather) --
         _build_class_grid(). Overlap is impossible from here on.
      2. The neighborhood radius closes the UNION envelope once --
         _close_envelope() -- not each layer separately, which is what
         made v1 re-fill the holes the visibility mask had punched in
         its ceiling layer. Added cells inherit their nearest
         neighbour's class. The ARTCC mask is re-applied afterwards,
         because a raster closing can push across a concave stretch of
         the boundary.
      3. Connected components, then the area filter applied to WHOLE
         components. A component below min_area_sq_mi goes entirely;
         nothing is ever dropped out of the middle of an area, which is
         v1's other failure mode (a small polygon failing the filter
         inside a larger one leaves a gap).
      4. Within each surviving component, either one dominant class
         takes the whole thing or class sub-regions absorb each other
         until every survivor is worth drawing and none encloses
         another -- _partition_component().
      5. One contour per region, traced from a coarsened copy of the
         region grid. Adjacent regions share boundary vertices exactly,
         because the 0.5 isoline between an A cell and a B cell is the
         same line from either side. NOTHING is smoothed or simplified
         afterwards: per-polygon smoothing moves each polygon's copy of
         a shared edge independently and is precisely what would break
         that. (BOUNDARY_SMOOTHING_DEG and FINAL_SIMPLIFY_TOLERANCE_DEG
         are v1-only for this reason.)

    Parameters are identical to polygonize_ifr_grid()'s -- see there.
    """
    shape = ceil_grid.shape
    cell_areas = _cell_areas_sq_mi(grid_spec, shape)
    sampling = _cell_dimensions_km(grid_spec, shape)

    class_grid = _build_class_grid(ceil_grid, vis3_grid, vis1_grid, precip_grid, threshold_pct)
    closed, class_grid = _close_envelope(class_grid, neighborhood_radius_nm, sampling)

    # See CONUS_BOUNDARY_PATH's docstring in pipeline/boundaries.py.
    # Re-applied AFTER the closing (v1 applies it before, which is all
    # it can do): the closing works on a raster envelope and will
    # happily bridge across a concave stretch of the boundary, e.g. the
    # notch where the ARTCC line follows the Canadian border.
    inside_artcc = get_boundary_mask(grid_spec, shape, CONUS_BOUNDARY_PATH)
    closed &= inside_artcc
    class_grid = np.where(closed, class_grid, IFR_CLASS_NONE).astype(np.uint8)

    # --- components + area filter, applied to whole components ---
    components = measure.label(closed, connectivity=2)
    component_areas = np.bincount(
        components.ravel(), weights=np.broadcast_to(cell_areas, shape).ravel()
    )
    keep = np.zeros(component_areas.shape[0], dtype=bool)
    keep[1:] = component_areas[1:] >= min_area_sq_mi
    kept = keep[components]

    if not kept.any():
        return _ifr_feature_collection(
            [], [], date, fxx, threshold_pct, neighborhood_radius_nm, min_area_sq_mi
        )

    # Dropping small components can leave an enclosed pocket of nothing
    # inside a surviving one; a GFA element can't have a hole, so fill
    # those in and let the fill inherit its nearest neighbour's class,
    # exactly as the closing does.
    filled = binary_fill_holes(kept)
    if (filled & ~kept).any():
        _distance, nearest = distance_transform_edt(~kept, sampling=sampling, return_indices=True)
        class_grid = np.where(filled, class_grid[nearest[0], nearest[1]], IFR_CLASS_NONE).astype(np.uint8)
        kept = filled
    else:
        class_grid = np.where(kept, class_grid, IFR_CLASS_NONE).astype(np.uint8)

    # --- regions within each component ---
    components = measure.label(kept, connectivity=2)
    region_ids = np.zeros(shape, dtype=np.int32)
    next_region_id = 1
    # Each component is partitioned inside its own bounding box: the
    # absorption loop below allocates a mask per sub-region per pass,
    # and doing that at full CONUS size for a component covering one
    # state is most of the cost of this function.
    for comp_id, window in enumerate(find_objects(components), start=1):
        if window is None:
            continue
        next_region_id = _partition_component(
            class_grid[window],
            components[window] == comp_id,
            cell_areas[window[0]],
            region_ids[window],
            next_region_id,
        )

    # --- contour once, from a coarsened copy ---
    factor_y = max(1, int(round(CONTOUR_RESOLUTION_DEG / abs(grid_spec.dy))))
    factor_x = max(1, int(round(CONTOUR_RESOLUTION_DEG / grid_spec.dx)))
    # A grid smaller than one coarse cell would reduce to nothing at
    # all. Only reachable on a toy grid, but "returns no polygons" is a
    # miserable way to find that out, so fall back to contouring at
    # native resolution instead.
    if shape[0] < factor_y or shape[1] < factor_x:
        factor_y = factor_x = 1
    coarse_spec = _coarse_grid_spec(grid_spec, factor_y, factor_x)
    coarse_ids = _majority_downsample(region_ids, factor_y, factor_x)

    # THE ARTCC BOUNDARY, A SECOND TIME -- now on the grid that is
    # actually about to be contoured. The fine-grid application above
    # guarantees no hazard CELL is outside the boundary, but a contour
    # runs along coarse cell EDGES, so a coarse cell straddling the line
    # still drew up to half a coarse cell (~5 km at 0.1 deg) outside it.
    # Dropping any coarse cell not wholly inside pushes that error
    # inward: under-covering our own area of responsibility is a
    # forecaster edit, drawing outside it is a product error. (What
    # remains is a corner sliver -- see _fully_inside_downsample.)
    #
    # Applied BEFORE the split and hole passes below rather than
    # literally last: masking can cut a region in two or notch a hole
    # into it, and those two passes are what keep such a region
    # contourable as a single ring.
    coarse_ids = np.where(
        _fully_inside_downsample(inside_artcc, factor_y, factor_x), coarse_ids, 0
    )

    # Coarsening can pinch a one-cell isthmus, leaving what was one
    # region in two disconnected pieces -- which would contour into two
    # rings. Relabelling connected blobs of equal id makes each piece
    # its own region, which is the honest answer: at the resolution
    # actually being emitted, they ARE two separate areas. (measure.label
    # on an integer array connects neighbours of the same value, so this
    # splits without merging anything that was already distinct.)
    coarse_ids = measure.label(coarse_ids, connectivity=1, background=0)
    _fill_coarse_holes(coarse_ids)

    # Which fine cells sit under each coarse region -- the polygon's
    # footprint is the coarse blob, so what's under THAT footprint is
    # what its cause/weather should describe. Trailing fine rows/columns
    # dropped by the majority reduce stay 0 and take part in nothing.
    fine_region_of = np.zeros(shape, dtype=np.int32)
    covered_rows = coarse_ids.shape[0] * factor_y
    covered_cols = coarse_ids.shape[1] * factor_x
    fine_region_of[:covered_rows, :covered_cols] = np.repeat(
        np.repeat(coarse_ids, factor_y, axis=0), factor_x, axis=1
    )

    polygons = []
    per_polygon_properties = []
    weights = np.broadcast_to(cell_areas, shape)
    for region_id in [int(rid) for rid in np.unique(coarse_ids) if rid > 0]:
        # Only classified cells describe the region; a coarse cell that
        # tipped into the region while holding no hazard of its own
        # shouldn't dilute its label.
        fine_mask = (fine_region_of == region_id) & (class_grid > 0)
        if not fine_mask.any():
            fine_mask = fine_region_of == region_id
        cause, weather_type = _region_cause_and_weather(class_grid[fine_mask], weights[fine_mask])
        properties = {"cause": cause}
        if weather_type:
            properties["weather_type"] = weather_type
        for part in _contour_region(coarse_ids == region_id, coarse_spec):
            polygons.append(part)
            per_polygon_properties.append(dict(properties))

    return _ifr_feature_collection(
        polygons, per_polygon_properties, date, fxx,
        threshold_pct, neighborhood_radius_nm, min_area_sq_mi,
    )


def _ifr_feature_collection(
    polygons: list,
    per_polygon_properties: list,
    date: datetime,
    fxx: int,
    threshold_pct: float,
    neighborhood_radius_nm: float,
    min_area_sq_mi: float,
) -> dict:
    """
    The shared output envelope -- deliberately byte-for-byte the same
    property set v1 emits, so nothing downstream (the web app, the XML
    export, the PGEN export) can tell which implementation drew the
    polygons.
    """
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


def polygonize_ifr_grid_active(
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
    THE entry point every production caller uses (the scheduled
    pipeline, the web app's recompute endpoint, generate_ifr_polygons):
    runs whichever implementation USE_LABEL_GRID_POLYGONIZE selects.

    Both implementations stay reachable by name -- polygonize_ifr_grid()
    for v1, polygonize_ifr_grid_v2() for v2 -- so tests (and the
    comparison in tests/test_ifr_label_grid.py) can pin one deliberately
    instead of depending on how the switch happens to be set.
    """
    implementation = polygonize_ifr_grid_v2 if USE_LABEL_GRID_POLYGONIZE else polygonize_ifr_grid
    return implementation(
        ceil_grid, vis3_grid, vis1_grid, precip_grid, grid_spec, date, fxx,
        threshold_pct=threshold_pct,
        neighborhood_radius_nm=neighborhood_radius_nm,
        min_area_sq_mi=min_area_sq_mi,
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
    wrapper around prepare_ifr_grid() + polygonize_ifr_grid_active(),
    kept for existing callers (pipeline/generate_latest_ifr.py,
    pipeline/test_live_ifr_fetch.py) that just want a one-shot result
    without caring about the two-phase split.
    """
    ceil_grid, vis3_grid, vis1_grid, precip_grid, grid_spec = prepare_ifr_grid(
        date, fxx, target_resolution_deg=target_resolution_deg
    )
    return polygonize_ifr_grid_active(
        ceil_grid, vis3_grid, vis1_grid, precip_grid, grid_spec, date, fxx,
        threshold_pct=threshold_pct,
        neighborhood_radius_nm=neighborhood_radius_nm,
        min_area_sq_mi=min_area_sq_mi,
    )
