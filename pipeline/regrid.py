"""
pipeline/regrid.py
-------------------
NBM's native CONUS grid is projected (Lambert Conformal Conic), not a
plain lon/lat grid -- pipeline/polygons.py assumes a REGULAR lon/lat
grid (see the NOTE at the bottom of that file), so something has to sit
between "raw NBM data" and "polygons.py".

This module resamples a curvilinear (projected) field onto a new,
regular lon/lat grid using standard scattered-data interpolation.

DELIBERATE DESIGN CHOICE: we do not hardcode NBM's specific projection
parameters (standard parallels, central meridian, earth radius) anywhere
in this codebase. When you open a fetched GRIB2 message with
cfgrib/xarray, it decodes the message's own embedded grid definition and
gives you back real per-cell latitude/longitude arrays automatically --
this is a mature, long-established eccodes feature, not something we're
implementing ourselves. Hand-copying projection numbers from
documentation risks silently mislocating every polygon if we got a
parameter slightly wrong or if NOAA ever changes the grid; reading them
from the data itself can't be wrong in that way.

Typical usage (once pipeline/fetch_nbm.py + cfgrib give you an
xarray DataArray for a message):

    da = xr.open_dataset("ceiling_prob.grib2", engine="cfgrib")["unknown"]
    regridded, grid_spec = regrid_to_regular_latlon(
        da.values, da.latitude.values, da.longitude.values
    )
    polygons = grid_to_polygons(regridded, grid_spec, threshold=50.0)
"""

from __future__ import annotations

import numpy as np
from scipy.interpolate import griddata

from pipeline.polygons import GridSpec


def regrid_to_regular_latlon(
    values: np.ndarray,
    native_lats: np.ndarray,
    native_lons: np.ndarray,
    target_resolution_deg: float = 0.025,
    target_bounds: tuple[float, float, float, float] | None = None,
    method: str = "linear",
) -> tuple[np.ndarray, GridSpec]:
    """
    Resample a 2D field on a curvilinear grid onto a new, regular
    lon/lat grid.

    Parameters
    ----------
    values : 2D array, shape matching native_lats/native_lons
        The data to resample (e.g. probability of IFR ceiling, 0-100).
    native_lats, native_lons : 2D arrays, same shape as values
        Per-grid-cell latitude/longitude, as decoded by cfgrib/eccodes
        from the source GRIB2 message's own grid definition.
    target_resolution_deg : float
        Resolution of the new regular grid, in degrees. Default ~0.025
        deg is roughly comparable to NBM's native ~2.5km resolution at
        mid-latitudes.
    target_bounds : (west, south, east, north), optional
        Bounding box for the new grid. If None, uses the native data's
        own lat/lon extent, trimmed by a small margin to avoid
        extrapolation artifacts right at the native grid's ragged edge
        (a curvilinear grid isn't a rectangle in lon/lat space).
    method : str
        Passed to scipy.interpolate.griddata: 'linear', 'nearest', or
        'cubic'. 'linear' is a reasonable default for smooth
        probability fields; 'nearest' avoids ever inventing values
        between a 0 and a 100 cell but produces blockier polygons.

    Returns
    -------
    (regridded_values, grid_spec) -- grid_spec is a
    pipeline.polygons.GridSpec, ready to pass straight into
    pipeline.polygons.grid_to_polygons().
    """
    if values.shape != native_lats.shape or values.shape != native_lons.shape:
        raise ValueError(
            f"Shape mismatch: values{values.shape}, lats{native_lats.shape}, lons{native_lons.shape}"
        )

    # NBM (like most NWP grib2 data) encodes longitude in 0-360 degrees
    # East convention (e.g. 275 instead of -85), not the -180/180
    # convention GeoJSON/web maps expect. Confirmed against real output:
    # raw values like 275.11 and 234.34 only make sense as CONUS
    # locations once converted (-84.89 near the Great Lakes, -125.66
    # near the Pacific Northwest). Fixing this here, once, means every
    # current and future hazard module gets it for free rather than
    # each having to remember to do this themselves.
    native_lons = np.where(native_lons > 180, native_lons - 360, native_lons)

    if target_bounds is None:
        margin = target_resolution_deg * 4
        west = float(native_lons.min()) + margin
        east = float(native_lons.max()) - margin
        south = float(native_lats.min()) + margin
        north = float(native_lats.max()) - margin
    else:
        west, south, east, north = target_bounds

    n_cols = int(round((east - west) / target_resolution_deg))
    n_rows = int(round((north - south) / target_resolution_deg))

    target_lons = west + np.arange(n_cols) * target_resolution_deg
    # row 0 = northernmost, matching GridSpec's expected convention (see polygons.py)
    target_lats = north - np.arange(n_rows) * target_resolution_deg
    grid_lon, grid_lat = np.meshgrid(target_lons, target_lats)

    points = np.column_stack([native_lons.ravel(), native_lats.ravel()])
    regridded = griddata(points, values.ravel(), (grid_lon, grid_lat), method=method)

    grid_spec = GridSpec(
        west=west,
        north=north,
        dx=target_resolution_deg,
        dy=-target_resolution_deg,
    )
    return regridded, grid_spec
