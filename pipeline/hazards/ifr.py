"""
pipeline/hazards/ifr.py
------------------------
Real IFR (ceiling/visibility) hazard polygon generation from NBM
probabilistic guidance -- the first hazard-specific module built on top
of the hazard-agnostic pipeline.polygons + pipeline.regrid + pipeline.fetch_nbm.

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
"""

from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np
import xarray as xr

from pipeline.fetch_nbm import fetch_idx, fetch_message_bytes, find_message, parse_idx, save_message_to_tempfile
from pipeline.polygons import grid_to_polygons, polygons_to_feature_collection
from pipeline.regrid import regrid_to_regular_latlon

# Exact filters confirmed against a real NBM inventory (see
# pipeline/inspect_nbm.py's output) -- these substrings must ALL appear
# in a message's raw .idx line for find_message() to select it, and it
# raises if that's not exactly one message, so a subtle NBM format
# change would fail loudly here rather than silently grab the wrong field.
CEILING_PROB_FILTER = {"variable": "CEIL", "level": "cloud ceiling", "extra": "prob <304.8"}
VISIBILITY_PROB_FILTER = {"variable": "VIS", "level": "surface", "extra": "prob <4828.03"}


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
    target_resolution_deg: float = 0.025,
) -> dict:
    """
    Fetches real NBM ceiling + visibility probability data for the
    given model cycle (date) and forecast hour (fxx), combines them,
    and returns a GeoJSON FeatureCollection of IFR hazard polygons.

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

    combined = np.maximum(ceil_regridded, vis_regridded)

    polygons = grid_to_polygons(combined, grid_spec, threshold=threshold_pct)

    valid_time = date + timedelta(hours=fxx)
    return polygons_to_feature_collection(
        polygons,
        properties={
            "hazard": "IFR",
            "threshold_pct": threshold_pct,
            "valid_time": valid_time.isoformat() + "Z",
            "model_cycle": date.isoformat() + "Z",
            "forecast_hour": fxx,
        },
    )
