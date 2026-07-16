"""
tests/test_fetch_terrain.py
-----------------------------
Tests every piece of pipeline/fetch_terrain.py that doesn't require
network access (tile naming, byte parsing, the radius math, and the
full pooling/filtering pipeline against a synthetic mosaic with a known
embedded peak). The actual S3 fetch itself needs a real run with
internet egress to confirm -- see fetch_terrain.py's module docstring.
"""

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.fetch_terrain import (
    _block_max_pool,
    _block_mean_pool,
    _parse_hgt_bytes,
    _radius_deg,
    compute_output_grids,
    list_conus_tiles,
    skadi_tile_name,
    skadi_url,
)
from pipeline.grid_spec import GridSpec


def test_skadi_tile_name_examples():
    # Colorado Front Range
    assert skadi_tile_name(39, -105) == "N39W105"
    # Southern hemisphere / eastern hemisphere edge cases, to make sure
    # the hemisphere-letter logic isn't accidentally CONUS-only
    assert skadi_tile_name(-33, 151) == "S33E151"  # Sydney
    assert skadi_tile_name(0, 0) == "N00E000"


def test_skadi_url_shape():
    url = skadi_url(39, -105)
    assert url.endswith("/N39/N39W105.hgt.gz")
    assert url.startswith("https://s3.amazonaws.com/elevation-tiles-prod/skadi")


def test_list_conus_tiles_covers_known_point():
    tiles = list_conus_tiles()
    # Boston Mountains, AR -- the specific area flagged as possibly
    # missing from the legacy NMAP shapefile
    assert (35, -93) in tiles
    # Should NOT wildly overshoot -- sanity bound on count (CONUS_BOUNDS
    # is 61 x 28 degrees = 1,708 possible tiles, before any are skipped
    # for being all-ocean)
    assert len(tiles) == 61 * 28


def test_parse_hgt_bytes_void_becomes_sea_level():
    tile_size = 4  # small synthetic tile, not a real 3601
    raw = np.array(
        [[100, 200, -32768, 300], [400, 500, 600, 700], [0, 0, 0, 0], [1, 2, 3, 4]],
        dtype=">i2",
    )
    parsed = _parse_hgt_bytes(raw.tobytes(), tile_size=tile_size)
    assert parsed.shape == (tile_size, tile_size)
    assert parsed[0, 2] == 0.0  # void sentinel -> sea level
    assert parsed[0, 0] == 100.0
    assert parsed[1, 3] == 700.0


def test_radius_deg_equator_is_symmetric():
    lat_deg, lon_deg = _radius_deg(12.0, center_lat_deg=0.0)
    assert lat_deg == 12.0 / 60.0
    # at the equator cos(0)=1, so lon and lat radii should match exactly
    assert abs(lon_deg - lat_deg) < 1e-9


def test_radius_deg_high_latitude_widens_longitude():
    lat_deg_low, lon_deg_low = _radius_deg(12.0, center_lat_deg=22.0)
    lat_deg_high, lon_deg_high = _radius_deg(12.0, center_lat_deg=50.0)
    # latitude half-width never changes with location -- 1 nm is always
    # 1 arcminute of latitude
    assert abs(lat_deg_low - lat_deg_high) < 1e-9
    # longitude half-width MUST grow at higher latitude (meridians
    # converge toward the poles) -- this is the specific bug being
    # guarded against: a single global conversion factor would be wrong
    # by ~45% at one end of CONUS or the other
    assert lon_deg_high > lon_deg_low
    # sanity-check the actual ratio matches cos(22)/cos(50)
    import math
    expected_ratio = math.cos(math.radians(22.0)) / math.cos(math.radians(50.0))
    assert abs((lon_deg_high / lon_deg_low) - expected_ratio) < 1e-6


def test_block_max_pool_preserves_peak():
    arr = np.zeros((4, 4), dtype=np.float32)
    arr[1, 1] = 999.0  # one sharp peak inside the top-left 2x2 block
    pooled = _block_max_pool(arr, factor=2)
    assert pooled.shape == (2, 2)
    assert pooled[0, 0] == 999.0  # MAX pool must not average the peak away
    assert pooled[0, 1] == 0.0
    assert pooled[1, 0] == 0.0


def test_block_mean_pool_averages():
    arr = np.array([[0, 0, 10, 10], [0, 0, 10, 10], [0, 0, 0, 0], [0, 0, 0, 0]], dtype=np.float32)
    pooled = _block_mean_pool(arr, factor=2)
    assert pooled.shape == (2, 2)
    assert pooled[0, 0] == 0.0
    assert pooled[0, 1] == 10.0  # uniform block -> mean == that value
    assert pooled[1, 0] == 0.0


def test_compute_output_grids_finds_synthetic_peak():
    """
    Builds a small synthetic "intermediate mosaic" at 30 arcsec (matching
    fetch_terrain's real INTERMEDIATE_ARCSEC) with ONE sharp 9,000 ft
    peak embedded in an otherwise flat 2,000 ft plain, and confirms:
      - ridge_elevation_ft shows ~9,000 ft in a neighborhood AROUND the
        peak (not just at the exact peak pixel) -- this is the entire
        point of the terrain-radius search.
      - baseline_elevation_ft does NOT jump to 9,000 ft -- it should
        stay close to the surrounding 2,000 ft, confirming baseline
        represents "the ground here," not "the nearest peak."
      - the output grid's real-world coordinates correctly locate the
        peak where it was actually placed, not off-by-one or transposed.
    """
    intermediate_deg = 30 / 3600.0  # matches INTERMEDIATE_ARCSEC
    n_rows, n_cols = 200, 200
    mosaic = np.full((n_rows, n_cols), 2000.0, dtype=np.float32)

    peak_row, peak_col = 100, 100
    mosaic[peak_row, peak_col] = 9000.0

    west, north = -110.0, 40.0
    mosaic_grid_spec = GridSpec(west=west, north=north, dx=intermediate_deg, dy=-intermediate_deg)
    peak_lat = north - peak_row * intermediate_deg
    peak_lon = west + peak_col * intermediate_deg

    output_bounds = (west, north - n_rows * intermediate_deg, west + n_cols * intermediate_deg, north)

    baseline_ft, ridge_ft, out_grid_spec = compute_output_grids(
        mosaic,
        mosaic_grid_spec,
        terrain_radius_nm=12.0,
        output_resolution_deg=90 / 3600.0,  # matches OUTPUT_RESOLUTION_DEG (0.025 deg)
        output_bounds=output_bounds,
    )

    # Map the peak's real-world location into output grid indices
    out_row = round((out_grid_spec.north - peak_lat) / out_grid_spec.dx)
    out_col = round((peak_lon - out_grid_spec.west) / out_grid_spec.dx)

    # Ridge grid: the peak should register at/near its real location,
    # clearly elevated above the flat 2,000 ft plain
    assert ridge_ft[out_row, out_col] > 8000

    # Ridge grid: a few cells away but still within the ~12nm search
    # radius, the peak should STILL be visible (that's the entire
    # feature being tested -- a valley cell near a ridge must see it)
    assert ridge_ft[out_row, out_col + 2] > 8000

    # Far away from the peak (outside any reasonable 12nm radius),
    # ridge should have fallen back to the flat plain's elevation
    assert ridge_ft[0, 0] < 2500

    # Baseline should NOT have jumped to peak height anywhere -- it's a
    # local smoothing, not a nearby-ridge search
    assert baseline_ft.max() < 3000


if __name__ == "__main__":
    import pytest

    pytest.main([__file__, "-v"])
