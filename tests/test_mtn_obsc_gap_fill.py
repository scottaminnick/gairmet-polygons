"""
tests/test_mtn_obsc_gap_fill.py
--------------------------------
The enclosed-gap fill in polygonize_mtn_obsc_grid() (pipeline.polygons.
fill_enclosed_gaps).

WHAT WENT WRONG: the mountainous re-mask after the raster closing --
right, and tested, at the scale of the Central Valley -- also punches
out every small valley floor inside a mountain mass. On the 13/03Z
cycle the Appalachian polygon carried 39 holes with 234 vertices
against a 159-vertex outline; a downstream converter crashed on the
point count, and none of that detail is visible on aviationweather.gov.
Gaps smaller than the minimum polygon area are now filled, on the
raster, after the re-mask.

The contract these pin, each with a control:

  - an enclosed gap under min_area is filled; one at or over it is not
  - a gap that reaches the outside is never filled, whatever its size
  - the fill happens AFTER the re-mask, so it cannot re-admit water or
    the far side of a real valley -- the Central Valley test in
    test_mtn_obsc_raster_closing.py still holds
  - the mountainous-area figure does not count filled ground
"""

import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.polygons import GridSpec, cell_areas_sq_mi, fill_enclosed_gaps  # noqa: E402

VALID_DATE = datetime(2026, 9, 13, 3)


# ---------------------------------------------------------------------------
# The raster operation on its own (no shapely, no boundaries).
# ---------------------------------------------------------------------------

def _block_mask(shape, holes):
    """A solid block with rectangular holes cut out. holes: [(r0, r1, c0, c1)]."""
    mask = np.zeros(shape, dtype=bool)
    mask[10:-10, 10:-10] = True
    for r0, r1, c0, c1 in holes:
        mask[r0:r1, c0:c1] = False
    return mask


def test_small_enclosed_gap_is_filled_and_large_one_is_kept():
    shape = (200, 200)
    small = (50, 54, 50, 54)        # 16 cells
    large = (100, 160, 100, 160)    # 3,600 cells
    mask = _block_mask(shape, [small, large])
    areas = np.ones((shape[0], 1))  # 1 sq mi per cell, so areas are cell counts

    filled = fill_enclosed_gaps(mask, areas, max_area_sq_mi=100)

    assert filled[50:54, 50:54].all(), "the 16-cell gap was not filled"
    assert not filled[100:160, 100:160].any(), "the 3,600-cell gap was filled -- it is over the limit"
    # Nothing else moved: the only difference is the small gap.
    changed = filled ^ mask
    assert changed.sum() == 16, f"{changed.sum()} cells changed, expected exactly the 16 of the small gap"


def test_a_gap_open_to_the_outside_is_never_filled():
    """
    A notch in the outline is not a hole, however small. The control is
    the same-sized gap fully enclosed, which IS filled.
    """
    shape = (100, 100)
    mask = _block_mask(shape, [])
    mask[10:14, 40:44] = False       # 4x4 notch touching the block's top edge...
    mask[0:10, 40:44] = False        # ...which is itself outside the block: open to the array edge
    mask[50:54, 50:54] = False       # control: 4x4 enclosed gap

    filled = fill_enclosed_gaps(mask, np.ones((shape[0], 1)), max_area_sq_mi=100)

    assert filled[50:54, 50:54].all(), "control failed: the enclosed 16-cell gap was not filled"
    assert not filled[10:14, 40:44].any(), "a notch open to the outside was filled as if it were a hole"


def test_threshold_is_strict_and_uses_real_cell_areas():
    """
    Holes STRICTLY smaller than the limit fill, and the comparison is in
    square miles from the per-row cell areas, not in cell counts.
    """
    shape = (60, 60)
    gap = (slice(20, 24), slice(20, 24))
    mask = _block_mask(shape, [(20, 24, 20, 24)])    # 16 cells
    areas = np.full((shape[0], 1), 2.0)              # 2 sq mi per cell -> 32 sq mi gap

    at_limit = fill_enclosed_gaps(mask, areas, max_area_sq_mi=32)[gap]
    assert not at_limit.any(), "a 32 sq mi gap filled at a 32 sq mi limit -- the comparison should be strict"

    over_limit = fill_enclosed_gaps(mask, areas, max_area_sq_mi=33)[gap]
    assert over_limit.all(), "a 32 sq mi gap did not fill at a 33 sq mi limit"

    # In cell counts the same gap is 16, so a 17 limit in 1 sq mi cells
    # fills it -- the area array, not the count, is what is compared.
    assert fill_enclosed_gaps(mask, np.ones((shape[0], 1)), max_area_sq_mi=17)[gap].all()


def test_zero_limit_and_empty_mask_are_no_ops():
    shape = (40, 40)
    mask = _block_mask(shape, [(15, 18, 15, 18)])
    areas = np.ones((shape[0], 1))
    assert np.array_equal(fill_enclosed_gaps(mask, areas, max_area_sq_mi=0), mask)
    empty = np.zeros(shape, dtype=bool)
    assert np.array_equal(fill_enclosed_gaps(empty, areas, max_area_sq_mi=1e9), empty)


def test_diagonally_enclosed_gap_counts_as_enclosed():
    """
    Foreground 8-connected, background 4-connected: a ring of hazard
    cells joined only at their corners still encloses its interior, which
    is how marching squares would contour it too.
    """
    mask = np.zeros((9, 9), dtype=bool)
    # A diamond ring of cells touching corner to corner.
    for i in range(5):
        mask[i, 4 - i] = mask[i, 4 + i] = mask[8 - i, 4 - i] = mask[8 - i, 4 + i] = True
    assert not mask[4, 4], "the centre must start empty"
    filled = fill_enclosed_gaps(mask, np.ones((9, 1)), max_area_sq_mi=100)
    assert filled[4, 4], "the interior of a corner-connected ring was not treated as enclosed"


# ---------------------------------------------------------------------------
# Through polygonize_mtn_obsc_grid(): the re-mask ordering and the
# mountainous-area figure. shapely and the boundary files are needed.
# ---------------------------------------------------------------------------

shapely = pytest.importorskip("shapely")
from shapely.geometry import Point  # noqa: E402
from shapely.geometry import shape as shapely_shape  # noqa: E402

import pipeline.hazards.mtn_obsc as mtn_obsc  # noqa: E402
from pipeline.hazards.mtn_obsc import CEILING_PROB_THRESHOLDS_FT, polygonize_mtn_obsc_grid  # noqa: E402


def _covers(fc, lat, lon):
    point = Point(lon, lat)
    return any(shapely_shape(f["geometry"]).contains(point) for f in fc["features"])


def _hollow_range_scenario():
    """
    One mountain mass over central Pennsylvania / West Virginia with two
    flat valleys inside it: a small one (~200 sq mi, well under the
    3,000 sq mi minimum) and a large one (~9,000 sq mi, over it).
    Uniform 90% ceiling probability so the shapes are pure terrain.
    """
    spec = GridSpec(west=-82.0, north=42.0, dx=0.025, dy=-0.025)
    shape = (200, 200)                      # ~5 deg square, all inland US
    baseline = np.full(shape, 1200.0)
    ridge = np.full(shape, 1200.0)          # flat...
    ridge[20:180, 20:180] = 3200.0          # ...except a broad range (2,000 ft relief)
    small = (60, 70, 60, 74)                # 10 x 14 cells ~ 0.25 x 0.35 deg ~ 200 sq mi
    large = (100, 160, 90, 170)             # 60 x 80 cells ~ 1.5 x 2 deg ~ 9,000 sq mi
    for r0, r1, c0, c1 in (small, large):
        ridge[r0:r1, c0:c1] = 1200.0        # valley floor: zero relief
    ceiling_probs = {t: np.full(shape, 90.0) for t in CEILING_PROB_THRESHOLDS_FT}
    zeros = np.zeros(shape)

    def centre(box):
        r0, r1, c0, c1 = box
        return spec.north + ((r0 + r1) / 2) * spec.dy, spec.west + ((c0 + c1) / 2) * spec.dx

    return spec, shape, (ceiling_probs, zeros, zeros, zeros, baseline, ridge), centre(small), centre(large)


def _polygonize(spec, grids, **kwargs):
    original = mtn_obsc.USE_RASTER_CLOSING_MTNOBSC
    mtn_obsc.USE_RASTER_CLOSING_MTNOBSC = True
    try:
        return polygonize_mtn_obsc_grid(
            *grids, spec, VALID_DATE, 3,
            threshold_pct=50.0, neighborhood_radius_nm=50.0, min_area_sq_mi=3000.0, **kwargs,
        )
    finally:
        mtn_obsc.USE_RASTER_CLOSING_MTNOBSC = original


def test_small_valley_is_filled_and_large_valley_is_kept():
    spec, shape, grids, small_pt, large_pt = _hollow_range_scenario()
    fc = _polygonize(spec, grids)
    assert fc["features"], "no polygons at all -- the scenario is broken"

    assert _covers(fc, *small_pt), "a ~200 sq mi valley inside the range was left as a hole"
    assert not _covers(fc, *large_pt), "a ~9,000 sq mi valley was filled -- it is over the minimum area"

    # And the polygon carries no interior rings at all for the small one.
    holes = sum(len(f["geometry"]["coordinates"]) - 1 for f in fc["features"]
                if f["geometry"]["type"] == "Polygon")
    assert holes == 1, f"expected exactly one hole (the large valley), found {holes}"


def test_filled_valleys_do_not_count_as_mountainous():
    """
    The readout beside RELIEF reports land passing the relief gate. A
    filled valley is inside the polygon and still not mountainous.
    """
    spec, shape, grids, _small, _large = _hollow_range_scenario()
    ridge, baseline = grids[5], grids[4]
    fc = _polygonize(spec, grids)

    # Independent count: cells with real relief, in square miles.
    mountainous = (ridge - baseline) >= 500.0
    expected = float((cell_areas_sq_mi(spec, shape) * mountainous).sum())
    got = fc["mountainous_area_sq_mi"]
    # The real gates (land, ARTCC) trim the edges of this inland window
    # slightly; the tolerance is far smaller than the ~200 sq mi valley.
    assert abs(got - expected) < 0.05 * expected, f"mountainous area {got} vs relief-only {expected:.0f}"
    assert got < expected + 100, "the mountainous-area figure appears to include the filled valley"
