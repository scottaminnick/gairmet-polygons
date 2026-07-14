"""
tests/test_ifr_cause_attribution.py
--------------------------------------
Tests pipeline/hazards/ifr.py's per-polygon "cause" attribution (CIG,
VIS, or CIG/VIS) -- confirms it's actually computed per-polygon from
the real ceiling/visibility grids, not just a single shared guess.
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


def _generate(ceil_grid, vis_grid, **kwargs):
    defaults = {"threshold_pct": 50.0, "neighborhood_radius_nm": 0, "min_area_sq_mi": 0.001}
    defaults.update(kwargs)
    return polygonize_ifr_grid(ceil_grid, vis_grid, GRID_SPEC, VALID_DATE, 0, **defaults)


def test_pure_ceiling_case_attributed_as_cig():
    n = 100
    ceil_grid = np.zeros((n, n))
    vis_grid = np.zeros((n, n))
    ceil_grid[30:70, 30:70] = 80

    fc = _generate(ceil_grid, vis_grid)
    assert len(fc["features"]) == 1
    assert fc["features"][0]["properties"]["cause"] == "CIG"


def test_pure_visibility_case_attributed_as_vis():
    n = 100
    ceil_grid = np.zeros((n, n))
    vis_grid = np.zeros((n, n))
    vis_grid[30:70, 30:70] = 80

    fc = _generate(ceil_grid, vis_grid)
    assert len(fc["features"]) == 1
    assert fc["features"][0]["properties"]["cause"] == "VIS"


def test_overlapping_ceiling_and_visibility_attributed_as_both():
    n = 100
    ceil_grid = np.zeros((n, n))
    vis_grid = np.zeros((n, n))
    ceil_grid[30:70, 30:70] = 80
    vis_grid[30:70, 30:70] = 80

    fc = _generate(ceil_grid, vis_grid)
    assert len(fc["features"]) == 1
    assert fc["features"][0]["properties"]["cause"] == "CIG/VIS"


def test_two_separate_polygons_get_independently_correct_causes():
    """
    The critical case: cause attribution must be computed PER POLYGON,
    not as one shared/global guess for the whole output. Two separate,
    far-apart regions -- one pure ceiling, one pure visibility -- must
    each get their own correct, DIFFERENT cause.
    """
    n = 200
    ceil_grid = np.zeros((n, n))
    vis_grid = np.zeros((n, n))
    ceil_grid[20:50, 20:50] = 80     # region A: ceiling only
    vis_grid[150:180, 150:180] = 80  # region B: visibility only, far away

    fc = _generate(ceil_grid, vis_grid)
    assert len(fc["features"]) == 2

    causes = sorted(f["properties"]["cause"] for f in fc["features"])
    assert causes == ["CIG", "VIS"]


if __name__ == "__main__":
    test_pure_ceiling_case_attributed_as_cig()
    test_pure_visibility_case_attributed_as_vis()
    test_overlapping_ceiling_and_visibility_attributed_as_both()
    test_two_separate_polygons_get_independently_correct_causes()
    print("All manual checks passed.")
