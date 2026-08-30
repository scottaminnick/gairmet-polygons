"""
tests/test_mtn_obsc_relief.py
--------------------------------
MOUNTAINOUS_RELIEF_THRESHOLD_FT as a forecaster-settable parameter.

The threshold decides what counts as mountainous at all, so it sits
upstream of every other MTN OBSC control: clearance, radius and min-area
all act on terrain the relief gate has already admitted. It is applied at
RECOMPUTE time -- once, in polygonize_mtn_obsc_grid, against ridge minus
baseline from the cached grid -- which is what makes a live slider
possible without re-fetching terrain. fetch_terrain.py never reads it,
and that separation is the first thing pinned below, because a later
change that moved the gate upstream would break the slider silently: the
control would still move, the output would just stop responding.

The mountainous-area figure the viewer reports is checked here too. It is
a FeatureCollection foreign member rather than a feature property,
because at a high enough threshold there are no features to hang it on --
which is exactly the reading a forecaster needs to see.
"""

import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import fetch_terrain  # noqa: E402
from pipeline.hazards import mtn_obsc  # noqa: E402
from pipeline.hazards.mtn_obsc import (  # noqa: E402
    CEILING_PROB_THRESHOLDS_FT,
    MOUNTAINOUS_RELIEF_THRESHOLD_FT,
    polygonize_mtn_obsc_grid,
)
from pipeline.polygons import GridSpec, cell_areas_sq_mi  # noqa: E402

VALID_DATE = datetime(2026, 7, 16, 12)

# Inland Wyoming: far enough from any coast or ARTCC edge that the land
# and boundary masks are not what these tests are measuring.
SPEC = GridSpec(west=-108.0, north=42.0, dx=0.025, dy=-0.025)
SHAPE = (200, 400)


# grid_to_polygons closes an open contour by joining its endpoints, so a
# region running to the array border is swallowed rather than traced. Every
# synthetic feature here is therefore inset from the edge -- an ordinary
# trap in this codebase's tests, not a property of the relief gate.
INSET = 20


def _relief_field(relief_ft):
    """Flat terrain with `relief_ft` over an inset interior block."""
    field = np.zeros(SHAPE)
    field[INSET:-INSET, INSET:-INSET] = relief_ft
    return field


def _inset(field):
    """Zero the border of a relief field -- see INSET."""
    out = np.zeros_like(field)
    out[INSET:-INSET, INSET:-INSET] = field[INSET:-INSET, INSET:-INSET]
    return out


def _polygonize(relief_field_ft, **kwargs):
    field = _relief_field(relief_field_ft) if np.isscalar(relief_field_ft) else relief_field_ft
    baseline = np.full(SHAPE, 5000.0)
    ridge = baseline + field
    ceiling_probs = {t: np.full(SHAPE, 90.0) for t in CEILING_PROB_THRESHOLDS_FT}
    zeros = np.zeros(SHAPE)
    return polygonize_mtn_obsc_grid(
        ceiling_probs, zeros, zeros, zeros, baseline, ridge, SPEC, VALID_DATE, 6,
        threshold_pct=50.0, neighborhood_radius_nm=0.0, min_area_sq_mi=0.0, **kwargs,
    )


# ---------------------------------------------------------------------------
# Where the threshold is applied -- the property the slider depends on.
# ---------------------------------------------------------------------------

def test_the_threshold_is_not_baked_into_the_cached_terrain_grid():
    """
    A live slider is only possible because the cached grid stores ridge
    and baseline ELEVATIONS, not a pre-computed mountainous mask. If the
    gate ever moves into the fetch, the slider keeps moving and the map
    stops changing -- a failure with no visible symptom.
    """
    source = Path(fetch_terrain.__file__).read_text()
    assert "MOUNTAINOUS_RELIEF" not in source, (
        "fetch_terrain.py applies the relief threshold; it would then be baked into "
        "the cached grid and the live slider could not change it"
    )

    grids = ("baseline_elevation_ft", "ridge_elevation_ft")
    for name in grids:
        assert name in source, f"the cached grid no longer stores {name}"


def test_the_default_is_unchanged_so_behaviour_moves_only_when_someone_moves_it():
    assert MOUNTAINOUS_RELIEF_THRESHOLD_FT == 500.0
    import inspect

    default = inspect.signature(polygonize_mtn_obsc_grid).parameters["mountainous_relief_ft"].default
    assert default == MOUNTAINOUS_RELIEF_THRESHOLD_FT


# ---------------------------------------------------------------------------
# The gate itself.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("relief_ft, threshold_ft, expect_hazard", [
    (900.0, 500.0, True),      # comfortably over
    (900.0, 1000.0, False),    # raised past it
    (1000.0, 1000.0, True),    # the boundary is inclusive (>=)
    (999.0, 1000.0, False),
])
def test_terrain_is_mountainous_exactly_when_relief_meets_the_threshold(
    relief_ft, threshold_ft, expect_hazard
):
    fc = _polygonize(relief_ft, mountainous_relief_ft=threshold_ft)
    assert bool(fc["features"]) is expect_hazard


def test_raising_the_threshold_only_ever_shrinks_the_mountainous_area():
    """
    Monotonicity. The gate is a single comparison, so a higher threshold
    can only remove cells -- if closing or the area filter ever made the
    mask grow with the threshold, the slider would stop being readable as
    "how strict am I being".
    """
    # A ramp, so each threshold cuts the mask at a different place rather
    # than flipping the whole grid at once.
    ramp = _inset(np.linspace(0.0, 3000.0, SHAPE[1])[None, :].repeat(SHAPE[0], axis=0))

    areas = [
        _polygonize(ramp, mountainous_relief_ft=t)["mountainous_area_sq_mi"]
        for t in (500.0, 1000.0, 1500.0, 2000.0, 2500.0)
    ]
    assert areas == sorted(areas, reverse=True), f"not monotonic: {areas}"
    assert areas[0] > areas[-1], "the ramp should actually be cut by these thresholds"


# ---------------------------------------------------------------------------
# The reported figure.
# ---------------------------------------------------------------------------

def test_the_reported_area_matches_the_mask_it_claims_to_measure():
    """
    Checked against an independently summed cell-area grid, not against a
    previously recorded number: the figure is the one thing on the panel a
    forecaster has no other way to sanity-check.
    """
    ramp = _inset(np.linspace(0.0, 3000.0, SHAPE[1])[None, :].repeat(SHAPE[0], axis=0))
    fc = _polygonize(ramp, mountainous_relief_ft=1500.0)

    expected = float((cell_areas_sq_mi(SPEC, SHAPE) * (ramp >= 1500.0)).sum())
    assert fc["mountainous_area_sq_mi"] == pytest.approx(round(expected), abs=1)


def test_the_area_is_reported_even_when_no_polygon_survives():
    """
    The case the figure exists for. A forecaster winding the threshold up
    until the hazard disappears needs to see the mask shrinking on the way
    -- "0 polygons" alone does not distinguish "too strict" from "broken".
    """
    fc = _polygonize(400.0, mountainous_relief_ft=3000.0)
    assert fc["features"] == []
    assert fc["mountainous_area_sq_mi"] == 0


def test_the_threshold_travels_on_the_polygons_it_produced():
    """
    Provenance. The viewer re-seeds an hour's sliders from its scheduled
    snapshot's properties, so a polygon that does not carry the threshold
    it was cut at cannot be reset to it.
    """
    fc = _polygonize(2000.0, mountainous_relief_ft=1250.0)
    assert fc["features"]
    for feature in fc["features"]:
        assert feature["properties"]["mountainous_relief_ft"] == 1250.0


def test_the_area_is_a_collection_member_not_a_feature_property():
    """
    It describes the mask, not any one polygon. Putting it on features
    would both duplicate it and make it unreportable at zero polygons.
    """
    fc = _polygonize(2000.0, mountainous_relief_ft=1250.0)
    assert "mountainous_area_sq_mi" in fc
    for feature in fc["features"]:
        assert "mountainous_area_sq_mi" not in feature["properties"]


def test_the_generator_forwards_the_threshold():
    """generate_mtn_obsc_polygons is the pipeline entry point; a parameter
    it drops is one the scheduled run silently ignores."""
    import inspect

    signature = inspect.signature(mtn_obsc.generate_mtn_obsc_polygons)
    assert "mountainous_relief_ft" in signature.parameters
    source = inspect.getsource(mtn_obsc.generate_mtn_obsc_polygons)
    assert "mountainous_relief_ft=mountainous_relief_ft" in source


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
