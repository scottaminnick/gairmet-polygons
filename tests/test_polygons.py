"""
test_polygons.py
-----------------
Sanity-checks pipeline/polygons.py using entirely made-up data. No
internet connection or real NBM file is needed to run this -- that's
the whole point of keeping polygons.py hazard-agnostic.

Run with:  python3 -m pytest tests/test_polygons.py -v
"""

import sys
from pathlib import Path

import numpy as np

# Make `pipeline` importable when running this file directly (not just via pytest)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.polygons import GridSpec, grid_to_polygons, polygons_to_feature_collection


def make_fake_ifr_probability_grid(rows=200, cols=300):
    """
    Builds a fake "probability of IFR ceiling" grid (values 0-100) with:
      - one big, smooth "blob" of high probability (like a real synoptic
        low-ceiling area)
      - a smaller, separate blob (to prove we correctly get MULTIPLE
        polygons, not just one)
      - scattered single-pixel noise spikes (to prove our small-area
        filter actually removes speckle instead of returning garbage)
    """
    rng = np.random.default_rng(seed=42)
    y, x = np.mgrid[0:rows, 0:cols]

    # Big blob centered around (60, 90)
    blob1 = 100 * np.exp(-(((y - 60) ** 2) / (2 * 35**2) + ((x - 90) ** 2) / (2 * 45**2)))

    # Smaller, separate blob centered around (150, 220)
    blob2 = 100 * np.exp(-(((y - 150) ** 2) / (2 * 12**2) + ((x - 220) ** 2) / (2 * 15**2)))

    grid = np.clip(blob1 + blob2, 0, 100)

    # Sprinkle in ~40 random single-pixel noise spikes above our test
    # threshold, scattered away from the real blobs, to confirm the
    # min_area filter cleans them up.
    noise_rows = rng.integers(0, rows, size=40)
    noise_cols = rng.integers(0, cols, size=40)
    grid[noise_rows, noise_cols] = 90

    return grid


def test_grid_to_polygons_basic_shape():
    values = make_fake_ifr_probability_grid()

    # Pretend this grid covers a small lon/lat box (doesn't matter which
    # real place -- we're just testing the math). dy is negative because
    # row 0 = north edge (rasterio/"image" convention).
    grid = GridSpec(west=-110.0, north=45.0, dx=0.02, dy=-0.02)

    threshold = 70.0  # "probability of IFR >= 70%" -- a plausible G-AIRMET-like cutoff
    polygons = grid_to_polygons(
        values,
        grid,
        threshold=threshold,
        min_area_deg2=0.02,       # should be big enough to kill 1-pixel noise
        simplify_tolerance_deg=0.02,
    )

    assert len(polygons) >= 1, "Expected at least one polygon above threshold"
    assert len(polygons) <= 3, (
        f"Expected ~2 real polygons after speckle filtering, got {len(polygons)} -- "
        "the min_area filter may not be removing noise as expected"
    )

    for poly in polygons:
        assert poly.is_valid, "shapely returned an invalid polygon"
        assert poly.area > 0

    print(f"\n[OK] Got {len(polygons)} polygon(s) after filtering noise.")
    for i, poly in enumerate(polygons):
        minx, miny, maxx, maxy = poly.bounds
        print(f"  polygon {i}: area={poly.area:.4f} deg^2, bounds=({minx:.2f},{miny:.2f})-({maxx:.2f},{maxy:.2f}), vertices={len(poly.exterior.coords)}")


def test_empty_grid_returns_no_polygons():
    """If nothing meets the threshold, we should get an empty list, not an error."""
    values = np.zeros((50, 50))
    grid = GridSpec(west=0.0, north=0.0, dx=1.0, dy=-1.0)
    polygons = grid_to_polygons(values, grid, threshold=50.0)
    assert polygons == []


def test_feature_collection_round_trip():
    """Confirm the GeoJSON wrapping works and carries properties through."""
    values = make_fake_ifr_probability_grid()
    grid = GridSpec(west=-110.0, north=45.0, dx=0.02, dy=-0.02)
    polygons = grid_to_polygons(values, grid, threshold=70.0, min_area_deg2=0.02)

    fc = polygons_to_feature_collection(
        polygons,
        properties={"hazard": "IFR", "threshold_pct": 70, "valid_time": "2026-07-07T18:00:00Z"},
    )

    assert fc["type"] == "FeatureCollection"
    assert len(fc["features"]) == len(polygons)
    for feature in fc["features"]:
        assert feature["properties"]["hazard"] == "IFR"
        assert feature["geometry"]["type"] in ("Polygon", "MultiPolygon")


if __name__ == "__main__":
    # Allow running this file directly (not just through pytest) for a
    # quick manual check while developing.
    test_grid_to_polygons_basic_shape()
    test_empty_grid_returns_no_polygons()
    test_feature_collection_round_trip()
    print("\nAll manual checks passed.")
