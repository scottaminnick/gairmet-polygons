"""
pipeline/hazards/tango_common.py
----------------------------------
Shared machinery for the TANGO-category G-AIRMET first-guess layers:
surface wind (NWSI 10-811 section 7.1 "STG SFC WND") and low-level wind
shear ("LLWS POTENTIAL"). pipeline/hazards/sfc_wind.py and
pipeline/hazards/llws.py are thin configs over this module -- everything
that is the same between them lives here once.

MODELLED ON MTN OBSC'S RASTER PATH, NOT IFR'S. Both layers are
deterministic NBM fields thresholded to a yes/no mask, which is MTN
OBSC's shape (mask -> raster closing -> re-gate -> contour), and MTN
OBSC's raster path is the one that already solved "a vector closing can
push a polygon back over ground a gate removed".

NO PROBABILITIES. Both layers read NBM CORE deterministic wind on the
same source cycle as IFR / MTN OBSC (resolve_nbm_cycle; F00..F12 map to
NBM f006..f018 through NBM_LEAD_TIME_OFFSET_HOURS). The probabilistic
(QMD) wind products post 7-8 hours after the cycle, too late for the run
window. See docs/METHODS.md, "TANGO: surface wind and LLWS".

TWO PHASES, same split as every other hazard:

  Phase A -- prepare_tango_grid(): fetch one NBM message, m/s -> knots,
      regrid. Slow, needs the network. NO smoothing here: the cached
      grid is the raw speed so every Phase B parameter stays live.
  Phase B -- polygonize_tango_grid(): the cheap, NBM-independent half.
      Safe to call repeatedly on the cached grid with different
      parameters (the webapp's live recompute).

PHASE B ORDER (each step exists for a reason; do not reorder):
  1. mask = speed_kt >= speed_threshold_kt. ">=" is deliberate: the
     directive says "30 knots or greater".
  2. Smooth the YES/NO MASK, not the speeds, then re-cut at 0.5.
     Smoothing speed values shrank a real LLWS area by 55% at sigma 1.5
     in testing (a ~30 kt-floor sparse field has no shoulders to
     smooth); smoothing the mask shrank it 14%.
  3. Re-apply the gate AFTER smoothing -- smoothing bleeds across the
     ARTCC boundary. ARTCC area of responsibility ONLY: no land mask,
     because these are not terrain hazards (confirmed with a forecaster).
  4. Radius merge on the RASTER (close_mask, gate re-applied), then
     fill_enclosed_gaps, exactly as MTN OBSC.
  5. Contour a real-valued layer (the smoothed mask value, LAYER_OFF
     where gated out) at 0.5, for sub-pixel edges, then area filter,
     boundary smoothing, simplification.
  6. Per-polygon properties, peak_speed_kt taken from the RAW grid.

ALL PARAMETER DEFAULTS ARE UNCALIBRATED PLACEHOLDERS. See each layer's
config and the "uncalibrated" list in docs/METHODS.md.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np
from scipy.ndimage import gaussian_filter

from pipeline.boundaries import CONUS_BOUNDARY_PATH, get_boundary_mask
from pipeline.fetch_terrain import CONUS_BOUNDS, OUTPUT_RESOLUTION_DEG
from pipeline.polygons import (
    cell_areas_sq_mi,
    cell_dimensions_km,
    close_mask,
    fill_enclosed_gaps,
    filter_polygons_by_area,
    geodesic_area_sq_mi,
    grid_to_polygons,
    polygons_to_feature_collection,
    rasterize_polygon_cells,
    smooth_polygon_boundary,
)
from pipeline.regrid import regrid_to_regular_latlon

# NBM wind is m/s. 1 m/s = 1.943844 kt.
MS_TO_KT = 1.943844

# Phase A cache: knots x 2 as uint8 = 0.5 kt resolution, 127.5 kt ceiling.
# See pipeline.polygons.save_grid_cache(scale=...).
CACHE_SCALE = 2.0
CACHE_GRID_KEY = "speed_kt"

# The contour level on the smoothed mask. 0.5 is where a smoothed 0/1
# mask crosses "more cells yes than no".
CONTOUR_LEVEL = 0.5

# What a cell the raster closing / gap fill ADDED is worth in the contour
# layer: fully "in", so the isoline lands at the cell edge instead of
# half-eroding the added area (same trick as MTN OBSC's CLOSING_FILL_PCT).
CLOSING_FILL = 1.0

# Gated-out cells. Below any possible contour level, so the gate is part
# of the surface being contoured rather than a separate masking step.
LAYER_OFF = -1.0

# Same fixed cosmetic parameters as IFR and MTN OBSC.
BOUNDARY_SMOOTHING_DEG = 0.02
FINAL_SIMPLIFY_TOLERANCE_DEG = 0.05


@dataclass(frozen=True)
class TangoLayer:
    """Everything that differs between the Tango layers."""

    key: str  # "sfc_wind" / "llws": filenames, manifest names, env var prefix
    hazard: str  # GeoJSON "hazard" property
    phenomenon: str  # NWSI 10-811 label, e.g. "STG SFC WND"
    idx_filters: dict  # find_message substring filters (ALL must match)
    idx_exclude: tuple  # substrings that disqualify an idx line
    regrid_method: str  # scipy griddata method for Phase A
    default_threshold_kt: float
    threshold_range_kt: tuple  # (min, max) for the live-adjust slider
    default_smooth_sigma_cells: float = 1.5
    default_neighborhood_radius_nm: float = 50.0
    default_min_area_sq_mi: float = 1000.0
    category: str = "TANGO"


SMOOTH_SIGMA_RANGE = (0.0, 3.0)  # step 0.5, 0 = off


def find_layer_message(rows: list[dict], layer: TangoLayer) -> dict:
    """
    Exactly one idx line for this layer, or an error. The tests pin each
    layer's filter to a single line of a saved real idx.

    find_message_excluding is used for BOTH layers (an empty exclude list
    makes it identical to find_message). Besides being one code path, it
    keeps this function free of pipeline.fetch_nbm, which imports
    `requests` -- not in the light dependency set CI's tests run under.
    """
    from pipeline.hazards.mtn_obsc import find_message_excluding

    return find_message_excluding(rows, list(layer.idx_exclude), **layer.idx_filters)


def prepare_tango_grid(
    date: datetime,
    fxx: int,
    layer: TangoLayer,
    target_resolution_deg: float = OUTPUT_RESOLUTION_DEG,
):
    """
    PHASE A for one layer and one NBM forecast hour. Returns
    (speed_kt, grid_spec, matched_idx_line).

    No smoothing, deliberately: the cached grid is the raw speed so the
    smoothing sigma stays adjustable in Phase B. NaN (outside the native
    grid, which only the "linear" method produces) becomes 0 kt.

    sfc_wind regrids "linear" and llws "nearest" -- see each layer's
    config for why. Note "linear" builds a Delaunay triangulation of the
    whole native grid (the 140-198 s/field cost documented in
    pipeline/hazards/mtn_obsc.py).
    """
    from pipeline.hazards.ifr import fetch_probability_grid

    matched: list[str] = []

    def finder(rows, **_filters):
        message = find_layer_message(rows, layer)
        matched.append(message["_raw_line"])
        return message

    values_ms, lats, lons = fetch_probability_grid(date, fxx, layer.idx_filters, finder=finder)
    regridded, grid_spec = regrid_to_regular_latlon(
        values_ms * MS_TO_KT,
        lats,
        lons,
        target_bounds=CONUS_BOUNDS,
        target_resolution_deg=target_resolution_deg,
        method=layer.regrid_method,
    )
    return np.nan_to_num(regridded).astype(np.float32), grid_spec, matched[0]


def polygonize_tango_grid(
    speed_kt: np.ndarray,
    grid_spec,
    layer: TangoLayer,
    date: datetime,
    fxx: int,
    speed_threshold_kt: float | None = None,
    smooth_sigma_cells: float | None = None,
    neighborhood_radius_nm: float | None = None,
    min_area_sq_mi: float | None = None,
) -> dict:
    """
    PHASE B for one layer: a cached raw speed grid (knots) in, a GeoJSON
    FeatureCollection out. Arguments left as None take the layer's
    defaults. See the module docstring for the order of operations.

    Parameters (all forecaster-adjustable, all uncalibrated placeholders)
    ----------
    speed_threshold_kt : cells at or above this are "yes" (>=, not >).
    smooth_sigma_cells : sigma of the Gaussian applied to the YES/NO
        MASK. 0 turns smoothing off.
    neighborhood_radius_nm : raster closing radius.
    min_area_sq_mi : polygons below this geodesic area are dropped, and
        enclosed gaps below it are filled.
    """
    if speed_threshold_kt is None:
        speed_threshold_kt = layer.default_threshold_kt
    if smooth_sigma_cells is None:
        smooth_sigma_cells = layer.default_smooth_sigma_cells
    if neighborhood_radius_nm is None:
        neighborhood_radius_nm = layer.default_neighborhood_radius_nm
    if min_area_sq_mi is None:
        min_area_sq_mi = layer.default_min_area_sq_mi

    shape = speed_kt.shape

    # 1. The threshold. ">=" on purpose: "30 knots or greater".
    mask = speed_kt >= speed_threshold_kt

    # 2. Smooth the mask, not the speeds, and re-cut at 0.5. The real
    # valued result is kept: it is what gets contoured in step 5.
    if smooth_sigma_cells > 0:
        smoothed = gaussian_filter(mask.astype(np.float32), smooth_sigma_cells)
        mask = smoothed >= CONTOUR_LEVEL
    else:
        smoothed = mask.astype(np.float32)

    # 3. Gate AFTER smoothing -- smoothing bleeds across the boundary.
    within_artcc = get_boundary_mask(grid_spec, shape, CONUS_BOUNDARY_PATH)
    mask &= within_artcc

    # 4. The neighborhood radius on the raster, gate re-applied, then the
    # small enclosed gaps the closing could not bridge.
    closed = close_mask(mask, cell_dimensions_km(grid_spec, shape), neighborhood_radius_nm)
    closed &= within_artcc
    closed = fill_enclosed_gaps(closed, cell_areas_sq_mi(grid_spec, shape), min_area_sq_mi)

    # 5. Contour. Real smoothed values where the cell is not gated out;
    # cells the closing added are pinned fully "in"; gated cells off.
    layer_grid = np.where(
        within_artcc,
        np.where(closed & ~mask, CLOSING_FILL, smoothed),
        LAYER_OFF,
    )

    polygons = grid_to_polygons(layer_grid, grid_spec, threshold=CONTOUR_LEVEL, min_area_deg2=0.001)
    polygons = filter_polygons_by_area(polygons, min_area_sq_mi=min_area_sq_mi)
    polygons = [
        smooth_polygon_boundary(p, smoothing_deg=BOUNDARY_SMOOTHING_DEG, join_style=2) for p in polygons
    ]
    polygons = [p.simplify(FINAL_SIMPLIFY_TOLERANCE_DEG, preserve_topology=True) for p in polygons]
    polygons = [p for p in polygons if not p.is_empty]

    # 6. Per-polygon properties. The peak comes from the RAW grid, not
    # the smoothed mask, so it is a real speed.
    per_polygon = []
    for p in polygons:
        rr, cc = rasterize_polygon_cells(p, grid_spec, shape)
        peak = round(float(speed_kt[rr, cc].max()), 1) if len(rr) else None
        per_polygon.append(
            {
                "peak_speed_kt": peak,
                "area_sq_mi": round(geodesic_area_sq_mi(p)),
                "category": layer.category,
                "phenomenon": layer.phenomenon,
            }
        )

    valid_time = date + timedelta(hours=fxx)
    return polygons_to_feature_collection(
        polygons,
        properties={
            "hazard": layer.hazard,
            "speed_threshold_kt": speed_threshold_kt,
            "smooth_sigma_cells": smooth_sigma_cells,
            "neighborhood_radius_nm": neighborhood_radius_nm,
            "min_area_sq_mi": min_area_sq_mi,
            "valid_time": valid_time.isoformat() + "Z",
            "model_cycle": date.isoformat() + "Z",
            "forecast_hour": fxx,
        },
        per_polygon_properties=per_polygon,
    )
