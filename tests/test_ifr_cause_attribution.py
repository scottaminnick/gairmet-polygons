"""
tests/test_ifr_cause_attribution.py
--------------------------------------
Tests pipeline/hazards/ifr.py's per-polygon attribution:
  - "cause" (CIG, VIS, or CIG/VIS) -- which criterion made this IFR.
  - "weather_type" (PCPN, BR, FG, or combinations) -- for polygons
    whose cause includes VIS, what's actually driving the visibility
    restriction, per confirmed AWC convention.
  - Geographic splitting: PCPN-driven and non-precip visibility-driven
    areas come out as genuinely SEPARATE polygon shapes when they're
    geographically distinct, not just separately-labeled pieces of one
    merged blob.
"""

import sys
from datetime import datetime
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.hazards.ifr import polygonize_ifr_grid
from pipeline.polygons import GridSpec

GRID_SPEC = GridSpec(west=-100.0, north=45.0, dx=0.03, dy=-0.03)
VALID_DATE = datetime(2026, 7, 14, 15)


def _generate(ceil_grid, vis3_grid, vis1_grid, precip_grid, **kwargs):
    defaults = {"threshold_pct": 50.0, "neighborhood_radius_nm": 0, "min_area_sq_mi": 0.001}
    defaults.update(kwargs)
    return polygonize_ifr_grid(ceil_grid, vis3_grid, vis1_grid, precip_grid, GRID_SPEC, VALID_DATE, 0, **defaults)


def _zeros(n=100):
    return tuple(np.zeros((n, n)) for _ in range(4))  # ceil, vis3, vis1, precip


# --- cause attribution (CIG/VIS/CIG-VIS) ---

def test_pure_ceiling_case_attributed_as_cig_with_no_weather_type():
    ceil, vis3, vis1, precip = _zeros()
    ceil[30:70, 30:70] = 80

    fc = _generate(ceil, vis3, vis1, precip)
    assert len(fc["features"]) == 1
    props = fc["features"][0]["properties"]
    assert props["cause"] == "CIG"
    assert "weather_type" not in props, "Pure ceiling polygons shouldn't get a weather_type at all"


def test_pure_visibility_case_attributed_as_vis():
    ceil, vis3, vis1, precip = _zeros()
    vis3[30:70, 30:70] = 80

    fc = _generate(ceil, vis3, vis1, precip)
    assert len(fc["features"]) == 1
    assert fc["features"][0]["properties"]["cause"] == "VIS"


def test_two_separate_polygons_get_independently_correct_causes():
    """The critical case for 'cause': computed PER POLYGON, not one shared/global guess."""
    n = 200
    ceil, vis3, vis1, precip = _zeros(n)
    ceil[20:50, 20:50] = 80      # region A: ceiling only
    vis3[150:180, 150:180] = 80  # region B: visibility only, far away

    fc = _generate(ceil, vis3, vis1, precip)
    assert len(fc["features"]) == 2
    causes = sorted(f["properties"]["cause"] for f in fc["features"])
    assert causes == ["CIG", "VIS"]


def test_ceiling_and_visibility_coinciding_produces_exactly_one_polygon_not_a_duplicate():
    """
    Regression guard: ceiling and visibility restrictions coinciding
    (e.g. a stratus deck) is common, not rare. Earlier iteration of this
    logic generated a REDUNDANT duplicate polygon here -- one from the
    CIG layer, one from the visibility layer, both covering the exact
    same area with identical labels. Confirmed and fixed during
    development; this guards against that regressing.
    """
    ceil, vis3, vis1, precip = _zeros()
    ceil[30:70, 30:70] = 80
    vis3[30:70, 30:70] = 80

    fc = _generate(ceil, vis3, vis1, precip)
    assert len(fc["features"]) == 1, f"Expected exactly 1 polygon (no duplicate), got {len(fc['features'])}"
    assert fc["features"][0]["properties"]["cause"] == "CIG/VIS"


# --- weather_type attribution (PCPN/BR/FG) ---

def test_pure_pcpn_case():
    ceil, vis3, vis1, precip = _zeros()
    vis3[30:70, 30:70] = 80
    precip[30:70, 30:70] = 80

    fc = _generate(ceil, vis3, vis1, precip)
    assert len(fc["features"]) == 1
    assert fc["features"][0]["properties"]["weather_type"] == "PCPN/BR"


def test_pure_fog_case():
    ceil, vis3, vis1, precip = _zeros()
    vis3[30:70, 30:70] = 80
    vis1[30:70, 30:70] = 80

    fc = _generate(ceil, vis3, vis1, precip)
    assert len(fc["features"]) == 1
    assert fc["features"][0]["properties"]["weather_type"] == "BR/FG"


def test_pure_br_case_with_no_precip_or_fog():
    ceil, vis3, vis1, precip = _zeros()
    vis3[30:70, 30:70] = 80

    fc = _generate(ceil, vis3, vis1, precip)
    assert len(fc["features"]) == 1
    assert fc["features"][0]["properties"]["weather_type"] == "BR"


def test_br_is_never_replaced_by_more_specific_descriptors():
    """Confirmed AWC convention: BR is a catch-all, always present alongside PCPN/FG, never replaced by them."""
    # PCPN case should still include BR
    ceil, vis3, vis1, precip = _zeros()
    vis3[30:70, 30:70] = 80
    precip[30:70, 30:70] = 80
    fc = _generate(ceil, vis3, vis1, precip)
    assert "BR" in fc["features"][0]["properties"]["weather_type"].split("/")

    # FG case should still include BR
    ceil, vis3, vis1, precip = _zeros()
    vis3[30:70, 30:70] = 80
    vis1[30:70, 30:70] = 80
    fc = _generate(ceil, vis3, vis1, precip)
    assert "BR" in fc["features"][0]["properties"]["weather_type"].split("/")


def test_pcpn_wins_when_pcpn_and_fog_conditions_genuinely_overlap():
    """
    Confirmed AWC practice: when precip is happening and visibility
    drops below 1SM in the SAME location, attribute it to the precip,
    not fog -- FG should NOT appear even though the 1SM threshold is
    crossed there.
    """
    ceil, vis3, vis1, precip = _zeros()
    vis3[30:70, 30:70] = 80
    vis1[30:70, 30:70] = 80  # same exact cells as precip
    precip[30:70, 30:70] = 80

    fc = _generate(ceil, vis3, vis1, precip)
    assert len(fc["features"]) == 1
    weather_type = fc["features"][0]["properties"]["weather_type"]
    assert weather_type == "PCPN/BR", f"Expected PCPN to win the overlap (no FG), got {weather_type}"


def test_geographically_separate_pcpn_and_fog_areas_produce_separate_polygons():
    """
    The core ask: breaking up areas of PCPN from areas of FG when
    they're genuinely in different places -- this must be real
    geography (two distinct polygon shapes), not just two labels
    stapled onto one merged blob.
    """
    n = 200
    ceil, vis3, vis1, precip = _zeros(n)
    vis3[20:50, 20:50] = 80      # region A: rainy, reduced vis, not foggy
    precip[20:50, 20:50] = 80
    vis3[150:180, 150:180] = 80  # region B: foggy, not raining, far away
    vis1[150:180, 150:180] = 80

    fc = _generate(ceil, vis3, vis1, precip)
    assert len(fc["features"]) == 2, f"Expected 2 SEPARATE polygons, got {len(fc['features'])}"
    weather_types = sorted(f["properties"]["weather_type"] for f in fc["features"])
    assert weather_types == ["BR/FG", "PCPN/BR"]


if __name__ == "__main__":
    test_pure_ceiling_case_attributed_as_cig_with_no_weather_type()
    test_pure_visibility_case_attributed_as_vis()
    test_two_separate_polygons_get_independently_correct_causes()
    test_ceiling_and_visibility_coinciding_produces_exactly_one_polygon_not_a_duplicate()
    test_pure_pcpn_case()
    test_pure_fog_case()
    test_pure_br_case_with_no_precip_or_fog()
    test_br_is_never_replaced_by_more_specific_descriptors()
    test_pcpn_wins_when_pcpn_and_fog_conditions_genuinely_overlap()
    test_geographically_separate_pcpn_and_fog_areas_produce_separate_polygons()
    print("All manual checks passed.")
