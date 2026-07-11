"""
pipeline/smoothing.py
-----------------------
Hazard-agnostic smoothing/generalization utilities. Like polygons.py,
this module doesn't know anything about NBM or aviation weather -- it
just operates on "a grid of numbers," which keeps it reusable across
every current and future hazard.

Two techniques live here:

1. neighborhood_max_smooth() -- "neighborhood probability" smoothing.
   Replaces each grid cell's value with the MAXIMUM value found within
   a real-world radius around it. This is what pulls small, isolated
   hazard areas into nearby larger ones: a 60%-probability speck sitting
   near a 90%-probability region inherits that 90%, crosses threshold,
   and merges into the same polygon instead of drawing as its own tiny
   separate shape.

2. gaussian_smooth() -- simple spatial smoothing to round off small-
   scale noise BEFORE contouring, so polygon boundaries come out looking
   hand-drawn rather than pixel-jagged.

Both work on a REGULAR lon/lat grid (i.e. after pipeline.regrid has
already resampled NBM's native curvilinear grid) -- see the note in
neighborhood_max_smooth() about the one real accuracy limitation this
implies.
"""

from __future__ import annotations

import numpy as np
from scipy.ndimage import gaussian_filter, maximum_filter

from pipeline.polygons import GridSpec

KM_PER_DEGREE_LAT = 111.32  # ~constant; a degree of latitude is always about this many km
NM_TO_KM = 1.852


def neighborhood_max_smooth(values: np.ndarray, grid_spec: GridSpec, radius_nm: float) -> np.ndarray:
    """
    Replaces each cell with the max value found within radius_nm
    nautical miles, in real-world distance.

    ACCURACY NOTE: a degree of longitude covers a different real
    distance depending on latitude (shrinking toward the poles), but
    scipy's maximum_filter needs ONE fixed footprint shape for the
    whole grid. We correct for this using the grid's MEAN latitude,
    which is accurate at the center of a CONUS-scale domain and
    increasingly approximate toward its northern/southern edges (a
    ~25 deg latitude domain like CONUS has roughly +/-15% distortion
    at the extremes relative to the center). Good enough for a
    stylistic generalization step; would need a proper per-row
    correction (or doing this in a projected space) if more precision
    ever matters here.

    Parameters
    ----------
    values : 2D array
        Regular lon/lat grid (e.g. from pipeline.regrid).
    grid_spec : GridSpec
        Describes the grid's lon/lat spacing.
    radius_nm : float
        Real-world radius, in nautical miles. 0 or negative = no-op.

    Returns
    -------
    2D array, same shape as input.
    """
    if radius_nm <= 0:
        return values

    radius_km = radius_nm * NM_TO_KM

    mean_lat = grid_spec.north + (values.shape[0] * grid_spec.dy) / 2  # dy is negative, moving south
    km_per_degree_lon = KM_PER_DEGREE_LAT * np.cos(np.radians(mean_lat))
    if km_per_degree_lon < 1e-6:
        km_per_degree_lon = 1e-6  # guard against a pathological near-pole grid

    radius_rows = radius_km / KM_PER_DEGREE_LAT / abs(grid_spec.dy)
    radius_cols = radius_km / km_per_degree_lon / abs(grid_spec.dx)

    ry = max(int(round(radius_rows)), 1)
    rx = max(int(round(radius_cols)), 1)

    # Build a real (roughly circular, in true distance) disk-shaped
    # footprint, rather than scipy's default rectangular one -- a
    # rectangular footprint would over-merge diagonally.
    y, x = np.ogrid[-ry : ry + 1, -rx : rx + 1]
    footprint = (y / ry) ** 2 + (x / rx) ** 2 <= 1.0

    return maximum_filter(values, footprint=footprint, mode="nearest")


def gaussian_smooth(values: np.ndarray, sigma_cells: float) -> np.ndarray:
    """
    Thin wrapper around scipy's Gaussian filter -- rounds off small-
    scale noise in the probability field before contouring, so polygon
    boundaries look smoother/hand-drawn rather than pixel-jagged.

    sigma_cells is in GRID CELLS, not real distance (unlike
    neighborhood_max_smooth) -- this is deliberately a cheap cosmetic
    pass, not something we need real-world-distance precision for.
    0 or negative = no-op.
    """
    if sigma_cells <= 0:
        return values
    return gaussian_filter(values, sigma=sigma_cells, mode="nearest")
