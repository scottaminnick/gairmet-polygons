"""
tests/test_ifr_pipeline.py
---------------------------
Tests the CORE LOGIC of generate_ifr_polygons() -- regridding two
curvilinear fields, combining via max(), and producing polygons --
without needing real network access. We substitute synthetic-but-real
LCC-projected data (same technique as test_regrid.py) for the parts
that would otherwise come from an actual NBM fetch.

This deliberately does NOT call generate_ifr_polygons() itself (that
function's first two lines are real network fetches) -- instead it
exercises the same regrid -> np.maximum -> grid_to_polygons sequence
directly, which is the actual novel logic worth testing here. The fetch
itself (pipeline/fetch_nbm.py) was already validated against your real
uploaded .idx file in the previous step.
"""

import sys
from pathlib import Path

import numpy as np
import pyproj

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.polygons import grid_to_polygons, polygons_to_feature_collection
from pipeline.regrid import regrid_to_regular_latlon


def make_synthetic_field(center_lon, center_lat, peak_value, spread_cells=15, nx=120, ny=100, dx_m=2500):
    """Same synthetic-LCC-grid technique as test_regrid.py, parameterized so we can build two different fields."""
    lcc_crs = pyproj.CRS.from_proj4(
        "+proj=lcc +lat_1=25 +lat_2=25 +lat_0=25 +lon_0=-95 +R=6371200 +units=m +no_defs"
    )
    to_lonlat = pyproj.Transformer.from_crs(lcc_crs, "EPSG:4326", always_xy=True)
    from_lonlat = pyproj.Transformer.from_crs("EPSG:4326", lcc_crs, always_xy=True)

    center_x, center_y = from_lonlat.transform(center_lon, center_lat)
    xs = center_x + (np.arange(nx) - nx / 2) * dx_m
    ys = center_y + (np.arange(ny) - ny / 2) * dx_m
    grid_x, grid_y = np.meshgrid(xs, ys)
    lons, lats = to_lonlat.transform(grid_x, grid_y)

    values = peak_value * np.exp(
        -(((grid_x - center_x) ** 2) / (2 * (spread_cells * dx_m) ** 2)
          + ((grid_y - center_y) ** 2) / (2 * (spread_cells * dx_m) ** 2))
    )
    return values, lats, lons


def test_max_combine_picks_the_higher_hazard_in_each_region():
    """
    Ceiling probability is high over Denver; visibility probability is
    high over Kansas City -- two separate, non-overlapping blobs. After
    max-combining, we should see BOTH regions flagged as hazardous
    (proving max() correctly implements the "OR" semantics), not just
    one or a blend that washes both out.
    """
    denver = (-104.99, 39.74)
    kansas_city = (-94.58, 39.10)

    ceil_values, ceil_lats, ceil_lons = make_synthetic_field(*denver, peak_value=90)
    vis_values, vis_lats, vis_lons = make_synthetic_field(*kansas_city, peak_value=85)

    # Same target grid for both, wide enough to cover both cities
    bounds = (-108.0, 37.0, -92.0, 42.0)  # west, south, east, north
    ceil_regridded, grid_spec = regrid_to_regular_latlon(
        ceil_values, ceil_lats, ceil_lons, target_resolution_deg=0.05, target_bounds=bounds
    )
    vis_regridded, _ = regrid_to_regular_latlon(
        vis_values, vis_lats, vis_lons, target_resolution_deg=0.05, target_bounds=bounds
    )

    combined = np.maximum(np.nan_to_num(ceil_regridded), np.nan_to_num(vis_regridded))

    print(f"Combined grid range: [{combined.min():.1f}, {combined.max():.1f}]")

    polygons = grid_to_polygons(combined, grid_spec, threshold=50.0, min_area_deg2=0.01)
    print(f"Found {len(polygons)} polygon(s) at 50% threshold")
    for p in polygons:
        print(f"  bounds: {p.bounds}")

    assert len(polygons) == 2, (
        f"Expected 2 separate hazard regions (Denver ceiling + KC visibility), got {len(polygons)}"
    )

    # Confirm one polygon covers each city, not both blended into one
    # region or one blob missing entirely.
    denver_covered = any(
        p.bounds[0] < denver[0] < p.bounds[2] and p.bounds[1] < denver[1] < p.bounds[3] for p in polygons
    )
    kc_covered = any(
        p.bounds[0] < kansas_city[0] < p.bounds[2] and p.bounds[1] < kansas_city[1] < p.bounds[3] for p in polygons
    )
    assert denver_covered, "Denver (ceiling hazard) not found in output polygons"
    assert kc_covered, "Kansas City (visibility hazard) not found in output polygons"

    # Confirm the FeatureCollection wrapping works with real polygon output
    fc = polygons_to_feature_collection(
        polygons, properties={"hazard": "IFR", "threshold_pct": 50.0, "valid_time": "2026-07-09T18:00:00Z"}
    )
    assert len(fc["features"]) == 2
    assert all(f["properties"]["hazard"] == "IFR" for f in fc["features"])

    print("\n[OK] max-combine correctly preserved both separate hazard regions.")


if __name__ == "__main__":
    test_max_combine_picks_the_higher_hazard_in_each_region()
