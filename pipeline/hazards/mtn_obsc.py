"""
pipeline/hazards/mtn_obsc.py
------------------------------
Mountain Obscuration (MTN OBSC) polygon generation -- the second SIERRA-
category hazard, built on top of the same hazard-agnostic
pipeline.polygons + pipeline.regrid + pipeline.fetch_nbm stack as IFR,
plus a cached terrain grid from pipeline.fetch_terrain.

NWSI 10-811 Appendix D defines Mountain Obscuration as:
    "Conditions over significant portions of mountainous geographical
    areas are such that pilots in flight should not expect to maintain
    visual meteorological conditions or visual contact with mountains
    or mountain ridges near their route of flight."

THE CORE CONCEPTUAL DIFFERENCE FROM IFR: IFR is a flat AGL threshold
evaluated independently at every grid cell (ceiling < 1000 ft, full
stop, relative to that cell's own elevation). Mountain Obscuration is
RELATIONAL -- a 6,000 ft MSL cloud base is unremarkable VFR ceiling over
a 2,000 ft valley floor, but completely obscures a 9,000 ft ridge a few
miles away. NBM's ceiling probability fields are AGL relative to
whatever the model considers "the surface" AT THAT CELL -- they cannot
see a nearby ridge at all. Getting there needs:
  1. A terrain grid (pipeline.fetch_terrain's cached
     baseline_elevation_ft / ridge_elevation_ft -- NBM has no terrain
     field of its own, confirmed empty via pipeline/inspect_nbm.py
     against a real NBM core-file inventory).
  2. A per-pixel "critical ceiling" derived from that terrain:
         critical_ceiling_agl = ridge_elevation_ft - baseline_elevation_ft
                                 + clearance_margin_ft
     (ridge_elevation_ft is already a radius-based "highest nearby
     peak" search, not the grid cell's own elevation -- see
     pipeline/fetch_terrain.py.)
  3. Interpolating NBM's REAL published ceiling-probability thresholds
     to estimate P(ceiling <= critical_ceiling_agl) at that arbitrary,
     per-pixel-varying height.

REAL NBM FIELDS (confirmed against a real NBM core-file inventory, see
pipeline/inspect_nbm.py's output -- NOT the three flight-category
boundaries originally assumed):
    deterministic ceiling   -> CEIL:cloud ceiling:... (no "prob" text --
                               see find_message_excluding() below for why
                               isolating this needs more than plain
                               substring-inclusion matching)
    ceiling < 500 ft        -> CEIL:cloud ceiling:...:prob <152.4
    ceiling < 1000 ft       -> CEIL:cloud ceiling:...:prob <304.8
    ceiling < 2000 ft       -> CEIL:cloud ceiling:...:prob <609.6
    ceiling < 3000 ft       -> CEIL:cloud ceiling:...:prob <914.5
    ceiling < 6600 ft       -> CEIL:cloud ceiling:...:prob <2011.68
    lowest cloud base       -> CEIL:cloud base:... (deterministic, ANY
                               coverage -- distinct from ceiling, which
                               by definition requires BKN/OVC; NOT
                               currently used to drive polygon
                               generation, see SCOPE NOTE below)
NBM's own "prob fcst N/7" labeling on the five thresholds above implies
7 total bins exist somewhere internally, but only bins 2-6 (of 7) are
exposed in the core file -- 1/7 (below 500 ft) and 7/7 (above 6,600 ft)
are simply not available from this file family. This is why the
interpolation below explicitly refuses to extrapolate past 6,600 ft
rather than projecting a line with no data behind it -- see
interpolate_terrain_relative_probability()'s docstring.

Precipitation and visibility fields (PCPN/BR/FG weather-type
attribution) are the EXACT SAME real NBM fields IFR already confirmed
and uses -- reused directly from pipeline.hazards.ifr rather than
re-confirmed from scratch.

WEATHER-TYPE ATTRIBUTION: NWSI 10-811 Appendix D / section 7.1 item 4
lists the obscuration-cause phenomena as CLDS, PCPN, FU, HZ, and FG --
but real AWC practice ALSO includes BR (confirmed directly, the same
way IFR's BR-is-a-catch-all convention was confirmed) -- the directive's
own list is incomplete here, and this module follows real practice, not
the literal text. FU (smoke) and HZ (haze) are deliberately NOT
automated, for the exact same reason IFR already excludes them: no NBM
field exists for either (confirmed empty in the same real inventory
that confirmed everything else), and per AWC practice these are rare
enough to add manually within NMAP when finalizing a first-guess draft.

So the automatable weather_type tags are: CLDS, PCPN, BR, FG -- and
CLDS is a genuinely NEW case IFR never needed: the DEFAULT/base
attribution whenever terrain-relative ceiling probability crosses
threshold but NEITHER precipitation nor reduced visibility is also
present (i.e. it's just clouds sitting on the ridge, no other weather
phenomenon involved). Every Mountain Obscuration polygon gets a
weather_type -- unlike IFR, where "weather_type" is only set when cause
includes VIS, here it's always present (CLDS at minimum). Precedence
(PCPN wins any overlap with FG, BR is a catch-all never replaced) is the
identical rule already confirmed for IFR -- see
pipeline.hazards.ifr's docstring for the full reasoning; this module
reuses the same precip/vis3/vis1 grids and the same precedence logic,
just adds CLDS as a fourth possible tag.

THE "MOUNTAINOUS AREA" GATE: item 3 says WIDESPREAD mountain
obscuration over MOUNTAINOUS geographical areas -- this hazard
shouldn't paint flat high plains (high absolute elevation, low relief)
the same as real relief. Rather than a new terrain-roughness
computation, this reuses data pipeline.fetch_terrain already derived:
local relief = ridge_elevation_ft - baseline_elevation_ft (both already
loaded from the cached terrain grid). A cell only participates in
Mountain Obscuration polygon generation if this exceeds
MOUNTAINOUS_RELIEF_THRESHOLD_FT.

MOUNTAINOUS_RELIEF_THRESHOLD_FT IS A PLACEHOLDER, exactly like
pipeline.fetch_terrain's TERRAIN_RADIUS_NM -- not yet confirmed against
real output. The legacy NMAP mountain-obscuration shapefile (once
available) is the natural thing to compare against; isolated here in
one constant so it's a one-line change, no re-fetch needed (unlike
TERRAIN_RADIUS_NM, changing this does NOT require re-running
fetch_terrain.py, since it's applied at HAZARD-GENERATION time against
the already-cached ridge/baseline grids, not baked in at fetch time).

CLEARANCE_MARGIN_FT is a genuinely NEW kind of forecaster-adjustable
parameter -- one of five total for this hazard (vs. IFR's three):
threshold_pct and neighborhood_radius_nm/min_area_sq_mi are reused
as-is from IFR; clearance_margin_ft (0/500/1000/2000 ft options) is new
here; terrain_radius_nm is NOT a parameter of this module at all -- it's
baked into the terrain grid at fetch time (see
pipeline/fetch_terrain.py's module docstring for why that one is
different in kind from the other four).

SCOPE NOTE -- what this module does NOT yet do: the deterministic
ceiling and cloud-base fields are fetched and returned by
prepare_mtn_obsc_grid() (so they're available for a future per-pixel
"on click" diagnostic display, and for future refinement), but they do
NOT currently influence polygon generation. In particular, "partial-
cloud intersection" -- a SCATTERED layer sitting right at ridge height,
intermittently obscuring a peak without ever qualifying as a "ceiling"
at all (the "OCNL OBSC" case in real AIRMET text, vs. continuous
obscuration) -- is a real, valuable refinement that this first version
deliberately does not attempt, since cloud base here is a single
deterministic value with no probability distribution to interpolate the
way ceiling has. Flagged explicitly rather than half-modeled.

TWO-PHASE DESIGN, identical split to IFR: prepare_mtn_obsc_grid() is the
expensive, NBM-dependent phase (fetch NINE real NBM fields -- five
ceiling-probability thresholds, deterministic ceiling, cloud base,
precip, and two visibility thresholds -- regrid every one of them onto
the EXACT SAME fixed grid as the cached terrain data, smooth). This is a
real, expected cost difference from IFR's four fields, not an
inefficiency to fix. polygonize_mtn_obsc_grid() is the cheap,
NBM-independent phase (terrain-relative interpolation + threshold +
merge + area filter + boundary smoothing + attribution).

CRITICAL GRID-ALIGNMENT REQUIREMENT: every regrid_to_regular_latlon()
call in prepare_mtn_obsc_grid() passes target_bounds=CONUS_BOUNDS and
target_resolution_deg=OUTPUT_RESOLUTION_DEG -- imported directly from
pipeline.fetch_terrain, NOT redefined here (see that module's docstring
for why: regrid_to_regular_latlon() otherwise derives bounds
dynamically from each NBM message's own native extent, which would NOT
necessarily match the terrain grid's fixed box, silently misaligning
every downstream cell-by-cell computation). prepare_mtn_obsc_grid()
asserts this alignment explicitly rather than trusting it silently.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from functools import partial

import numpy as np

from pipeline.fetch_terrain import CONUS_BOUNDS, OUTPUT_RESOLUTION_DEG, load_terrain_grid
from pipeline.hazards.ifr import (
    PRECIP_PROB_THRESHOLD,
    VISIBILITY_1SM_PROB_FILTER,
    VISIBILITY_PROB_FILTER,
    fetch_precip_probability_grid,
    fetch_probability_grid,
)
from pipeline.polygons import (
    GridSpec,
    filter_polygons_by_area,
    grid_to_polygons,
    merge_nearby_polygons,
    polygons_to_feature_collection,
    rasterize_polygon_cells,
    smooth_polygon_boundary,
)
from pipeline.regrid import regrid_to_regular_latlon
from pipeline.smoothing import gaussian_smooth

# ---------------------------------------------------------------------------
# Real NBM field filters, confirmed against a real NBM core-file inventory
# (see pipeline/inspect_nbm.py's output) -- module docstring has the full
# list with context.
# ---------------------------------------------------------------------------

CEILING_PROB_THRESHOLDS_FT = [500, 1000, 2000, 3000, 6600]

CEILING_PROB_FILTERS = {
    500: {"variable": "CEIL", "level": "cloud ceiling", "extra": "prob <152.4"},
    1000: {"variable": "CEIL", "level": "cloud ceiling", "extra": "prob <304.8"},
    2000: {"variable": "CEIL", "level": "cloud ceiling", "extra": "prob <609.6"},
    3000: {"variable": "CEIL", "level": "cloud ceiling", "extra": "prob <914.5"},
    6600: {"variable": "CEIL", "level": "cloud ceiling", "extra": "prob <2011.68"},
}

# Deterministic ceiling shares variable+level with all five probability
# rows above -- the ONLY thing distinguishing it is the ABSENCE of "prob"
# text, which find_message()'s plain substring-inclusion can't express.
# See find_message_excluding() below.
DETERMINISTIC_CEILING_FILTER = {"variable": "CEIL", "level": "cloud ceiling"}
DETERMINISTIC_CEILING_EXCLUDE = ["prob"]

# Unlike "cloud ceiling", "cloud base" has exactly one row in the real
# inventory (no probability siblings to distinguish from), so plain
# find_message() via fetch_probability_grid works fine here -- no
# exclusion needed.
CLOUD_BASE_FILTER = {"variable": "CEIL", "level": "cloud base"}

# Forecaster-adjustable clearance margin options (see module docstring).
# 500 ft is the starting default -- a middle-of-the-road choice, not yet
# confirmed against real forecaster preference.
CLEARANCE_MARGIN_OPTIONS_FT = [0, 500, 1000, 2000]
DEFAULT_CLEARANCE_MARGIN_FT = 500.0

# PLACEHOLDER -- see module docstring's "mountainous area gate" section.
MOUNTAINOUS_RELIEF_THRESHOLD_FT = 500.0

# Same fixed cosmetic parameters as IFR, reused for visual consistency
# across both SIERRA-category hazards -- see pipeline/hazards/ifr.py's
# docstring for why these specific values.
GAUSSIAN_SIGMA_CELLS = 0.6
BOUNDARY_SMOOTHING_DEG = 0.02
FINAL_SIMPLIFY_TOLERANCE_DEG = 0.05

# Same sentinel-pinning trick as IFR's LAYER_OFF -- guaranteed to never
# cross any real 0-100 threshold_pct value, so non-mountainous cells are
# cleanly excluded from polygon generation without a separate mask step.
LAYER_OFF = -1.0

# The free (0 ft, 0%) anchor plus the five real published thresholds, in
# ascending order -- see interpolate_terrain_relative_probability().
THRESHOLD_ANCHORS_FT = np.array([0.0] + CEILING_PROB_THRESHOLDS_FT, dtype=np.float64)


def find_message_excluding(rows: list[dict], exclude: list[str], **include_filters: str) -> dict:
    """
    Like pipeline.fetch_nbm.find_message() (substring-inclusion,
    case-insensitive, raises unless exactly one match), but ALSO
    excludes any row whose raw .idx line contains any of the given
    exclude substrings. Needed because find_message() only supports
    "must contain" -- NBM's deterministic ceiling field shares its
    variable+level with its five probability siblings, and the ONLY
    thing distinguishing the deterministic row is the ABSENCE of "prob"
    text, which plain substring-inclusion can't express.
    """
    matches = [
        r
        for r in rows
        if all(v.lower() in r["_raw_line"].lower() for v in include_filters.values())
        and not any(x.lower() in r["_raw_line"].lower() for x in exclude)
    ]
    if len(matches) == 0:
        raise ValueError(f"No message matched: include={include_filters}, exclude={exclude}")
    if len(matches) > 1:
        lines = "; ".join(m["_raw_line"] for m in matches)
        raise ValueError(
            f"Ambiguous: {len(matches)} messages matched include={include_filters}, "
            f"exclude={exclude}: {lines}"
        )
    return matches[0]


def fetch_deterministic_ceiling_grid(date: datetime, fxx: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Fetches NBM's deterministic (non-probabilistic) ceiling field."""
    finder = partial(find_message_excluding, exclude=DETERMINISTIC_CEILING_EXCLUDE)
    return fetch_probability_grid(date, fxx, DETERMINISTIC_CEILING_FILTER, finder=finder)


def fetch_cloud_base_grid(date: datetime, fxx: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Fetches NBM's lowest-cloud-base field (any coverage, not just ceiling)."""
    return fetch_probability_grid(date, fxx, CLOUD_BASE_FILTER)


def fetch_ceiling_prob_grid(date: datetime, fxx: int, threshold_ft: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Fetches one of the five real ceiling-probability threshold fields."""
    return fetch_probability_grid(date, fxx, CEILING_PROB_FILTERS[threshold_ft])


def interpolate_terrain_relative_probability(
    critical_ceiling_agl: np.ndarray,
    prob_grids_by_threshold: dict[int, np.ndarray],
) -> tuple[np.ndarray, np.ndarray]:
    """
    Core terrain-relative math: for each grid cell, linearly
    interpolates NBM's real published ceiling-probability thresholds to
    estimate P(ceiling <= critical_ceiling_agl) -- a per-cell-varying
    height, not a fixed threshold the way IFR's single 1000 ft check is.

    Worked-example check (confirmed against a hand-computed example
    during design): critical_ceiling_agl=2750 ft, bracketed by the real
    2000 ft and 3000 ft thresholds (weight=0.75 of the way from 2000 to
    3000) -- NOT bracketed by 1000/3000 as an earlier hand-prototype
    assumed, back when only 3 thresholds were assumed to exist. Now that
    the real published set includes 2000 ft, this interpolation is more
    accurate than that original prototype, not just differently
    structured.

    IMPORTANT CAUTION (explicitly preserved, not smoothed over): this
    interpolation should NOT be oversold as a fully calibrated
    probability at arbitrary heights -- it is a reasonable estimate
    BRACKETED BY two real published values, and callers needing to show
    "where the number came from" (e.g. a future per-pixel click
    diagnostic) should surface which two real thresholds bracketed a
    given cell's estimate, not just the interpolated number alone.

    Extrapolation: for critical_ceiling_agl below the lowest anchor (0
    ft -- a free anchor, not a published NBM value, since ceiling can
    never be below the ground) or above the highest published threshold
    (6,600 ft), this does NOT project a line beyond the real data --
    NBM's own "prob fcst N/7" labeling implies thresholds exist beyond
    what the core file exposes (bins 1/7 and 7/7), so there's a real gap
    in available data, not just an arbitrary cutoff. Above 6,600 ft, the
    6,600 ft threshold's own probability is reported as a floor/
    lower-bound estimate instead, and the SECOND return value flags
    exactly which cells that applies to, so downstream code (or a
    future diagnostic display) can mark those as lower-confidence.

    Returns
    -------
    (derived_probability, is_extrapolated) : both 2D arrays matching
        critical_ceiling_agl's shape. is_extrapolated is True wherever
        critical_ceiling_agl exceeded the highest real published
        threshold (6,600 ft) -- NOT a hard error, just a confidence flag.
    """
    shape = critical_ceiling_agl.shape

    # index 0 = the free (0 ft, 0%) anchor; indices 1-5 = the five real
    # published thresholds, in ascending order (must match
    # THRESHOLD_ANCHORS_FT's construction: [0.0] + CEILING_PROB_THRESHOLDS_FT).
    stack = np.stack(
        [np.zeros(shape, dtype=np.float64)]
        + [prob_grids_by_threshold[t].astype(np.float64) for t in CEILING_PROB_THRESHOLDS_FT],
        axis=0,
    )

    x = np.clip(critical_ceiling_agl, THRESHOLD_ANCHORS_FT[0], THRESHOLD_ANCHORS_FT[-1])
    # searchsorted gives, per cell, the index of the smallest anchor
    # STRICTLY GREATER than x (side="right" so an exact anchor match
    # resolves to the bracket ABOVE it, not a zero-width one); clipped so
    # the highest possible x (== the last anchor, after the clip above)
    # still resolves to a valid [lo, hi) bracket rather than indexing past
    # the end of the anchor array.
    idx_hi = np.clip(np.searchsorted(THRESHOLD_ANCHORS_FT, x, side="right"), 1, len(THRESHOLD_ANCHORS_FT) - 1)
    idx_lo = idx_hi - 1

    x_lo = THRESHOLD_ANCHORS_FT[idx_lo]
    x_hi = THRESHOLD_ANCHORS_FT[idx_hi]
    weight = np.where(x_hi > x_lo, (x - x_lo) / np.where(x_hi > x_lo, x_hi - x_lo, 1.0), 0.0)

    row_idx, col_idx = np.indices(shape)
    y_lo = stack[idx_lo, row_idx, col_idx]
    y_hi = stack[idx_hi, row_idx, col_idx]
    derived = y_lo + weight * (y_hi - y_lo)

    is_extrapolated = critical_ceiling_agl > THRESHOLD_ANCHORS_FT[-1]
    # For extrapolated cells, report the highest real threshold's OWN
    # probability rather than a fabricated value with no data behind it
    # -- see docstring's caution above.
    derived = np.where(is_extrapolated, stack[-1], derived)

    return derived, is_extrapolated


def prepare_mtn_obsc_grid(
    date: datetime,
    fxx: int,
    terrain_grid_path: str = "data/terrain/terrain_grid.npz",
    target_resolution_deg: float = OUTPUT_RESOLUTION_DEG,
):
    """
    THE EXPENSIVE, NBM-DEPENDENT PHASE: fetches all nine real NBM fields
    (five ceiling-probability thresholds, deterministic ceiling, cloud
    base, precip, and two visibility thresholds), regrids every one onto
    the EXACT SAME fixed grid as the cached terrain data (see module
    docstring's "CRITICAL GRID-ALIGNMENT REQUIREMENT"), smooths, and
    loads the cached terrain grid -- asserting its GridSpec matches
    rather than trusting it silently.

    Returns a dict (deliberately not a long positional tuple -- nine
    NBM fields plus two terrain fields is too many positions to keep
    straight by memory, unlike IFR's four) with keys: "ceiling_prob"
    (dict keyed by threshold_ft -> grid), "deterministic_ceiling",
    "cloud_base", "precip", "vis3", "vis1", "baseline_elevation_ft",
    "ridge_elevation_ft", "grid_spec", "terrain_radius_nm" (informational
    -- whatever radius was baked into the loaded terrain grid at fetch
    time, NOT a parameter of this function).
    """
    target_bounds = CONUS_BOUNDS  # see module docstring -- must match fetch_terrain.py exactly

    def _fetch_and_regrid(values_lats_lons):
        values, lats, lons = values_lats_lons
        regridded, gs = regrid_to_regular_latlon(
            values, lats, lons, target_bounds=target_bounds, target_resolution_deg=target_resolution_deg
        )
        return gaussian_smooth(np.nan_to_num(regridded), sigma_cells=GAUSSIAN_SIGMA_CELLS), gs

    ceiling_prob_grids = {}
    grid_spec = None
    for threshold_ft in CEILING_PROB_THRESHOLDS_FT:
        grid, grid_spec = _fetch_and_regrid(fetch_ceiling_prob_grid(date, fxx, threshold_ft))
        ceiling_prob_grids[threshold_ft] = grid

    deterministic_ceiling_grid, _ = _fetch_and_regrid(fetch_deterministic_ceiling_grid(date, fxx))
    cloud_base_grid, _ = _fetch_and_regrid(fetch_cloud_base_grid(date, fxx))
    precip_grid, _ = _fetch_and_regrid(fetch_precip_probability_grid(date, fxx))
    vis3_grid, _ = _fetch_and_regrid(fetch_probability_grid(date, fxx, VISIBILITY_PROB_FILTER))
    vis1_grid, _ = _fetch_and_regrid(fetch_probability_grid(date, fxx, VISIBILITY_1SM_PROB_FILTER))

    terrain_grids, terrain_grid_spec, terrain_radius_nm = load_terrain_grid(terrain_grid_path)

    # Explicit alignment check rather than trusting it silently -- if
    # these ever drift apart (e.g. someone changes OUTPUT_RESOLUTION_DEG
    # in one place and not the other), every downstream cell-by-cell
    # computation would silently misalign without this.
    if (
        abs(terrain_grid_spec.west - grid_spec.west) > 1e-9
        or abs(terrain_grid_spec.north - grid_spec.north) > 1e-9
        or abs(terrain_grid_spec.dx - grid_spec.dx) > 1e-9
        or abs(terrain_grid_spec.dy - grid_spec.dy) > 1e-9
    ):
        raise ValueError(
            f"Terrain grid ({terrain_grid_spec}) does not match the regridded NBM "
            f"grid ({grid_spec}) -- these must be identical. Check that "
            f"fetch_terrain.py's CONUS_BOUNDS/OUTPUT_RESOLUTION_DEG haven't "
            f"drifted apart from what this function passes to regrid_to_regular_latlon()."
        )

    return {
        "ceiling_prob": ceiling_prob_grids,
        "deterministic_ceiling": deterministic_ceiling_grid,
        "cloud_base": cloud_base_grid,
        "precip": precip_grid,
        "vis3": vis3_grid,
        "vis1": vis1_grid,
        "baseline_elevation_ft": terrain_grids["baseline_elevation_ft"],
        "ridge_elevation_ft": terrain_grids["ridge_elevation_ft"],
        "grid_spec": grid_spec,
        "terrain_radius_nm": terrain_radius_nm,
    }


def _determine_weather_type(
    polygon,
    grid_spec: GridSpec,
    precip_grid: np.ndarray,
    vis3_grid: np.ndarray,
    vis1_grid: np.ndarray,
    threshold_pct: float,
) -> str:
    """
    Determines weather_type for a Mountain Obscuration polygon. Same
    PCPN/BR/FG precedence rule already confirmed for IFR (PCPN wins any
    overlap with FG; BR is a catch-all never replaced -- see
    pipeline.hazards.ifr's docstring), PLUS "CLDS" as the default/base
    tag whenever none of PCPN/BR/FG apply -- see module docstring for
    why CLDS is a genuinely new case Mountain Obscuration needs that IFR
    never did. Unlike IFR's weather_type (only set when cause includes
    VIS), this ALWAYS returns a non-empty label -- every Mountain
    Obscuration polygon has at least "plain clouds against terrain" as
    its baseline cause.
    """
    rr, cc = rasterize_polygon_cells(polygon, grid_spec, vis3_grid.shape)
    if len(rr) == 0:
        return "CLDS"

    precip_here = precip_grid[rr, cc] >= threshold_pct
    vis3_here = vis3_grid[rr, cc] >= threshold_pct
    vis1_here = vis1_grid[rr, cc] >= threshold_pct

    labels = []
    if precip_here.any():
        labels.append("PCPN")
    if vis3_here.any():
        labels.append("BR")
    if (vis1_here & ~precip_here).any():  # PCPN wins the overlap -- see ifr.py's docstring
        labels.append("FG")

    return "/".join(labels) if labels else "CLDS"


def polygonize_mtn_obsc_grid(
    ceiling_prob_grids: dict[int, np.ndarray],
    precip_grid: np.ndarray,
    vis3_grid: np.ndarray,
    vis1_grid: np.ndarray,
    baseline_elevation_ft: np.ndarray,
    ridge_elevation_ft: np.ndarray,
    grid_spec,
    date: datetime,
    fxx: int,
    threshold_pct: float = 50.0,
    clearance_margin_ft: float = DEFAULT_CLEARANCE_MARGIN_FT,
    neighborhood_radius_nm: float = 50.0,
    min_area_sq_mi: float = 3000.0,
    terrain_radius_nm: float | None = None,
) -> dict:
    """
    THE CHEAP, NBM-INDEPENDENT PHASE: given already-prepared ceiling-
    probability grids and the cached terrain grids (see
    prepare_mtn_obsc_grid()), applies the forecaster-adjustable
    parameters and returns a GeoJSON FeatureCollection.

    Safe to call repeatedly against the SAME cached grids with different
    parameter values -- same "no NBM access, just numpy/shapely/scipy
    math" property as IFR's polygonize_ifr_grid(), and for the same
    reason (the web app's live parameter-adjustment endpoint).

    Parameters
    ----------
    threshold_pct : float
        Probability (0-100) above which a cell counts as "hazard
        present." Forecaster-adjustable, same meaning as IFR's.
    clearance_margin_ft : float
        One of CLEARANCE_MARGIN_OPTIONS_FT (0/500/1000/2000 ft) --
        added to the real terrain rise before interpolating probability.
        NEW parameter, not present for IFR.
    neighborhood_radius_nm, min_area_sq_mi : float
        Reused as-is from IFR -- same meaning, same defaults. NOT the
        same thing as terrain_radius_nm (see module docstring).
    terrain_radius_nm : float, optional
        INFORMATIONAL ONLY -- whatever radius was baked into the loaded
        terrain grid at fetch time (see prepare_mtn_obsc_grid()'s
        return). Included in output properties so it's visible which
        terrain-grid vintage produced a given result; does NOT affect
        this function's own math at all.

    Returns
    -------
    dict (GeoJSON FeatureCollection)
    """
    terrain_relief_ft = ridge_elevation_ft - baseline_elevation_ft
    mountainous_mask = terrain_relief_ft >= MOUNTAINOUS_RELIEF_THRESHOLD_FT

    critical_ceiling_agl = terrain_relief_ft + clearance_margin_ft
    derived_probability, _is_extrapolated = interpolate_terrain_relative_probability(
        critical_ceiling_agl, ceiling_prob_grids
    )

    # Same sentinel-pinning trick as IFR's layer exclusion -- cells
    # outside the mountainous gate can never cross any real threshold_pct.
    layer_grid = np.where(mountainous_mask, derived_probability, LAYER_OFF)

    polygons = grid_to_polygons(layer_grid, grid_spec, threshold=threshold_pct, min_area_deg2=0.001)
    polygons = merge_nearby_polygons(polygons, radius_nm=neighborhood_radius_nm)
    polygons = filter_polygons_by_area(polygons, min_area_sq_mi=min_area_sq_mi)
    polygons = [
        smooth_polygon_boundary(p, smoothing_deg=BOUNDARY_SMOOTHING_DEG, join_style=2) for p in polygons
    ]
    polygons = [p.simplify(FINAL_SIMPLIFY_TOLERANCE_DEG, preserve_topology=True) for p in polygons]
    polygons = [p for p in polygons if not p.is_empty]

    all_per_polygon_properties = []
    for p in polygons:
        weather_type = _determine_weather_type(p, grid_spec, precip_grid, vis3_grid, vis1_grid, threshold_pct)
        all_per_polygon_properties.append({"weather_type": weather_type})

    valid_time = date + timedelta(hours=fxx)
    return polygons_to_feature_collection(
        polygons,
        properties={
            "hazard": "MTN_OBSC",
            "threshold_pct": threshold_pct,
            "clearance_margin_ft": clearance_margin_ft,
            "neighborhood_radius_nm": neighborhood_radius_nm,
            "min_area_sq_mi": min_area_sq_mi,
            "terrain_radius_nm": terrain_radius_nm,
            "valid_time": valid_time.isoformat() + "Z",
            "model_cycle": date.isoformat() + "Z",
            "forecast_hour": fxx,
        },
        per_polygon_properties=all_per_polygon_properties,
    )


def generate_mtn_obsc_polygons(
    date: datetime,
    fxx: int,
    threshold_pct: float = 50.0,
    clearance_margin_ft: float = DEFAULT_CLEARANCE_MARGIN_FT,
    neighborhood_radius_nm: float = 50.0,
    min_area_sq_mi: float = 3000.0,
    terrain_grid_path: str = "data/terrain/terrain_grid.npz",
    target_resolution_deg: float = OUTPUT_RESOLUTION_DEG,
) -> dict:
    """
    Full pipeline in one call: fetch + prepare + polygonize. Thin
    wrapper, same role as IFR's generate_ifr_polygons(), for callers
    that just want a one-shot result without caring about the two-phase
    split.
    """
    prepared = prepare_mtn_obsc_grid(date, fxx, terrain_grid_path=terrain_grid_path, target_resolution_deg=target_resolution_deg)
    return polygonize_mtn_obsc_grid(
        prepared["ceiling_prob"],
        prepared["precip"],
        prepared["vis3"],
        prepared["vis1"],
        prepared["baseline_elevation_ft"],
        prepared["ridge_elevation_ft"],
        prepared["grid_spec"],
        date,
        fxx,
        threshold_pct=threshold_pct,
        clearance_margin_ft=clearance_margin_ft,
        neighborhood_radius_nm=neighborhood_radius_nm,
        min_area_sq_mi=min_area_sq_mi,
        terrain_radius_nm=prepared["terrain_radius_nm"],
    )
