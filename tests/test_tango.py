"""
tests/test_tango.py
---------------------
The TANGO layers (surface wind, LLWS): the idx filters, the knots x 2
grid cache, and Phase B on synthetic grids.

THE IDX PINS MATTER MOST. find_message-style matching is substring
matching on the whole idx line, and NBM's wind lines are a trap: the
bare text "10 m above ground" is also inside "surface - 610 m above
ground". A filter that is slightly too loose does not fail -- it matches
a different, plausible-looking field and produces wrong polygons. So the
filters are pinned to exactly one line of a SAVED REAL idx (NBM core
2026-10-04 06Z f006), and a control proves the trap is real in that file.
"""

import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.boundaries import CONUS_BOUNDARY_PATH, get_boundary_mask
from pipeline.hazards.llws import LLWS
from pipeline.hazards.mtn_obsc import find_message_excluding
from pipeline.hazards.sfc_wind import SFC_WIND
from pipeline.hazards.tango_common import (
    CACHE_SCALE,
    find_layer_message,
    polygonize_tango_grid,
)
from pipeline.polygons import GridSpec, load_grid_cache, save_grid_cache

FIXTURES = Path(__file__).resolve().parent / "fixtures"
REAL_IDX = FIXTURES / "nbm_core_20261004_06z_f006.idx"
CYCLE = datetime(2026, 10, 4, 9)


def real_rows():
    """Rows in the shape find_message_excluding reads (it needs only _raw_line)."""
    return [{"_raw_line": line} for line in REAL_IDX.read_text().splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# idx filters
# ---------------------------------------------------------------------------


def test_sfc_wind_filter_matches_exactly_the_10m_deterministic_line():
    line = find_layer_message(real_rows(), SFC_WIND)["_raw_line"]
    assert ":WIND:10 m above ground:6 hour fcst:" in line
    assert "610 m above ground" not in line
    assert "std dev" not in line


def test_llws_filter_matches_exactly_the_610m_line():
    line = find_layer_message(real_rows(), LLWS)["_raw_line"]
    assert ":WIND:surface - 610 m above ground:6 hour fcst:" in line


def test_the_two_layers_never_match_the_same_line():
    rows = real_rows()
    assert find_layer_message(rows, SFC_WIND)["_raw_line"] != find_layer_message(rows, LLWS)["_raw_line"]


def test_the_610m_trap_is_real_in_this_idx():
    """
    Control. The loose filter -- no colons, no exclusion -- must match
    MORE than one WIND line in the saved idx, otherwise the pins above
    would pass for any filter and prove nothing.
    """
    rows = real_rows()
    loose = [r for r in rows if "WIND" in r["_raw_line"] and "10 m above ground" in r["_raw_line"]]
    assert len(loose) >= 3  # 10 m, 10 m ens std dev, and surface - 610 m
    assert any("610 m above ground" in r["_raw_line"] for r in loose)
    with pytest.raises(ValueError, match="Ambiguous"):
        find_message_excluding(rows, [], variable="WIND", level="10 m above ground")


def test_a_filter_matching_nothing_raises_rather_than_guessing():
    with pytest.raises(ValueError):
        find_message_excluding(real_rows(), [], variable=":WIND:", level="surface - 999 m above ground")


# ---------------------------------------------------------------------------
# knots x 2 grid cache
# ---------------------------------------------------------------------------

SPEC = GridSpec(west=-100.0, north=40.0, dx=0.025, dy=-0.025)


def test_scale_round_trip_is_within_a_quarter_knot(tmp_path):
    rng = np.random.default_rng(0)
    speeds = (rng.random((40, 60)) * 100).astype(np.float32)
    speeds[0, 0] = 0.0
    save_grid_cache(tmp_path / "g.npz", {"speed_kt": speeds}, SPEC, scale=CACHE_SCALE)
    loaded, spec = load_grid_cache(tmp_path / "g.npz", scale=CACHE_SCALE)
    assert spec == SPEC
    assert np.abs(loaded["speed_kt"] - speeds).max() <= 0.25 + 1e-6
    assert loaded["speed_kt"][0, 0] == 0.0


def test_scale_round_trip_is_exact_on_half_knots(tmp_path):
    speeds = np.array([[0.0, 0.5, 30.0, 30.5, 41.0, 127.5]], dtype=np.float32)
    save_grid_cache(tmp_path / "g.npz", {"s": speeds}, SPEC, scale=2)
    loaded, _ = load_grid_cache(tmp_path / "g.npz", scale=2)
    np.testing.assert_array_equal(loaded["s"], speeds)


def test_scaled_cache_clips_instead_of_wrapping_and_survives_nan(tmp_path):
    speeds = np.array([[130.0, 300.0, np.nan]], dtype=np.float32)
    save_grid_cache(tmp_path / "g.npz", {"s": speeds}, SPEC, scale=2)
    loaded, _ = load_grid_cache(tmp_path / "g.npz", scale=2)
    np.testing.assert_array_equal(loaded["s"], [[127.5, 127.5, 0.0]])


def test_the_cache_knows_its_own_scale(tmp_path):
    speeds = np.full((4, 4), 40.0, dtype=np.float32)
    save_grid_cache(tmp_path / "g.npz", {"s": speeds}, SPEC, scale=2)
    # Self-describing: no scale given still returns knots, not knots x 2.
    assert load_grid_cache(tmp_path / "g.npz")[0]["s"][0, 0] == 40.0
    with pytest.raises(ValueError, match="scale"):
        load_grid_cache(tmp_path / "g.npz", scale=1)


def test_default_cache_path_is_bit_identical_to_before(tmp_path):
    """
    scale=None must be today's behaviour exactly: percent, rounded,
    uint8, no extra key. The reference below is the pre-change
    implementation written out independently.
    """
    rng = np.random.default_rng(1)
    grids = {"a": rng.random((30, 50)).astype(np.float32) * 100, "b": rng.random((30, 50)).astype(np.float32) * 100}

    save_grid_cache(tmp_path / "new.npz", grids, SPEC)
    np.savez_compressed(
        tmp_path / "old.npz",
        west=SPEC.west, north=SPEC.north, dx=SPEC.dx, dy=SPEC.dy,
        **{n: np.round(g).astype(np.uint8) for n, g in grids.items()},
    )
    new, old = np.load(tmp_path / "new.npz"), np.load(tmp_path / "old.npz")
    assert sorted(new.files) == sorted(old.files)
    assert "scale" not in new.files
    for name in old.files:
        assert new[name].dtype == old[name].dtype
        assert new[name].tobytes() == old[name].tobytes()

    loaded, spec = load_grid_cache(tmp_path / "new.npz")
    assert spec == SPEC
    for name, g in grids.items():
        assert loaded[name].dtype == np.float32
        np.testing.assert_array_equal(loaded[name], np.round(g).astype(np.uint8).astype(np.float32))


# ---------------------------------------------------------------------------
# Phase B on synthetic grids
# ---------------------------------------------------------------------------

# Kansas / Nebraska: well inside the ARTCC area of responsibility, so the
# gate cannot interfere with the tests that are not about the gate.
INTERIOR_SPEC = GridSpec(west=-105.0, north=42.0, dx=0.025, dy=-0.025)
INTERIOR_SHAPE = (200, 400)  # 5 deg x 10 deg


def block(spec, shape, lat0, lat1, lon0, lon1, value, background=0.0):
    grid = np.full(shape, background, dtype=np.float32)
    rows = [int(round((spec.north - lat) / -spec.dy)) for lat in (lat0, lat1)]
    cols = [int(round((lon - spec.west) / spec.dx)) for lon in (lon0, lon1)]
    grid[min(rows):max(rows), min(cols):max(cols)] = value
    return grid


def total_area(fc):
    return sum(f["properties"]["area_sq_mi"] for f in fc["features"])


def run(speed, spec, layer=SFC_WIND, **kw):
    return polygonize_tango_grid(speed, spec, layer, CYCLE, 6, **kw)


def test_smoothing_keeps_area_within_25_percent():
    # A sparse event field like LLWS: a patch of 45 kt on a zero background.
    speed = block(INTERIOR_SPEC, INTERIOR_SHAPE, 41.0, 39.0, -103.0, -99.0, 45.0)
    unsmoothed = run(speed, INTERIOR_SPEC, speed_threshold_kt=40.0, smooth_sigma_cells=0)
    smoothed = run(speed, INTERIOR_SPEC, speed_threshold_kt=40.0, smooth_sigma_cells=1.5)
    assert total_area(unsmoothed) > 20000  # the control: there is something to shrink
    assert abs(total_area(smoothed) - total_area(unsmoothed)) / total_area(unsmoothed) <= 0.25


def test_smoothing_a_ragged_edge_does_not_swallow_the_feature():
    rng = np.random.default_rng(2)
    speed = block(INTERIOR_SPEC, INTERIOR_SHAPE, 41.0, 39.0, -103.0, -99.0, 45.0)
    speckle = (rng.random(speed.shape) < 0.03) & (speed == 0)
    speed[speckle] = 45.0  # isolated single-cell hits outside the patch
    smoothed = run(speed, INTERIOR_SPEC, speed_threshold_kt=40.0, smooth_sigma_cells=1.5)
    assert len(smoothed["features"]) == 1  # one area; the speckle is gone
    assert total_area(smoothed) > 0.75 * 25000


def test_the_threshold_is_greater_than_or_equal():
    at = block(INTERIOR_SPEC, INTERIOR_SHAPE, 41.0, 39.0, -103.0, -99.0, 30.0)
    just_under = block(INTERIOR_SPEC, INTERIOR_SHAPE, 41.0, 39.0, -103.0, -99.0, 29.99)
    assert len(run(at, INTERIOR_SPEC, speed_threshold_kt=30.0, smooth_sigma_cells=0)["features"]) == 1
    assert run(just_under, INTERIOR_SPEC, speed_threshold_kt=30.0, smooth_sigma_cells=0)["features"] == []


def test_polygon_properties():
    speed = block(INTERIOR_SPEC, INTERIOR_SHAPE, 41.0, 39.0, -103.0, -99.0, 45.0)
    speed[100, 150] = 61.5  # a peak inside the patch
    fc = run(speed, INTERIOR_SPEC, LLWS, smooth_sigma_cells=0)
    assert len(fc["features"]) == 1
    props = fc["features"][0]["properties"]
    assert props["peak_speed_kt"] == 61.5  # from the RAW grid, not a smoothed one
    assert props["category"] == "TANGO"
    assert props["phenomenon"] == "LLWS POTENTIAL"
    assert props["area_sq_mi"] > 20000
    assert props["hazard"] == "LLWS"
    assert props["speed_threshold_kt"] == 40.0  # LLWS default
    assert props["valid_time"] == "2026-10-04T15:00:00Z"


def test_parameters_default_to_the_layers_and_can_be_overridden():
    speed = block(INTERIOR_SPEC, INTERIOR_SHAPE, 41.0, 39.0, -103.0, -99.0, 35.0)
    # 35 kt: over sfc_wind's 30 default, under llws's 40.
    assert len(run(speed, INTERIOR_SPEC, SFC_WIND)["features"]) == 1
    assert run(speed, INTERIOR_SPEC, LLWS)["features"] == []
    assert len(run(speed, INTERIOR_SPEC, LLWS, speed_threshold_kt=35.0)["features"]) == 1


def test_min_area_drops_small_features():
    small = block(INTERIOR_SPEC, INTERIOR_SHAPE, 40.5, 40.0, -102.0, -101.0, 45.0)  # ~1,500 sq mi
    assert len(run(small, INTERIOR_SPEC, smooth_sigma_cells=0, min_area_sq_mi=1000)["features"]) == 1
    assert run(small, INTERIOR_SPEC, smooth_sigma_cells=0, min_area_sq_mi=5000)["features"] == []


# Straddles the US/Canada border at 49N (North Dakota / Manitoba).
BORDER_SPEC = GridSpec(west=-102.0, north=51.0, dx=0.025, dy=-0.025)
BORDER_SHAPE = (160, 200)  # 51N -> 47N, 102W -> 97W


def test_the_artcc_gate_applies_after_smoothing():
    speed = block(BORDER_SPEC, BORDER_SHAPE, 50.5, 47.5, -100.5, -98.5, 45.0)

    # Controls: the blob really does cross the border, and the gate mask
    # really does exclude the north side.
    rows_north = [r for r in range(BORDER_SHAPE[0]) if BORDER_SPEC.north + r * BORDER_SPEC.dy > 49.5]
    assert (speed[rows_north] >= 40.0).any()
    within = get_boundary_mask(BORDER_SPEC, BORDER_SHAPE, CONUS_BOUNDARY_PATH)
    assert not within[rows_north].any()
    assert within[~np.isin(np.arange(BORDER_SHAPE[0]), rows_north)].any()

    fc = run(speed, BORDER_SPEC, speed_threshold_kt=40.0, smooth_sigma_cells=3.0, neighborhood_radius_nm=50.0)
    assert len(fc["features"]) == 1
    lats = [lat for ring in fc["features"][0]["geometry"]["coordinates"] for lat in (c[1] for c in ring)]
    # Smoothing (sigma 3 cells = 0.075 deg) and the 50 nm closing would
    # both push the edge north if the gate ran first. 0.1 deg of slack
    # covers the contour half-cell and the boundary smoothing buffer.
    assert max(lats) < 49.1
    assert min(lats) < 48.0  # and the southern half survived
