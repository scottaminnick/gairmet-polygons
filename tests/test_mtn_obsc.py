"""
tests/test_mtn_obsc.py
------------------------
Tests every piece of pipeline/hazards/mtn_obsc.py that doesn't require
live NBM/network access -- the message-selection logic, the core
terrain-relative interpolation math, weather-type attribution, and an
end-to-end synthetic scenario for polygonize_mtn_obsc_grid(). Matches
the same scope convention as tests/test_ifr_pipeline.py and
tests/test_fetch_terrain.py: test the pure logic thoroughly with
synthetic data, don't try to mock the full NBM/cfgrib fetch chain.
"""

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.hazards.mtn_obsc import (
    CEILING_PROB_THRESHOLDS_FT,
    MOUNTAINOUS_RELIEF_THRESHOLD_FT,
    THRESHOLD_ANCHORS_FT,
    _determine_weather_type,
    find_message_excluding,
    interpolate_terrain_relative_probability,
    polygonize_mtn_obsc_grid,
)
from pipeline.grid_spec import GridSpec


# ---------------------------------------------------------------------------
# find_message_excluding
# ---------------------------------------------------------------------------

def test_find_message_excluding_isolates_deterministic_row():
    rows = [
        {"_raw_line": "15:0:d=2026071212:CEIL:cloud ceiling:6 hour fcst:"},
        {"_raw_line": "16:100:d=2026071212:CEIL:cloud ceiling:6 hour fcst:prob <152.4"},
        {"_raw_line": "17:200:d=2026071212:CEIL:cloud ceiling:6 hour fcst:prob <304.8"},
    ]
    result = find_message_excluding(rows, exclude=["prob"], variable="CEIL", level="cloud ceiling")
    assert result is rows[0]


def test_find_message_excluding_raises_on_zero_matches():
    rows = [{"_raw_line": "16:100:d=2026071212:CEIL:cloud ceiling:6 hour fcst:prob <152.4"}]
    try:
        find_message_excluding(rows, exclude=["prob"], variable="CEIL", level="cloud ceiling")
        assert False, "should have raised"
    except ValueError:
        pass


def test_find_message_excluding_raises_on_multiple_matches():
    rows = [
        {"_raw_line": "15:0:d=2026071212:CEIL:cloud ceiling:6 hour fcst:"},
        {"_raw_line": "21:400:d=2026071212:CEIL:cloud ceiling:9 hour fcst:"},
    ]
    try:
        find_message_excluding(rows, exclude=["prob"], variable="CEIL", level="cloud ceiling")
        assert False, "should have raised"
    except ValueError:
        pass


# ---------------------------------------------------------------------------
# interpolate_terrain_relative_probability -- the core math
# ---------------------------------------------------------------------------

def _uniform_prob_grids(probs_by_threshold: dict[int, float], shape=(3, 3)) -> dict[int, np.ndarray]:
    """Builds prob grids where every cell has the same value -- isolates
    the interpolation math from any spatial variation for these tests."""
    return {t: np.full(shape, p, dtype=np.float64) for t, p in probs_by_threshold.items()}


def test_interpolation_matches_hand_worked_example():
    """
    Direct check against the hand-worked example from design
    discussion: critical_ceiling_agl=2750 ft, bracketed by the REAL
    2000/3000 ft thresholds (not 1000/3000, which an earlier prototype
    assumed before the real threshold set -- including 2000 ft -- was
    confirmed). weight = (2750-2000)/(3000-2000) = 0.75.
    """
    probs = _uniform_prob_grids({500: 10.0, 1000: 31.0, 2000: 50.0, 3000: 74.0, 6600: 95.0})
    critical = np.full((3, 3), 2750.0)
    derived, is_extrap = interpolate_terrain_relative_probability(critical, probs)
    expected = 50.0 + 0.75 * (74.0 - 50.0)  # = 68.0
    assert np.allclose(derived, expected)
    assert not is_extrap.any()


def test_interpolation_exact_at_a_real_threshold():
    """At exactly a published threshold, interpolation should return
    that threshold's own value with no error from the bracket math."""
    probs = _uniform_prob_grids({500: 10.0, 1000: 31.0, 2000: 50.0, 3000: 74.0, 6600: 95.0}, shape=(2, 2))
    critical = np.full((2, 2), 1000.0)
    derived, is_extrap = interpolate_terrain_relative_probability(critical, probs)
    assert np.allclose(derived, 31.0)
    assert not is_extrap.any()


def test_interpolation_zero_anchor_at_zero_critical_ceiling():
    """At critical_ceiling_agl=0 (the free anchor), probability should
    be 0 -- ceiling can't be below the ground."""
    probs = _uniform_prob_grids({500: 10.0, 1000: 31.0, 2000: 50.0, 3000: 74.0, 6600: 95.0}, shape=(2, 2))
    critical = np.full((2, 2), 0.0)
    derived, is_extrap = interpolate_terrain_relative_probability(critical, probs)
    assert np.allclose(derived, 0.0)
    assert not is_extrap.any()


def test_interpolation_does_not_extrapolate_past_highest_threshold():
    """
    Above 6,600 ft (the highest real published threshold), this must
    NOT project a line beyond the data -- it should report the 6,600 ft
    threshold's own probability as a floor, and flag is_extrapolated.
    """
    probs = _uniform_prob_grids({500: 10.0, 1000: 31.0, 2000: 50.0, 3000: 74.0, 6600: 95.0}, shape=(2, 2))
    critical = np.full((2, 2), 10000.0)  # well above 6,600 ft
    derived, is_extrap = interpolate_terrain_relative_probability(critical, probs)
    assert np.allclose(derived, 95.0)  # the 6,600 ft threshold's own value, not extrapolated further
    assert is_extrap.all()


def test_interpolation_varies_correctly_per_pixel():
    """
    Confirms the vectorized math genuinely operates per-pixel, not just
    on a uniform grid -- two cells with different critical_ceiling_agl
    AND different underlying probability values should each get their
    own correctly-computed answer, not one shared result.
    """
    shape = (1, 2)
    probs = {
        500: np.array([[5.0, 50.0]]),
        1000: np.array([[10.0, 60.0]]),
        2000: np.array([[20.0, 70.0]]),
        3000: np.array([[30.0, 80.0]]),
        6600: np.array([[40.0, 90.0]]),
    }
    critical = np.array([[1000.0, 3000.0]])  # cell 0 exactly at 1000ft, cell 1 exactly at 3000ft
    derived, is_extrap = interpolate_terrain_relative_probability(critical, probs)
    assert np.allclose(derived[0, 0], 10.0)
    assert np.allclose(derived[0, 1], 80.0)
    assert not is_extrap.any()


# ---------------------------------------------------------------------------
# _determine_weather_type -- CLDS fallback + PCPN/BR/FG precedence
# ---------------------------------------------------------------------------

def _make_square_polygon():
    from shapely.geometry import box

    return box(-105.0, 39.0, -104.0, 40.0)


def test_weather_type_clds_fallback_when_nothing_else_applies():
    """The genuinely new case IFR never needed: plain clouds against
    terrain, no precip or visibility restriction anywhere -- should
    fall back to CLDS, not return None/empty the way IFR's
    weather_type sometimes does."""
    grid_spec = GridSpec(west=-106.0, north=41.0, dx=0.1, dy=-0.1)
    shape = (20, 20)
    precip_grid = np.zeros(shape)
    vis3_grid = np.zeros(shape)
    vis1_grid = np.zeros(shape)
    result = _determine_weather_type(_make_square_polygon(), grid_spec, precip_grid, vis3_grid, vis1_grid, 50.0)
    assert result == "CLDS"


def test_weather_type_pcpn_detected():
    grid_spec = GridSpec(west=-106.0, north=41.0, dx=0.1, dy=-0.1)
    shape = (20, 20)
    precip_grid = np.full(shape, 80.0)
    vis3_grid = np.zeros(shape)
    vis1_grid = np.zeros(shape)
    result = _determine_weather_type(_make_square_polygon(), grid_spec, precip_grid, vis3_grid, vis1_grid, 50.0)
    assert result == "PCPN"


def test_weather_type_pcpn_wins_over_fg_on_overlap():
    """Same precedence rule already confirmed for IFR: where PCPN and
    FG-level visibility restriction coincide, PCPN wins -- FG only
    applies where vis<1SM crosses threshold AND precip does not."""
    grid_spec = GridSpec(west=-106.0, north=41.0, dx=0.1, dy=-0.1)
    shape = (20, 20)
    precip_grid = np.full(shape, 80.0)
    vis3_grid = np.full(shape, 80.0)
    vis1_grid = np.full(shape, 80.0)  # same cells as precip -- PCPN should win here
    result = _determine_weather_type(_make_square_polygon(), grid_spec, precip_grid, vis3_grid, vis1_grid, 50.0)
    assert result == "PCPN/BR"
    assert "FG" not in result


# ---------------------------------------------------------------------------
# polygonize_mtn_obsc_grid -- end-to-end synthetic scenario
# ---------------------------------------------------------------------------

def test_polygonize_excludes_flat_high_elevation_area():
    """
    The mountainous-gate's whole reason for existing: a flat-but-high
    area (e.g. the high plains) with LOW relief must NOT generate a
    polygon even if ceiling probability is high there -- only genuine
    relief (ridge meaningfully above baseline) should.
    """
    shape = (30, 30)
    grid_spec = GridSpec(west=-106.0, north=41.0, dx=0.05, dy=-0.05)

    # Flat high plains: baseline and ridge nearly identical (low relief)
    baseline = np.full(shape, 4000.0)
    ridge = np.full(shape, 4000.0 + MOUNTAINOUS_RELIEF_THRESHOLD_FT / 2)  # well under the gate

    # High ceiling-crossing probability EVERYWHERE, including this flat area
    ceiling_prob_grids = {t: np.full(shape, 90.0) for t in CEILING_PROB_THRESHOLDS_FT}
    precip_grid = np.zeros(shape)
    vis3_grid = np.zeros(shape)
    vis1_grid = np.zeros(shape)

    from datetime import datetime

    result = polygonize_mtn_obsc_grid(
        ceiling_prob_grids, precip_grid, vis3_grid, vis1_grid, baseline, ridge, grid_spec,
        datetime(2026, 7, 16, 12), 6,
    )
    assert len(result["features"]) == 0  # flat area, no polygon despite high ceiling-crossing probability


def test_polygonize_generates_polygon_over_real_relief():
    """
    Same idea as the exclusion test above, but now with a genuinely
    mountainous ISLAND in the middle of otherwise-flat terrain -- a
    perfectly uniform field has no boundary anywhere for marching
    squares to contour (confirmed directly: an earlier version of this
    test used uniform relief across the whole grid and produced zero
    polygons even in a clearly-mountainous scenario, purely because
    there was no spatial transition to trace a contour along). Real
    NBM/terrain data always has spatial variation, so this only matters
    for constructing a meaningful synthetic test, not for real usage.
    """
    shape = (30, 30)
    grid_spec = GridSpec(west=-106.0, north=41.0, dx=0.05, dy=-0.05)

    baseline = np.full(shape, 4000.0)
    ridge = np.full(shape, 4000.0 + MOUNTAINOUS_RELIEF_THRESHOLD_FT / 2)  # flat surroundings, under the gate
    # A mountainous island in the middle, well past the gate
    ridge[10:20, 10:20] = 4000.0 + MOUNTAINOUS_RELIEF_THRESHOLD_FT * 3

    ceiling_prob_grids = {t: np.full(shape, 90.0) for t in CEILING_PROB_THRESHOLDS_FT}
    precip_grid = np.zeros(shape)
    vis3_grid = np.zeros(shape)
    vis1_grid = np.zeros(shape)

    from datetime import datetime

    result = polygonize_mtn_obsc_grid(
        ceiling_prob_grids, precip_grid, vis3_grid, vis1_grid, baseline, ridge, grid_spec,
        datetime(2026, 7, 16, 12), 6,
        min_area_sq_mi=0,  # small synthetic grid -- disable the area filter so the polygon isn't dropped
    )
    assert len(result["features"]) > 0
    assert result["features"][0]["properties"]["weather_type"] == "CLDS"  # no precip/vis in this synthetic scenario


if __name__ == "__main__":
    import pytest

    pytest.main([__file__, "-v"])
