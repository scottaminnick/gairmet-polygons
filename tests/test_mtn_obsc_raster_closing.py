"""
tests/test_mtn_obsc_raster_closing.py
----------------------------------------
Tests the raster neighborhood closing in polygonize_mtn_obsc_grid()
(USE_RASTER_CLOSING_MTNOBSC) and the water-corrected baseline in
pipeline/fetch_terrain.py.

WHAT WENT WRONG, and what these pin: forecaster review of the 09Z run
found MTN OBSC areas sitting on Lake Superior. The land mask excludes
the lake correctly -- that was point-tested first -- so the coverage came
from merge_nearby_polygons(), which closes in VECTOR space after every
gate has already been applied and can therefore push a polygon straight
back across ground a gate removed. Closing the raster instead lets the
gates be re-applied to the closed mask, which is a guarantee rather than
a hope.

Every test here has a control that fails if the fix is removed. That
isn't ceremony: an assertion that some point is outside every polygon
passes trivially on a quiet day, or whenever the hazard simply never
reached that far, and a test that cannot distinguish those cases from a
working fix is not testing anything.
"""

import json
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import shapely
from shapely.geometry import Point
from shapely.geometry import shape as shapely_shape

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pipeline.hazards.mtn_obsc as mtn_obsc
from pipeline.boundaries import CONUS_BOUNDARY_PATH, LAND_BOUNDARY_PATH, get_boundary_mask
from pipeline.hazards.mtn_obsc import (
    CEILING_PROB_THRESHOLDS_FT,
    MIN_BASELINE_ELEVATION_FT,
    MOUNTAINOUS_RELIEF_THRESHOLD_FT,
    polygonize_mtn_obsc_grid,
)
from pipeline.polygons import GridSpec, cell_dimensions_km, close_mask

VALID_DATE = datetime(2026, 7, 16, 12)
RADIUS_NM = 50.0

# Lake Superior and both its shores. Deliberately wide enough that the
# lake is INTERIOR to the array: a masked-out region touching the array
# edge produces an open contour, which grid_to_polygons closes by
# joining its ends -- swallowing the region. That is a pre-existing
# property of the v1 contour path, unrelated to the closing, and a
# window that triggered it would make these tests measure the wrong
# thing.
SUPERIOR_SPEC = GridSpec(west=-93.0, north=49.5, dx=0.025, dy=-0.025)
SUPERIOR_SHAPE = (200, 400)

# The point from the forecaster's report.
MID_LAKE_SUPERIOR = (47.5, -87.5)

# A second lake point, 34 nm from the nearest shore, that the vector
# closing genuinely does cover at 50 nm. MID_LAKE_SUPERIOR sits in the
# wide eastern basin where the nearest US land across the water is more
# than 100 nm away -- beyond what a 50 nm closing can bridge from either
# side -- so it cannot tell a working fix from a quiet day. This one can.
BRIDGED_LAKE_POINT = (47.2, -90.25)


def _uniform_relief_scenario(spec, shape, relief_ft=2200.0, baseline_ft=800.0):
    """
    Terrain that is mountainous everywhere the gates allow: relief well
    over the threshold, baseline well over the deep-water floor, and
    ceiling probability at 90% for every published threshold. The hazard
    footprint is then exactly land AND inside-ARTCC, which is what makes
    "did a polygon end up over water" a clean question.
    """
    baseline = np.full(shape, baseline_ft)
    ridge = np.full(shape, baseline_ft + relief_ft)
    ceiling_probs = {t: np.full(shape, 90.0) for t in CEILING_PROB_THRESHOLDS_FT}
    zeros = np.zeros(shape)
    return ceiling_probs, zeros, zeros, zeros, baseline, ridge


def _polygonize(spec, shape, raster_closing, **kwargs):
    ceiling_probs, precip, vis3, vis1, baseline, ridge = _uniform_relief_scenario(spec, shape)
    original = mtn_obsc.USE_RASTER_CLOSING_MTNOBSC
    mtn_obsc.USE_RASTER_CLOSING_MTNOBSC = raster_closing
    try:
        return polygonize_mtn_obsc_grid(
            ceiling_probs, precip, vis3, vis1, baseline, ridge, spec, VALID_DATE, 6,
            threshold_pct=50.0, neighborhood_radius_nm=RADIUS_NM, min_area_sq_mi=3000.0,
            **kwargs,
        )
    finally:
        mtn_obsc.USE_RASTER_CLOSING_MTNOBSC = original


def _covers(feature_collection, lat, lon) -> bool:
    point = Point(lon, lat)
    return any(shapely_shape(f["geometry"]).contains(point) for f in feature_collection["features"])


def _covered_cells(feature_collection, spec, shape) -> np.ndarray:
    rows, cols = np.indices(shape)
    lons = (spec.west + cols * spec.dx).ravel()
    lats = (spec.north + rows * spec.dy).ravel()
    covered = np.zeros(lons.shape, dtype=bool)
    for feature in feature_collection["features"]:
        covered |= shapely.contains_xy(shapely_shape(feature["geometry"]), lons, lats)
    return covered.reshape(shape)


# ---------------------------------------------------------------------------
# Water: the reported bug.
# ---------------------------------------------------------------------------

def test_no_polygon_covers_lake_superior():
    """
    The 09Z report. With terrain mountainous across both shores and the
    radius at 50 nm, no polygon may sit on the lake.
    """
    fc = _polygonize(SUPERIOR_SPEC, SUPERIOR_SHAPE, raster_closing=True)
    assert fc["features"], "no polygons at all -- this test would be vacuous"

    assert not _covers(fc, *MID_LAKE_SUPERIOR), (
        f"{MID_LAKE_SUPERIOR[0]}N {abs(MID_LAKE_SUPERIOR[1])}W is open water in Lake Superior "
        "and is inside a MTN OBSC polygon"
    )
    assert not _covers(fc, *BRIDGED_LAKE_POINT), (
        f"{BRIDGED_LAKE_POINT[0]}N {abs(BRIDGED_LAKE_POINT[1])}W is open water in Lake Superior "
        "and is inside a MTN OBSC polygon"
    )


def test_the_vector_closing_did_cover_the_lake():
    """
    THE CONTROL for the test above, and the reason BRIDGED_LAKE_POINT
    exists alongside the reported one: on the vector path -- the code as
    it shipped for 09Z -- that point IS inside a polygon. So the
    assertion above is about the fix, not about the hazard never
    reaching the lake.

    Also pins the aggregate, which is the number worth watching: how
    much open water the two paths cover across the whole lake.
    """
    vector = _polygonize(SUPERIOR_SPEC, SUPERIOR_SHAPE, raster_closing=False)
    raster = _polygonize(SUPERIOR_SPEC, SUPERIOR_SHAPE, raster_closing=True)

    assert _covers(vector, *BRIDGED_LAKE_POINT), (
        "the vector closing no longer covers the lake point either, so the test above proves "
        "nothing -- pick a new BRIDGED_LAKE_POINT from a fresh measurement"
    )

    land = get_boundary_mask(SUPERIOR_SPEC, SUPERIOR_SHAPE, LAND_BOUNDARY_PATH)
    artcc = get_boundary_mask(SUPERIOR_SPEC, SUPERIOR_SHAPE, CONUS_BOUNDARY_PATH)
    water = ~land & artcc  # water this product could plausibly claim

    vector_water = int((_covered_cells(vector, SUPERIOR_SPEC, SUPERIOR_SHAPE) & water).sum())
    raster_water = int((_covered_cells(raster, SUPERIOR_SPEC, SUPERIOR_SHAPE) & water).sum())
    print(f"\n[water] cells of open water covered: vector {vector_water}, raster {raster_water}")

    assert raster_water < vector_water / 10, (
        f"raster closing covers {raster_water} water cells against the vector path's "
        f"{vector_water} -- expected an order of magnitude better"
    )


# ---------------------------------------------------------------------------
# The ARTCC gate, and the mountainous gate.
#
# These are asserted on the MASK rather than on polygons. For MTN OBSC
# the ARTCC gate is nearly redundant with the land gate -- us_states.json
# is US land only, so Canada and Mexico are already gone -- which leaves
# too few cells outside ARTCC but inside land to make a polygon-level
# test meaningful. The mask is where the guarantee actually lives.
# ---------------------------------------------------------------------------

def _gated_masks(spec, shape):
    ceiling_probs, _p, _v3, _v1, baseline, ridge = _uniform_relief_scenario(spec, shape)
    land = get_boundary_mask(spec, shape, LAND_BOUNDARY_PATH)
    artcc = get_boundary_mask(spec, shape, CONUS_BOUNDARY_PATH)
    mountainous = (
        ((ridge - baseline) >= MOUNTAINOUS_RELIEF_THRESHOLD_FT)
        & land
        & (baseline >= MIN_BASELINE_ELEVATION_FT)
        & artcc
    )
    return land, artcc, mountainous


def test_closing_reaches_outside_the_gates_and_the_remask_takes_it_back():
    """
    The mechanism, stated directly: the closing DOES spill across every
    gate (that is what a closing does -- it fills gaps, and it cannot
    know what made them), and re-applying the gates afterwards is what
    removes the spill. The first assertion is the control: if the
    closing ever stopped spilling, the second would pass for the wrong
    reason.
    """
    land, artcc, mountainous = _gated_masks(SUPERIOR_SPEC, SUPERIOR_SHAPE)
    closed = close_mask(mountainous, cell_dimensions_km(SUPERIOR_SPEC, SUPERIOR_SHAPE), RADIUS_NM)

    spilled_water = int((closed & ~land).sum())
    spilled_outside_artcc = int((closed & ~artcc).sum())
    spilled_flat = int((closed & ~mountainous).sum())
    print(
        f"\n[spill] closing adds {spilled_water} water cells, {spilled_outside_artcc} outside ARTCC, "
        f"{spilled_flat} non-mountainous"
    )
    assert spilled_water > 0, "the closing didn't cross water here, so the re-mask can't be tested"
    assert spilled_outside_artcc > 0, "the closing didn't cross the ARTCC boundary here"

    remasked = closed & land & artcc & mountainous
    assert not (remasked & ~land).any(), "water survived the re-mask"
    assert not (remasked & ~artcc).any(), "ground outside the ARTCC boundary survived the re-mask"
    assert not (remasked & ~mountainous).any(), "non-mountainous ground survived the re-mask"


def test_closing_does_not_invent_mountainous_terrain_across_a_flat_gap():
    """
    The gate that is easiest to forget, and the reason the re-mask is
    three lines rather than one: two ranges either side of a flat valley
    ~69 nm wide, which is inside what a 50 nm closing bridges (a closing
    joins gaps up to twice its radius). The valley is not mountainous
    and must not be drawn as though it were.

    The control is the vector path, which does exactly that -- one
    polygon covering both ranges AND the valley between them.
    """
    spec = GridSpec(west=-108.0, north=42.0, dx=0.025, dy=-0.025)
    shape = (200, 300)
    baseline = np.full(shape, 5000.0)
    ridge = np.full(shape, 5000.0)          # flat: relief 0 everywhere...
    ridge[40:160, 40:100] = 8000.0          # ...except a western range...
    ridge[40:160, 160:220] = 8000.0         # ...and an eastern one.
    ceiling_probs = {t: np.full(shape, 90.0) for t in CEILING_PROB_THRESHOLDS_FT}
    zeros = np.zeros(shape)

    valley_lon = spec.west + 130 * spec.dx
    valley_lat = spec.north + 100 * spec.dy

    results = {}
    original = mtn_obsc.USE_RASTER_CLOSING_MTNOBSC
    try:
        for raster in (True, False):
            mtn_obsc.USE_RASTER_CLOSING_MTNOBSC = raster
            results[raster] = polygonize_mtn_obsc_grid(
                ceiling_probs, zeros, zeros, zeros, baseline, ridge, spec, VALID_DATE, 6,
                threshold_pct=50.0, neighborhood_radius_nm=RADIUS_NM, min_area_sq_mi=3000.0,
            )
    finally:
        mtn_obsc.USE_RASTER_CLOSING_MTNOBSC = original

    assert _covers(results[False], valley_lat, valley_lon), (
        "the vector closing no longer bridges the valley, so this test has no control"
    )
    assert not _covers(results[True], valley_lat, valley_lon), (
        "the raster closing filled a flat valley the relief gate excluded -- the mountainous "
        "re-mask is missing or ineffective"
    )
    assert len(results[True]["features"]) == 2, (
        f"expected the two ranges to stay separate, got {len(results[True]['features'])} polygons"
    )


# ---------------------------------------------------------------------------
# Output stability for the closing path.
#
# tests/fixtures/mtn_obsc_golden.geojson -- the older fixture -- cannot
# see either of these changes: its scenario runs at radius 0, so the
# closing is a no-op, and it hands polygonize_mtn_obsc_grid() terrain
# arrays directly, so nothing in fetch_terrain.py can reach it. It is
# byte-identical before and after, which is correct but not coverage.
# This fixture is the one that exercises the closing.
# ---------------------------------------------------------------------------

CLOSING_GOLDEN_PATH = Path(__file__).resolve().parent / "fixtures" / "mtn_obsc_golden_closing.geojson"


def test_closing_path_output_is_stable():
    """
    Pins the raster closing's output on the Lake Superior scenario at
    50 nm -- feature count, geometry structure, properties, and
    coordinates to 1e-9 degrees.

    Captured from the raster path as first written, so unlike the older
    fixture this one has no pre-change baseline to compare against; its
    job is to make the NEXT change to the closing visible. Regenerate it
    deliberately (and say so) if the closing is meant to move.
    """
    golden = json.loads(CLOSING_GOLDEN_PATH.read_text())
    current = json.loads(json.dumps(_polygonize(SUPERIOR_SPEC, SUPERIOR_SHAPE, raster_closing=True)))

    assert len(current["features"]) == len(golden["features"]), (
        f"feature count changed: {len(golden['features'])} -> {len(current['features'])}"
    )
    for index, (got, want) in enumerate(zip(current["features"], golden["features"])):
        assert got["properties"] == want["properties"], f"feature {index} properties changed"
        assert got["geometry"]["type"] == want["geometry"]["type"], f"feature {index} type changed"
        got_ring = np.asarray(got["geometry"]["coordinates"][0], dtype=float)
        want_ring = np.asarray(want["geometry"]["coordinates"][0], dtype=float)
        assert got_ring.shape == want_ring.shape, (
            f"feature {index} vertex count changed: {want_ring.shape[0]} -> {got_ring.shape[0]}"
        )
        assert np.allclose(got_ring, want_ring, atol=1e-9, rtol=0), f"feature {index} geometry moved"


# ---------------------------------------------------------------------------
# Water-corrected baseline (pipeline/fetch_terrain.py).
#
# The land mask is applied on the MOSAIC grid (30 arcsec), which is the
# grid both baseline averaging steps actually run on -- not the 0.025 deg
# output grid, which is 3x coarser and could not tell which cells inside
# an output block are land.
# ---------------------------------------------------------------------------

def _terrain_scenario(monkeypatch, mosaic, land):
    """Runs compute_output_grids against a stubbed land mask."""
    import pipeline.boundaries as boundaries
    import pipeline.fetch_terrain as fetch_terrain

    monkeypatch.setattr(boundaries, "get_boundary_mask", lambda spec, shape, path: land)
    mosaic_deg = 1 / 120.0                                  # 30 arcsec, as in production
    spec = GridSpec(west=-100.0, north=45.0, dx=mosaic_deg, dy=-mosaic_deg)
    rows, cols = mosaic.shape
    bounds = (spec.west, spec.north - rows * mosaic_deg, spec.west + cols * mosaic_deg, spec.north)
    return fetch_terrain.compute_output_grids(
        mosaic, spec, output_resolution_deg=3 * mosaic_deg, output_bounds=bounds
    )


def test_baseline_averages_over_land_only(monkeypatch):
    """
    A block half water at 0 ft and half land at 1,000 ft has a baseline
    of 1,000 ft, not 500. The plain mean is what put false relief along
    every coast and large lake: relief is ridge minus baseline, so
    averaging in the water surface inflates it everywhere the two meet.
    """
    shape = (12, 12)
    mosaic = np.zeros(shape, dtype=np.float32)
    land = np.zeros(shape, dtype=bool)
    # Each output block is 3x3 mosaic cells. Make the middle block
    # column exactly half water / half land by splitting on a block
    # boundary one cell in.
    mosaic[:, 4:] = 1000.0
    land[:, 4:] = True

    baseline, _ridge, _spec = _terrain_scenario(monkeypatch, mosaic, land)

    # Block column 1 covers mosaic columns 3,4,5: one water cell, two land.
    # The plain mean would be 667; the masked mean is 1,000.
    straddling = float(baseline[0, 1])
    assert abs(straddling - 1000.0) < 1.0, (
        f"baseline over a part-water block is {straddling:.0f} ft; expected ~1000 (land only), "
        "not a mean dragged down by the water surface"
    )


def test_baseline_is_nan_where_a_block_has_no_land(monkeypatch):
    """
    No land in the block means the block is water, and water cannot be
    mountainous. NaN rather than a substituted elevation, so the relief
    gate rejects it without special-casing -- substituting 0 ft would
    hand the gate a plausible sea-level baseline under whatever ridge
    height the max filter reached from the nearest shore, which is the
    false relief this change removes.
    """
    import pipeline.fetch_terrain as fetch_terrain

    shape = (12, 12)
    mosaic = np.zeros(shape, dtype=np.float32)
    land = np.zeros(shape, dtype=bool)
    mosaic[:, 6:] = 1000.0
    land[:, 6:] = True

    baseline, _ridge, _spec = _terrain_scenario(monkeypatch, mosaic, land)

    assert baseline[0, 0] == fetch_terrain.NO_LAND_BASELINE_SENTINEL, (
        "an all-water block should carry the no-land sentinel"
    )
    assert baseline[0, 3] == 1000, "an all-land block should carry its real elevation"


def test_the_no_land_sentinel_loads_back_as_nan(tmp_path, monkeypatch):
    """
    The sentinel exists only because the on-disk format is int16, which
    has no NaN. It must never escape the loader -- every consumer sees a
    real elevation or NaN, never -32768 ft.
    """
    import pipeline.fetch_terrain as fetch_terrain

    path = tmp_path / "terrain_grid.npz"
    baseline = np.array([[1000, fetch_terrain.NO_LAND_BASELINE_SENTINEL]], dtype=np.int16)
    np.savez_compressed(
        path,
        baseline_elevation_ft=baseline,
        ridge_elevation_ft=np.array([[3000, 3000]], dtype=np.int16),
        west=-100.0, north=45.0, dx=0.025, dy=-0.025, terrain_radius_nm=12.0,
    )

    grids, _spec, _radius = fetch_terrain.load_terrain_grid(str(path))
    loaded = grids["baseline_elevation_ft"]
    assert loaded[0, 0] == 1000.0
    assert np.isnan(loaded[0, 1]), "the sentinel must come back as NaN, not as an elevation"


def test_nan_baseline_is_not_mountainous():
    """
    The other half of the NaN contract: a cell with no baseline is
    rejected by the relief gate rather than crashing, warning, or
    sneaking through. Half the grid is NaN; none of it may be drawn.
    """
    spec = GridSpec(west=-108.0, north=42.0, dx=0.025, dy=-0.025)
    shape = (200, 300)
    # Both blocks kept clear of the array edges: a region touching the
    # border contours open, and grid_to_polygons closes it by joining
    # the ends -- an artefact that has nothing to do with what is being
    # tested here.
    baseline = np.full(shape, np.nan)
    baseline[40:160, 40:120] = 5000.0           # real land, real baseline
    ridge = np.full(shape, 5000.0)
    ridge[40:160, 40:120] = 8000.0              # relief 3000 ft -> mountainous
    ridge[40:160, 180:260] = 9000.0             # would look like 9000 ft of relief
                                                # if a NaN baseline were read as 0
    ceiling_probs = {t: np.full(shape, 90.0) for t in CEILING_PROB_THRESHOLDS_FT}
    zeros = np.zeros(shape)

    fc = polygonize_mtn_obsc_grid(
        ceiling_probs, zeros, zeros, zeros, baseline, ridge, spec, VALID_DATE, 6,
        threshold_pct=50.0, neighborhood_radius_nm=0.0, min_area_sq_mi=3000.0,
    )

    land_lat = spec.north + 100 * spec.dy
    assert _covers(fc, land_lat, spec.west + 80 * spec.dx), (
        "the block with a real baseline and real relief should still be drawn"
    )
    assert not _covers(fc, land_lat, spec.west + 220 * spec.dx), (
        "a cell with a NaN (no-land) baseline was drawn as mountainous"
    )


if __name__ == "__main__":
    import pytest

    pytest.main([__file__, "-v", "-s"])
