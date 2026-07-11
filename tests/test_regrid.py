"""
tests/test_regrid.py
---------------------
Tests pipeline/regrid.py using a SYNTHETIC Lambert Conformal Conic grid
built with real pyproj math (using the NDFD/HRRR 2.5km CONUS grid
parameters found during development -- standard parallel 25N, central
meridian -95W, spherical earth). We're not claiming these are certainly
NBM's exact parameters (that gets confirmed once real data flows
through), but they're genuine, realistic LCC projection math -- this
test is about verifying OUR resampling logic correctly un-distorts a
curvilinear grid, not about verifying eccodes' well-established grid
decoding (which we're deliberately not reimplementing -- see regrid.py's
docstring).
"""

import sys
from pathlib import Path

import numpy as np
import pyproj

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.regrid import regrid_to_regular_latlon


def make_synthetic_lcc_grid(nx=100, ny=80, dx_m=2500, dy_m=2500):
    """
    Builds a small Lambert Conformal Conic grid (real projection math,
    via pyproj) with a Gaussian blob placed at a KNOWN real-world
    lon/lat location, so we can verify after regridding that the blob
    ends up back at (approximately) that same location.
    """
    lcc_crs = pyproj.CRS.from_proj4(
        "+proj=lcc +lat_1=25 +lat_2=25 +lat_0=25 +lon_0=-95 "
        "+R=6371200 +units=m +no_defs"
    )
    to_lonlat = pyproj.Transformer.from_crs(lcc_crs, "EPSG:4326", always_xy=True)
    from_lonlat = pyproj.Transformer.from_crs("EPSG:4326", lcc_crs, always_xy=True)

    # Pick a known target location -- roughly Denver, CO -- and center
    # our small synthetic grid on it in the LCC's own x/y space.
    target_lon, target_lat = -104.99, 39.74
    center_x, center_y = from_lonlat.transform(target_lon, target_lat)

    xs = center_x + (np.arange(nx) - nx / 2) * dx_m
    ys = center_y + (np.arange(ny) - ny / 2) * dy_m
    grid_x, grid_y = np.meshgrid(xs, ys)

    native_lons, native_lats = to_lonlat.transform(grid_x, grid_y)

    # Gaussian blob centered exactly at the known target grid cell (in
    # x/y space, i.e. dead center of our synthetic grid).
    values = 100 * np.exp(
        -(((grid_x - center_x) ** 2) / (2 * (15 * dx_m) ** 2)
          + ((grid_y - center_y) ** 2) / (2 * (15 * dy_m) ** 2))
    )

    return values, native_lats, native_lons, (target_lon, target_lat)


def test_regrid_preserves_location_and_range():
    values, native_lats, native_lons, (target_lon, target_lat) = make_synthetic_lcc_grid()

    print(f"Native grid: lon range [{native_lons.min():.3f}, {native_lons.max():.3f}], "
          f"lat range [{native_lats.min():.3f}, {native_lats.max():.3f}]")
    print(f"Known blob center: lon={target_lon}, lat={target_lat}")
    print(f"Native data range: [{values.min():.2f}, {values.max():.2f}]")

    regridded, grid_spec = regrid_to_regular_latlon(
        values, native_lats, native_lons, target_resolution_deg=0.02
    )

    print(f"\nRegridded shape: {regridded.shape}")
    print(f"Regridded data range: [{np.nanmin(regridded):.2f}, {np.nanmax(regridded):.2f}]")
    print(f"GridSpec: west={grid_spec.west:.3f}, north={grid_spec.north:.3f}, "
          f"dx={grid_spec.dx}, dy={grid_spec.dy}")

    # Find where the peak actually landed after regridding
    peak_row, peak_col = np.unravel_index(np.nanargmax(regridded), regridded.shape)
    peak_lon = grid_spec.west + peak_col * grid_spec.dx
    peak_lat = grid_spec.north + peak_row * grid_spec.dy
    print(f"\nPeak found at: lon={peak_lon:.3f}, lat={peak_lat:.3f}")
    print(f"Distance from known center: "
          f"{abs(peak_lon - target_lon):.3f} deg lon, {abs(peak_lat - target_lat):.3f} deg lat")

    assert abs(peak_lon - target_lon) < 0.05, "Peak longitude drifted too far from known location"
    assert abs(peak_lat - target_lat) < 0.05, "Peak latitude drifted too far from known location"
    assert np.nanmax(regridded) > 90, "Peak value dropped too much during interpolation"
    assert np.nanmin(regridded) >= -1, "Interpolation produced implausible negative values"

    print("\n[OK] Regridding preserved both the blob's location and its value range.")


def test_regrid_converts_0_360_longitude_convention():
    """
    Regression test for a real bug found in production output: NBM (like
    most NWP grib2 data) encodes longitude in 0-360 convention (e.g. 275
    instead of -85). Feeding that straight through produced polygons
    shifted 360 degrees away from their real location. This confirms the
    conversion happens, and that normal -180/180 input passes through
    unaffected (i.e. this fix doesn't break the common case).
    """
    values, native_lats, native_lons, (target_lon, target_lat) = make_synthetic_lcc_grid()

    # Simulate what cfgrib actually handed us in production: same real
    # locations, but in 0-360 convention.
    native_lons_0_360 = np.where(native_lons < 0, native_lons + 360, native_lons)
    assert native_lons_0_360.min() > 180, "test setup should produce 0-360-style values"

    regridded, grid_spec = regrid_to_regular_latlon(
        values, native_lats, native_lons_0_360, target_resolution_deg=0.02
    )

    peak_row, peak_col = np.unravel_index(np.nanargmax(regridded), regridded.shape)
    peak_lon = grid_spec.west + peak_col * grid_spec.dx
    peak_lat = grid_spec.north + peak_row * grid_spec.dy

    print(f"0-360 input converted; peak found at lon={peak_lon:.3f}, lat={peak_lat:.3f} "
          f"(expected near {target_lon}, {target_lat})")

    assert -180 <= grid_spec.west <= 180, f"grid_spec.west={grid_spec.west} wasn't converted to -180/180"
    assert abs(peak_lon - target_lon) < 0.05, "0-360 input wasn't correctly converted to real location"
    assert abs(peak_lat - target_lat) < 0.05

    print("[OK] 0-360 longitude convention correctly converted to real -180/180 location.")


if __name__ == "__main__":
    test_regrid_preserves_location_and_range()
    test_regrid_converts_0_360_longitude_convention()
    print("\nAll manual checks passed.")
