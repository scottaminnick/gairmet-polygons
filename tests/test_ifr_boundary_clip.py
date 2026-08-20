"""
tests/test_ifr_boundary_clip.py
----------------------------------
Regression tests for the ARTCC-boundary clip in
pipeline/hazards/ifr.py's polygonize_ifr_grid().

The real problem this pins: NBM's CONUS grid has genuine coverage far
beyond AWC's area of responsibility -- out over the Pacific, up into
Canada, out over the Atlantic -- and IFR conditions out there are REAL
data, not noise, so nothing in the threshold/merge/area-filter chain
drops them. Before the clip existed, a marine layer sitting offshore
produced a real polygon hundreds of miles out to sea. MTN OBSC already
hit the identical problem with its generously-sized terrain grid and
solved it with data/boundaries/artcc.json; IFR now uses that same file
through the same shared helper (pipeline.boundaries.get_boundary_mask).

Runs against a grid round-tripped through pipeline.polygons'
save_grid_cache/load_grid_cache -- i.e. the same cached-grid path the
web app's live recompute endpoint uses, quantization and all, rather
than handing polygonize_ifr_grid() in-memory float arrays it would
never see in production.
"""

import sys
from datetime import datetime
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pipeline.hazards.ifr as ifr
from pipeline.hazards.ifr import polygonize_ifr_grid
from pipeline.polygons import GridSpec, load_grid_cache, save_grid_cache

# Deliberately a REAL NBM-like domain reaching well out over the
# Pacific: west of -130 is water NBM genuinely forecasts for and the
# ARTCC boundary genuinely excludes. Small enough (280x500 at 0.05 deg)
# to keep the boundary rasterization quick.
GRID_SPEC = GridSpec(west=-140.0, north=52.0, dx=0.05, dy=-0.05)
SHAPE = (280, 500)
VALID_DATE = datetime(2026, 7, 14, 15)

# Well offshore in the Pacific -- ~450 nm west of the Oregon coast, far
# outside any ARTCC FIR, and comfortably inside the synthetic hazard
# block below so it WOULD be polygonized without the clip.
PACIFIC_POINT = (45.0, -135.0)  # (lat, lon)

# Real land inside the same hazard block, to keep the offshore assertion
# from passing simply because nothing was generated at all.
PORTLAND_POINT = (45.5, -122.6)


def _cached_test_grids(tmp_path):
    """
    Builds the four IFR probability grids, writes them through
    save_grid_cache(), and reads them back with load_grid_cache() --
    exercising the real cached-grid path (uint8 quantization included)
    that the web app's recompute endpoint runs against.

    Ceiling is high across one block spanning open Pacific water through
    the Oregon/Washington coast into inland terrain; visibility and
    precipitation are zero everywhere, so only the CIG layer is active
    and the resulting polygon shape is decided purely by the clip. The
    block is kept clear of the grid's own edges so its contour closes
    within the domain rather than against the array border.
    """
    ceil = np.zeros(SHAPE, dtype=np.float32)
    ceil[80:200, 60:380] = 90.0  # lat 42N-48N, lon 137W-121W
    zeros = np.zeros(SHAPE, dtype=np.float32)

    cache_path = tmp_path / "ifr_test_grid.npz"
    save_grid_cache(
        cache_path,
        {"ceiling": ceil, "visibility_3sm": zeros, "visibility_1sm": zeros, "precipitation": zeros},
        GRID_SPEC,
    )
    grids, grid_spec = load_grid_cache(cache_path)
    return (
        grids["ceiling"],
        grids["visibility_3sm"],
        grids["visibility_1sm"],
        grids["precipitation"],
        grid_spec,
    )


def _polygonize(grids):
    ceil, vis3, vis1, precip, grid_spec = grids
    return polygonize_ifr_grid(
        ceil, vis3, vis1, precip, grid_spec, VALID_DATE, 0,
        threshold_pct=50.0,
        neighborhood_radius_nm=0.0,
        min_area_sq_mi=3000.0,
    )


def _covers(feature_collection, lat, lon) -> bool:
    from shapely.geometry import Point, shape

    point = Point(lon, lat)
    return any(shape(f["geometry"]).contains(point) for f in feature_collection["features"])


def test_offshore_pacific_point_is_not_inside_any_ifr_polygon(tmp_path):
    """
    THE regression: 45N 135W is open Pacific, hundreds of miles outside
    every ARTCC FIR, and the synthetic ceiling grid flags it as solidly
    IFR. No generated polygon may contain it.
    """
    fc = _polygonize(_cached_test_grids(tmp_path))

    assert len(fc["features"]) > 0, "test grid produced no polygons at all -- the offshore check would be vacuous"
    lat, lon = PACIFIC_POINT
    assert not _covers(fc, lat, lon), (
        f"{lat}N {abs(lon)}W is open Pacific outside every ARTCC FIR, but a generated IFR polygon covers it"
    )


def test_land_inside_the_artcc_boundary_is_still_covered(tmp_path):
    """
    The other half of the same guarantee: clipping must not eat real
    CONUS. Portland sits in the same synthetic hazard block as the
    offshore point above, well inside the ARTCC boundary, and must still
    come out inside a polygon.
    """
    fc = _polygonize(_cached_test_grids(tmp_path))

    lat, lon = PORTLAND_POINT
    assert _covers(fc, lat, lon), (
        f"{lat}N {abs(lon)}W is real CONUS land inside the ARTCC boundary and must still be polygonized"
    )


def test_offshore_point_would_be_covered_without_the_clip(tmp_path, monkeypatch):
    """
    Proves the test above actually has teeth rather than passing because
    the synthetic grid never reached that far west: with the boundary
    mask forced all-True (i.e. the pre-clip behavior), the SAME grid does
    put a polygon over 45N 135W.
    """
    monkeypatch.setattr(ifr, "get_boundary_mask", lambda grid_spec, shape, path: np.ones(shape, dtype=bool))
    fc = _polygonize(_cached_test_grids(tmp_path))

    lat, lon = PACIFIC_POINT
    assert _covers(fc, lat, lon), (
        "Without the ARTCC clip this grid should cover the offshore point -- if it doesn't, "
        "the clip test above proves nothing"
    )


def test_clip_uses_the_artcc_boundary_file(tmp_path):
    """
    Pins WHICH boundary the clip applies: the polygon's western edge
    should stop at the real ARTCC/oceanic boundary, not at the hazard
    block's own western edge (137W) and not at the coastline (~124W --
    the ARTCC boundary deliberately includes adjacent coastal waters per
    NWSI 10-811, so IFR is expected to extend some way offshore).
    """
    from shapely.geometry import shape

    fc = _polygonize(_cached_test_grids(tmp_path))
    westernmost = min(shape(f["geometry"]).bounds[0] for f in fc["features"])

    assert westernmost > -137.0, "polygon reaches the hazard block's own west edge -- nothing was clipped"
    assert westernmost < -124.0, (
        "polygon stops at or inside the coastline -- the ARTCC boundary includes adjacent coastal waters, "
        "so this looks like the wrong boundary file (or the land mask) is being applied"
    )


if __name__ == "__main__":
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        test_offshore_pacific_point_is_not_inside_any_ifr_polygon(Path(d))
        test_land_inside_the_artcc_boundary_is_still_covered(Path(d))
        test_clip_uses_the_artcc_boundary_file(Path(d))
        print("[OK] IFR polygons are clipped to the ARTCC boundary.")
