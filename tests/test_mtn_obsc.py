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
    MIN_BASELINE_ELEVATION_FT,
    MOUNTAINOUS_RELIEF_THRESHOLD_FT,
    THRESHOLD_ANCHORS_FT,
    _determine_weather_type,
    _get_conus_mask,
    find_message_excluding,
    interpolate_terrain_relative_probability,
    polygonize_mtn_obsc_grid,
    prepare_mtn_obsc_grid,
)
import pipeline.hazards.mtn_obsc as mtn_obsc
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


# ---------------------------------------------------------------------------
# Bathymetry / ocean-depth exclusion (MIN_BASELINE_ELEVATION_FT) and the
# CONUS/ARTCC boundary mask (_get_conus_mask) -- both added directly in
# response to a real production run painting real relief in Mexico,
# Quebec, and open Pacific water. See the constants' own docstrings in
# mtn_obsc.py for the full real-data diagnosis behind each.
# ---------------------------------------------------------------------------

def test_polygonize_excludes_deep_ocean_bathymetry():
    """
    Direct regression test for the confirmed real case: a cell with
    deep-negative baseline_elevation_ft (real ETOPO1 ocean depth, not a
    void) and a ridge_elevation_ft at/near sea level -- meaning ALL of
    the "relief" comes from ocean DEPTH, not from any real nearby peak
    -- must not generate a polygon, even though relief alone would
    exceed MOUNTAINOUS_RELIEF_THRESHOLD_FT.
    """
    shape = (30, 30)
    grid_spec = GridSpec(west=-106.0, north=41.0, dx=0.05, dy=-0.05)

    baseline = np.full(shape, 4000.0)  # flat surroundings, won't pass the relief gate anyway
    ridge = np.full(shape, 4000.0 + MOUNTAINOUS_RELIEF_THRESHOLD_FT / 2)
    # A "deep ocean" island: real bathymetry-style negative baseline,
    # ridge at sea level -- relief exceeds the threshold, but purely
    # from depth, not from any real terrain.
    baseline[10:20, 10:20] = -1060.0  # matches the real confirmed Santa Cruz-area value
    ridge[10:20, 10:20] = 0.0

    ceiling_prob_grids = {t: np.full(shape, 90.0) for t in CEILING_PROB_THRESHOLDS_FT}
    precip_grid = np.zeros(shape)
    vis3_grid = np.zeros(shape)
    vis1_grid = np.zeros(shape)

    from datetime import datetime

    result = polygonize_mtn_obsc_grid(
        ceiling_prob_grids, precip_grid, vis3_grid, vis1_grid, baseline, ridge, grid_spec,
        datetime(2026, 7, 16, 12), 6,
        min_area_sq_mi=0,
    )
    assert len(result["features"]) == 0  # pure bathymetric relief, no real terrain -- must not generate a polygon


def test_polygonize_still_generates_polygon_when_baseline_is_positive():
    """
    Confirms MIN_BASELINE_ELEVATION_FT doesn't accidentally exclude real
    onshore relief -- same setup as the deep-ocean test above, but with
    a plausible ON-LAND baseline instead of ocean depth.
    """
    shape = (30, 30)
    grid_spec = GridSpec(west=-106.0, north=41.0, dx=0.05, dy=-0.05)

    baseline = np.full(shape, 4000.0)
    ridge = np.full(shape, 4000.0 + MOUNTAINOUS_RELIEF_THRESHOLD_FT / 2)
    baseline[10:20, 10:20] = 4000.0  # on land, real relief
    ridge[10:20, 10:20] = 4000.0 + MOUNTAINOUS_RELIEF_THRESHOLD_FT * 3

    ceiling_prob_grids = {t: np.full(shape, 90.0) for t in CEILING_PROB_THRESHOLDS_FT}
    precip_grid = np.zeros(shape)
    vis3_grid = np.zeros(shape)
    vis1_grid = np.zeros(shape)

    from datetime import datetime

    result = polygonize_mtn_obsc_grid(
        ceiling_prob_grids, precip_grid, vis3_grid, vis1_grid, baseline, ridge, grid_spec,
        datetime(2026, 7, 16, 12), 6,
        min_area_sq_mi=0,
    )
    assert len(result["features"]) > 0


def test_get_conus_mask_excludes_quebec_and_mexico_includes_conus():
    """
    Direct check against the REAL data/boundaries/artcc.json file (not a
    synthetic boundary) -- confirms the specific real coordinates from
    the actual problem run: Quebec (St. Lawrence/Laurentians) and
    Mexico (Sierra Madre) are OUTSIDE the real ARTCC boundary, while
    known real CONUS mountain locations are inside it.
    """
    grid_spec = GridSpec(west=-126.0, north=50.0, dx=0.025, dy=-0.025)
    shape = (1120, 2440)
    mask = _get_conus_mask(grid_spec, shape)

    def idx(lat, lon):
        row = round((grid_spec.north - lat) / grid_spec.dx)
        col = round((lon - grid_spec.west) / grid_spec.dx)
        return row, col

    outside_points = {
        "Quebec, St. Lawrence area": (47.65, -70.4),
        "Mexico, Sierra Madre": (24.5, -103.0),
    }
    for name, (lat, lon) in outside_points.items():
        r, c = idx(lat, lon)
        assert not mask[r, c], f"{name} should be OUTSIDE the CONUS/ARTCC boundary"

    inside_points = {
        "Denver, CO": (39.7392, -104.9903),
        "Boston Mountains, AR": (35.7, -93.5),
        "Mt. Whitney, CA": (36.5785, -118.2923),
    }
    for name, (lat, lon) in inside_points.items():
        r, c = idx(lat, lon)
        assert mask[r, c], f"{name} should be INSIDE the CONUS/ARTCC boundary"


def test_get_conus_mask_is_memoized():
    """
    Confirms the expensive union+rasterize step (several real seconds --
    see load_boundary_mask's docstring) only actually runs once across
    repeated calls with the same grid, not once per call -- the whole
    reason this memoization exists, since a real run calls this once per
    snapshot (5x) and shouldn't pay that cost 5 times over.
    """
    import pipeline.polygons as polygons_module

    call_count = {"n": 0}
    real_load_boundary_mask = polygons_module.load_boundary_mask

    def counting_load_boundary_mask(*args, **kwargs):
        call_count["n"] += 1
        return real_load_boundary_mask(*args, **kwargs)

    # Patch where mtn_obsc looks it up (it was imported by name into that
    # module's namespace), and use a distinct grid_spec/shape so this
    # test can't accidentally reuse another test's cache entry.
    import unittest.mock

    with unittest.mock.patch.object(mtn_obsc, "load_boundary_mask", side_effect=counting_load_boundary_mask):
        grid_spec = GridSpec(west=-999.0, north=999.0, dx=1.0, dy=-1.0)  # distinct, won't collide with other tests
        shape = (5, 5)
        _get_conus_mask(grid_spec, shape)
        _get_conus_mask(grid_spec, shape)
        _get_conus_mask(grid_spec, shape)

    assert call_count["n"] == 1, f"Expected exactly 1 real computation, got {call_count['n']}"


# ---------------------------------------------------------------------------
# prepare_mtn_obsc_grid -- sequential fetch orchestration
# (previously concurrent -- see prepare_mtn_obsc_grid's docstring for the
# full diagnostic story of why that was tried, then reverted)
# ---------------------------------------------------------------------------

def _fake_native_grid(fill_value: float):
    """A tiny synthetic 'native NBM grid' -- shape/coordinates don't
    need to be realistic, just enough for regrid_to_regular_latlon to
    run without crashing. The actual values are irrelevant to what
    these tests check (the ORCHESTRATION logic -- correct result
    mapping to the right key -- not the numeric content)."""
    lons, lats = np.meshgrid(np.linspace(-110, -100, 8), np.linspace(35, 45, 8))
    values = np.full((8, 8), fill_value)
    return values, lats, lons


def test_prepare_mtn_obsc_grid_maps_each_field_to_the_correct_key(monkeypatch, tmp_path):
    """
    Confirms every one of the ten fields' results lands under the
    correct dict key -- straightforward for a sequential loop, but still
    worth confirming directly rather than assuming, especially since
    this was a genuinely easy place to introduce a subtle bug when this
    was a concurrent design (see prepare_mtn_obsc_grid's docstring).
    Giving every field a DISTINCT fill value makes any accidental
    cross-wiring between fields immediately obvious.
    """
    fake_values = {
        500: 10.0, 1000: 20.0, 2000: 30.0, 3000: 40.0, 6600: 50.0,
    }

    def fake_fetch_ceiling_prob_grid(date, fxx, threshold_ft):
        return _fake_native_grid(fake_values[threshold_ft])

    def fake_fetch_deterministic_ceiling_grid(date, fxx):
        return _fake_native_grid(60.0)

    def fake_fetch_cloud_base_grid(date, fxx):
        return _fake_native_grid(70.0)

    def fake_fetch_precip_probability_grid(date, fxx):
        return _fake_native_grid(80.0)

    def fake_fetch_probability_grid(date, fxx, filters):
        # Used directly for BOTH vis3 and vis1 -- distinguish by filter
        # content the same way the real filters do.
        if filters is mtn_obsc.VISIBILITY_PROB_FILTER:
            return _fake_native_grid(90.0)
        return _fake_native_grid(100.0)

    monkeypatch.setattr(mtn_obsc, "fetch_ceiling_prob_grid", fake_fetch_ceiling_prob_grid)
    monkeypatch.setattr(mtn_obsc, "fetch_deterministic_ceiling_grid", fake_fetch_deterministic_ceiling_grid)
    monkeypatch.setattr(mtn_obsc, "fetch_cloud_base_grid", fake_fetch_cloud_base_grid)
    monkeypatch.setattr(mtn_obsc, "fetch_precip_probability_grid", fake_fetch_precip_probability_grid)
    monkeypatch.setattr(mtn_obsc, "fetch_probability_grid", fake_fetch_probability_grid)

    # A minimal real terrain grid file, matching CONUS_BOUNDS/OUTPUT_RESOLUTION_DEG
    # exactly (required by prepare_mtn_obsc_grid's alignment check).
    terrain_path = tmp_path / "terrain_grid.npz"
    n_rows = round((mtn_obsc.CONUS_BOUNDS[3] - mtn_obsc.CONUS_BOUNDS[1]) / mtn_obsc.OUTPUT_RESOLUTION_DEG)
    n_cols = round((mtn_obsc.CONUS_BOUNDS[2] - mtn_obsc.CONUS_BOUNDS[0]) / mtn_obsc.OUTPUT_RESOLUTION_DEG)
    np.savez_compressed(
        terrain_path,
        baseline_elevation_ft=np.zeros((n_rows, n_cols), dtype=np.int16),
        ridge_elevation_ft=np.zeros((n_rows, n_cols), dtype=np.int16),
        west=mtn_obsc.CONUS_BOUNDS[0], north=mtn_obsc.CONUS_BOUNDS[3],
        dx=mtn_obsc.OUTPUT_RESOLUTION_DEG, dy=-mtn_obsc.OUTPUT_RESOLUTION_DEG,
        terrain_radius_nm=12.0,
    )

    from datetime import datetime

    result = prepare_mtn_obsc_grid(datetime(2026, 7, 16, 12), 6, terrain_grid_path=str(terrain_path))

    # Spot-check a real pixel that should have gotten a real (non-NaN,
    # non-zero-from-fallback) regridded value inside the fake native
    # grid's coverage area, confirming each field landed under the
    # correct key regardless of which order the concurrent fetches
    # actually completed in.
    row, col = 400, 1000  # somewhere inside the fake grid's ~35-45N, -110 to -100W coverage
    for threshold_ft, expected in fake_values.items():
        assert abs(result["ceiling_prob"][threshold_ft][row, col] - expected) < 0.01, threshold_ft
    assert abs(result["deterministic_ceiling"][row, col] - 60.0) < 0.01
    assert abs(result["cloud_base"][row, col] - 70.0) < 0.01
    assert abs(result["precip"][row, col] - 80.0) < 0.01
    assert abs(result["vis3"][row, col] - 90.0) < 0.01
    assert abs(result["vis1"][row, col] - 100.0) < 0.01


def test_prepare_mtn_obsc_grid_prints_progress_for_all_ten_fields(monkeypatch, tmp_path, capsys):
    """Confirms progress output actually appears for all ten fields --
    the whole point of this diagnostic logging is to distinguish
    'making steady progress' from 'stuck,' so it needs to actually show
    up for every field, not silently skip any."""

    def fake_ceiling(date, fxx, threshold_ft):
        return _fake_native_grid(1.0)

    def fake_single(date, fxx):
        return _fake_native_grid(1.0)

    def fake_probability(date, fxx, filters):
        return _fake_native_grid(1.0)

    monkeypatch.setattr(mtn_obsc, "fetch_ceiling_prob_grid", fake_ceiling)
    monkeypatch.setattr(mtn_obsc, "fetch_deterministic_ceiling_grid", fake_single)
    monkeypatch.setattr(mtn_obsc, "fetch_cloud_base_grid", fake_single)
    monkeypatch.setattr(mtn_obsc, "fetch_precip_probability_grid", fake_single)
    monkeypatch.setattr(mtn_obsc, "fetch_probability_grid", fake_probability)

    terrain_path = tmp_path / "terrain_grid.npz"
    n_rows = round((mtn_obsc.CONUS_BOUNDS[3] - mtn_obsc.CONUS_BOUNDS[1]) / mtn_obsc.OUTPUT_RESOLUTION_DEG)
    n_cols = round((mtn_obsc.CONUS_BOUNDS[2] - mtn_obsc.CONUS_BOUNDS[0]) / mtn_obsc.OUTPUT_RESOLUTION_DEG)
    np.savez_compressed(
        terrain_path,
        baseline_elevation_ft=np.zeros((n_rows, n_cols), dtype=np.int16),
        ridge_elevation_ft=np.zeros((n_rows, n_cols), dtype=np.int16),
        west=mtn_obsc.CONUS_BOUNDS[0], north=mtn_obsc.CONUS_BOUNDS[3],
        dx=mtn_obsc.OUTPUT_RESOLUTION_DEG, dy=-mtn_obsc.OUTPUT_RESOLUTION_DEG,
        terrain_radius_nm=12.0,
    )

    from datetime import datetime

    prepare_mtn_obsc_grid(datetime(2026, 7, 16, 12), 6, terrain_grid_path=str(terrain_path))
    captured = capsys.readouterr()
    for i in range(1, 11):
        assert f"[{i}/10]" in captured.out, f"missing progress line for field {i}/10"


if __name__ == "__main__":
    import pytest

    pytest.main([__file__, "-v"])
