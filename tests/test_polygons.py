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


def test_geodesic_area_matches_known_reference():
    """
    A roughly 1deg x 1deg box near the equator should be close to
    111km x 111km = ~4,780 sq mi (real geodesic area, not the flat
    degrees^2 proxy). This is a sanity check against a known reference
    value, not just internal consistency.
    """
    from pipeline.polygons import geodesic_area_sq_mi
    from shapely.geometry import box

    one_degree_box_at_equator = box(0, 0, 1, 1)
    area = geodesic_area_sq_mi(one_degree_box_at_equator)
    print(f"\n1deg x 1deg box at equator: {area:.0f} sq mi (expected ~4,780)")
    assert 4500 < area < 5100, f"Geodesic area {area:.0f} sq mi is too far from the expected ~4,780"


def test_geodesic_area_correctly_varies_with_latitude():
    """
    The SAME 1deg x 1deg box should have a noticeably SMALLER real area
    at high latitude than at the equator (since longitude lines converge
    toward the poles) -- proving this isn't just reusing the flat
    degrees^2 approximation we're trying to replace.
    """
    from pipeline.polygons import geodesic_area_sq_mi
    from shapely.geometry import box

    equator_box = box(0, 0, 1, 1)
    high_lat_box = box(0, 59, 1, 60)

    area_equator = geodesic_area_sq_mi(equator_box)
    area_high_lat = geodesic_area_sq_mi(high_lat_box)
    print(f"\nEquator box: {area_equator:.0f} sq mi, 59-60N box: {area_high_lat:.0f} sq mi")

    assert area_high_lat < area_equator * 0.6, (
        "A 1x1 degree box near 60N should be meaningfully smaller in real area than one at the "
        "equator -- if this fails, the area calculation isn't properly geodesic"
    )


def test_min_area_sq_mi_filters_using_real_area_not_degrees():
    """
    Regression-style test for the actual bug this was built to fix:
    a polygon sized to pass a degrees^2 threshold at one latitude could
    legitimately fail a real-world square-mile threshold, and vice
    versa. Confirms min_area_sq_mi drives the real decision.
    """
    values = np.zeros((100, 100))
    values[40:60, 40:60] = 80  # a modest blob, not tiny

    # Placed at a high latitude, where the same degrees^2 footprint
    # covers meaningfully less real area.
    grid = GridSpec(west=-150.0, north=70.0, dx=0.05, dy=-0.05)

    polygons_loose = grid_to_polygons(values, grid, threshold=50.0, min_area_sq_mi=10)
    polygons_strict = grid_to_polygons(values, grid, threshold=50.0, min_area_sq_mi=100000)

    print(f"\nWith a tiny min_area_sq_mi: {len(polygons_loose)} polygon(s)")
    print(f"With an enormous min_area_sq_mi: {len(polygons_strict)} polygon(s)")

    assert len(polygons_loose) == 1, "A real, modest-sized blob should pass a tiny area threshold"
    assert len(polygons_strict) == 0, "The same blob should be filtered out by an enormous area threshold"


def test_boundary_smoothing_reduces_jaggedness():
    """
    Boundary smoothing is ALWAYS used together with a following
    simplify() pass in grid_to_polygons() (by design -- buffering with
    round joins adds curve-tessellation vertices that simplify() then
    cleans up), so that's the realistic combination to test, not
    smoothing in isolation.

    Uses perimeter/sqrt(area) as a "jaggedness" metric instead of raw
    vertex count -- a jagged boundary has much more perimeter for the
    same enclosed area than a smooth one, and this metric isn't
    sensitive to buffer's curve-tessellation vertex count the way a
    naive vertex-count comparison is.
    """
    rng = np.random.default_rng(1)
    yy, xx = np.mgrid[0:150, 0:150]
    base_blob = 90 * np.exp(-(((yy - 75) ** 2 + (xx - 75) ** 2) / (2 * 35 ** 2)))
    values = np.clip(base_blob + rng.normal(0, 12, size=base_blob.shape), 0, 100)

    grid = GridSpec(west=-100.0, north=40.0, dx=0.02, dy=-0.02)

    simplify_only = grid_to_polygons(
        values, grid, threshold=50.0, min_area_deg2=0.05, simplify_tolerance_deg=0.02
    )
    smoothed_then_simplified = grid_to_polygons(
        values, grid, threshold=50.0, min_area_deg2=0.05, simplify_tolerance_deg=0.02,
        boundary_smoothing_deg=0.03,
    )

    assert len(simplify_only) == 1 and len(smoothed_then_simplified) == 1

    def jaggedness(poly):
        return poly.length / (poly.area ** 0.5)

    j_before = jaggedness(simplify_only[0])
    j_after = jaggedness(smoothed_then_simplified[0])
    print(f"\nJaggedness (perimeter/sqrt(area)), simplify only: {j_before:.2f}")
    print(f"Jaggedness, smoothing+simplify: {j_after:.2f}")

    assert j_after < j_before, "Boundary smoothing should reduce the perimeter/sqrt(area) jaggedness ratio"

    # Confirm smoothing didn't relocate or drastically resize the blob
    dist_deg = simplify_only[0].centroid.distance(smoothed_then_simplified[0].centroid)
    area_ratio = smoothed_then_simplified[0].area / simplify_only[0].area
    print(f"Centroid shift: {dist_deg:.4f} deg, area ratio: {area_ratio:.2f}")
    assert dist_deg < 0.05, "Smoothing shouldn't relocate the polygon"
    assert 0.7 < area_ratio < 1.3, "Smoothing shouldn't drastically change the polygon's size"


def test_merge_nearby_polygons_preserves_isolated_shape():
    """
    The whole point of using polygon-level merging instead of a grid-level
    circular blur: an isolated polygon with nothing nearby should come
    back out close to its ORIGINAL shape and area, not inflated into a
    circle. This was a real bug found from comparing real output against
    an actual G-AIRMET graphic -- grid-level neighborhood-max smoothing
    inflated every isolated hazard area into a literal circle.
    """
    from shapely.geometry import box
    from pipeline.polygons import merge_nearby_polygons

    isolated = box(-100, 40, -99.5, 40.4)  # a simple rectangle, far from anything else
    far_away_other = box(-70, 25, -69.5, 25.4)  # nowhere near the first one

    result = merge_nearby_polygons([isolated, far_away_other], radius_nm=50)

    assert len(result) == 2, "Two far-apart polygons should remain separate"

    original_area = isolated.area
    matched = min(result, key=lambda p: abs(p.area - original_area))
    area_change_pct = 100 * abs(matched.area - original_area) / original_area
    print(f"\nIsolated polygon area change after merge pass: {area_change_pct:.1f}%")
    assert area_change_pct < 15, (
        f"An isolated polygon's area changed by {area_change_pct:.1f}% -- should stay close to "
        "its original size, not inflate like a grid-level circular blur would"
    )


def test_merge_nearby_polygons_merges_close_ones():
    """Two polygons within radius_nm of each other should merge into one connected shape."""
    from shapely.geometry import box
    from pipeline.polygons import geodesic_area_sq_mi, merge_nearby_polygons

    # Two small boxes about 10nm apart (roughly -- close enough for this test's purposes)
    box1 = box(-100.0, 40.0, -99.9, 40.1)
    box2 = box(-99.75, 40.0, -99.65, 40.1)

    far_radius_result = merge_nearby_polygons([box1, box2], radius_nm=50)
    close_radius_result = merge_nearby_polygons([box1, box2], radius_nm=1)

    print(f"\nWith large radius: {len(far_radius_result)} polygon(s)")
    print(f"With tiny radius: {len(close_radius_result)} polygon(s)")

    assert len(far_radius_result) == 1, "A radius large enough to span the gap should merge them"
    assert len(close_radius_result) == 2, "A radius much smaller than the gap should NOT merge them"

    merged_area = geodesic_area_sq_mi(far_radius_result[0])
    sum_of_originals = geodesic_area_sq_mi(box1) + geodesic_area_sq_mi(box2)
    assert merged_area > sum_of_originals, "Merging should add some bridging area, not just touch the two boxes"


def test_filter_polygons_by_area():
    """Basic sanity check that the standalone area filter actually filters."""
    from shapely.geometry import box
    from pipeline.polygons import filter_polygons_by_area

    tiny = box(0, 0, 0.01, 0.01)
    huge = box(0, 0, 5, 5)

    result = filter_polygons_by_area([tiny, huge], min_area_sq_mi=1000)
    assert len(result) == 1
    assert result[0] is huge


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
    test_geodesic_area_matches_known_reference()
    test_geodesic_area_correctly_varies_with_latitude()
    test_min_area_sq_mi_filters_using_real_area_not_degrees()
    test_boundary_smoothing_reduces_jaggedness()
    test_merge_nearby_polygons_preserves_isolated_shape()
    test_merge_nearby_polygons_merges_close_ones()
    test_filter_polygons_by_area()
    test_feature_collection_round_trip()
    print("\nAll manual checks passed.")
