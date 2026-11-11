"""
tests/test_min_area_final.py
-------------------------------
MIN AREA applies to the polygons that are actually emitted, for IFR, MTN
OBSC and TANGO: no output polygon, and no part of an output MultiPolygon,
is below the layer's minimum.

Each layer used to filter by area and THEN run steps that can split or
shrink a polygon, so pieces below the minimum were published. Measured on
real cycles:
  - IFR v2: the 0.1-degree majority reduce erases a neck narrower than one
    coarse cell and the relabel splits the region (2026-11-10 09Z F00: 7
    pieces of 17-339 sq mi under a 3,000 minimum), and the class partition
    splits a passing component into sub-minimum regions (2026-11-11 09Z
    F00: 3,128 sq mi into 1,538 + 1,589).
  - MTN OBSC and TANGO: boundary smoothing splits a polygon into a
    MultiPolygon with a small part (17 and 22 sq mi), and simplification
    shrinks one below the line (LLWS 274 under a 300 minimum).

The synthetic "dumbbell" below reproduces the split for every layer: a
large block joined to a small blob by a neck one fine cell wide. The whole
shape passes the minimum, the blob alone does not, and the neck is too
thin to survive either IFR's coarsening or the others' smoothing.
"""

import ast
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pytest
from shapely.geometry import MultiPolygon, Polygon, box
from shapely.geometry import shape as shapely_shape

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import pipeline.hazards.mtn_obsc as mtn  # noqa: E402
import pipeline.hazards.tango_common as tango  # noqa: E402
from pipeline.hazards.ifr import polygonize_ifr_grid, polygonize_ifr_grid_v2  # noqa: E402
from pipeline.hazards.llws import LLWS  # noqa: E402
from pipeline.hazards.sfc_wind import SFC_WIND  # noqa: E402
from pipeline.polygons import GridSpec, enforce_min_area_on_output, geodesic_area_sq_mi  # noqa: E402

# Production fine resolution (0.025 deg), over Utah/Colorado/Wyoming: inside
# the AOR and on U.S. land, so neither boundary mask touches the shape.
GRID_SPEC = GridSpec(west=-112.0, north=43.0, dx=0.025, dy=-0.025)
SHAPE = (320, 480)
CYCLE = datetime(2026, 11, 11, 9)


def dumbbell(value: float) -> np.ndarray:
    """~58,000 sq mi block, a 60-cell neck one cell wide, a ~900 sq mi blob."""
    field = np.zeros(SHAPE, dtype=np.float32)
    field[40:200, 40:200] = value   # block
    field[120, 200:260] = value     # neck: one fine cell wide
    field[110:130, 260:280] = value  # blob
    return field


BLOB = box(GRID_SPEC.west + 260 * GRID_SPEC.dx, GRID_SPEC.north + 130 * GRID_SPEC.dy,
           GRID_SPEC.west + 280 * GRID_SPEC.dx, GRID_SPEC.north + 110 * GRID_SPEC.dy)


def parts(feature_collection) -> list:
    """Every emitted polygon, with a MultiPolygon counted part by part."""
    out = []
    for feature in feature_collection["features"]:
        geometry = shapely_shape(feature["geometry"])
        out.extend(geometry.geoms if geometry.geom_type == "MultiPolygon" else [geometry])
    return out


def assert_nothing_below(feature_collection, min_area_sq_mi, label):
    areas = sorted(round(geodesic_area_sq_mi(p)) for p in parts(feature_collection))
    assert areas, f"{label}: nothing emitted -- the test shape is wrong"
    small = [a for a in areas if a < min_area_sq_mi]
    assert not small, f"{label}: polygons below MIN AREA {min_area_sq_mi:g}: {small} (all: {areas})"


# --- each layer, on the dumbbell ------------------------------------------------

def run_ifr_v2(min_area):
    zeros = np.zeros(SHAPE, dtype=np.float32)
    return polygonize_ifr_grid_v2(dumbbell(80.0), zeros, zeros, zeros, GRID_SPEC, CYCLE, 0,
                                  neighborhood_radius_nm=0.0, min_area_sq_mi=min_area)


def run_ifr_v1(min_area):
    zeros = np.zeros(SHAPE, dtype=np.float32)
    return polygonize_ifr_grid(dumbbell(80.0), zeros, zeros, zeros, GRID_SPEC, CYCLE, 0,
                               neighborhood_radius_nm=0.0, min_area_sq_mi=min_area)


def run_mtn_obsc(min_area):
    zeros = np.zeros(SHAPE, dtype=np.float32)
    return mtn.polygonize_mtn_obsc_grid(
        {t: dumbbell(80.0) for t in mtn.CEILING_PROB_THRESHOLDS_FT}, zeros, zeros, zeros,
        np.full(SHAPE, 4000.0, dtype=np.float32), np.full(SHAPE, 9000.0, dtype=np.float32),
        GRID_SPEC, CYCLE, 0, neighborhood_radius_nm=0.0, min_area_sq_mi=min_area, terrain_radius_nm=12.0)


def tango_runner(layer):
    def run(min_area):
        return tango.polygonize_tango_grid(dumbbell(80.0), GRID_SPEC, layer, CYCLE, 0,
                                           smooth_sigma_cells=0.0, neighborhood_radius_nm=0.0,
                                           min_area_sq_mi=min_area)
    return run


LAYERS = {
    "ifr": run_ifr_v2,
    "ifr_v1": run_ifr_v1,
    "mtn_obsc": run_mtn_obsc,
    "sfc_wind": tango_runner(SFC_WIND),
    "llws": tango_runner(LLWS),
}


@pytest.mark.parametrize("min_area", [1500.0, 3000.0])
@pytest.mark.parametrize("layer", sorted(LAYERS))
def test_no_output_polygon_is_below_min_area(layer, min_area):
    """The whole dumbbell passes MIN AREA; the piece split off it must not be emitted."""
    fc = LAYERS[layer](min_area)
    assert_nothing_below(fc, min_area, f"{layer} at MIN AREA {min_area:g}")
    # The block survives: the guarantee drops the split-off piece, not the area.
    assert max(geodesic_area_sq_mi(p) for p in parts(fc)) > 50_000


@pytest.mark.parametrize("layer", sorted(LAYERS))
def test_the_dumbbell_really_splits(layer):
    """
    Not vacuous: with MIN AREA well below the blob, every layer emits the
    blob as a separate polygon or part. So at 1,500 / 3,000 above it is
    the final guarantee that removes it, not some step that never split.
    """
    fc = LAYERS[layer](100.0)
    pieces = parts(fc)
    assert len(pieces) >= 2, f"{layer}: the neck did not split the shape ({len(pieces)} piece)"
    blob_pieces = [p for p in pieces if p.intersection(BLOB).area > 0.5 * p.area]
    assert blob_pieces and all(geodesic_area_sq_mi(p) < 1500 for p in blob_pieces), (
        f"{layer}: expected a split-off blob below 1,500 sq mi"
    )


# --- IFR's partition: a passing component is not lost to the final filter -------

def test_ifr_component_split_by_class_is_absorbed_not_dropped():
    """
    Like 2026-11-11 09Z F00's 3,128 sq mi component: one component over
    MIN AREA, three classes (CIG 38%, BR 31%, PCPN 31%), none dominant,
    each below MIN AREA. The sub-regions are absorbed into one polygon,
    so the area is still drawn, rather than emitted below the minimum --
    or dropped by the final filter, which would lose an area that passed
    as a whole.
    """
    zeros = np.zeros(SHAPE, dtype=np.float32)
    ceiling, vis3, precip = zeros.copy(), zeros.copy(), zeros.copy()
    ceiling[100:130, 100:124] = 80.0  # CIG, 24 columns
    vis3[100:130, 124:164] = 80.0     # VIS: BR for 20 columns, then PCPN for 20
    precip[100:130, 144:164] = 80.0

    def run(min_area):
        return polygonize_ifr_grid_v2(ceiling, vis3, zeros, precip, GRID_SPEC, CYCLE, 0,
                                      neighborhood_radius_nm=0.0, min_area_sq_mi=min_area)

    # Below every band, the split by class happens as before.
    split = run(500.0)
    areas = sorted(geodesic_area_sq_mi(p) for p in parts(split))
    assert len(areas) == 3 and max(areas) < 3000, f"expected three sub-3,000 bands, got {areas}"
    whole = sum(areas)
    assert whole > 3000

    # At 3,000 the component passes but no band does: one polygon, all of it.
    fc = run(3000.0)
    (polygon,) = parts(fc)
    assert geodesic_area_sq_mi(polygon) > 0.95 * whole
    assert fc["features"][0]["properties"]["cause"] == "CIG/VIS"


# --- the helper ---------------------------------------------------------------

def test_enforce_min_area_on_output_drops_small_polygons_and_parts():
    big = box(-110, 40, -108, 42)        # ~19,000 sq mi
    small = box(-105, 40, -104.9, 40.1)  # ~59 sq mi
    other_big = box(-100, 40, -98, 42)
    kept, sources = enforce_min_area_on_output(
        [small, MultiPolygon([big, small]), Polygon(), MultiPolygon([small, small.buffer(0.01)]),
         MultiPolygon([big, other_big]), other_big],
        1000.0,
    )
    assert sources == [1, 4, 5]
    assert kept[0].geom_type == "Polygon" and kept[0].equals(big)  # one part left: a Polygon
    assert kept[1].geom_type == "MultiPolygon" and len(kept[1].geoms) == 2
    assert kept[2].equals(other_big)


# --- every hazard ends with the guarantee -------------------------------------

@pytest.mark.parametrize("path, function", [
    ("pipeline/hazards/ifr.py", "polygonize_ifr_grid"),
    ("pipeline/hazards/ifr.py", "polygonize_ifr_grid_v2"),
    ("pipeline/hazards/mtn_obsc.py", "polygonize_mtn_obsc_grid"),
    ("pipeline/hazards/tango_common.py", "polygonize_tango_grid"),
])
def test_the_guarantee_runs_after_every_shape_changing_step(path, function):
    """
    Structural: in each polygonizer, enforce_min_area_on_output is called,
    and no smoothing, simplification or contouring call comes after it. A
    new step added after the guarantee would quietly reopen the gap.
    """
    tree = ast.parse((REPO_ROOT / path).read_text())
    (fn,) = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == function]
    calls = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
            calls.append((node.lineno, name))
    guard_lines = [line for line, name in calls if name == "enforce_min_area_on_output"]
    assert guard_lines, f"{function} never applies MIN AREA to its output"
    shape_changing = {"smooth_polygon_boundary", "simplify", "_simplify_regions", "_contour_region",
                      "grid_to_polygons", "merge_nearby_polygons", "buffer"}
    late = [(line, name) for line, name in calls if name in shape_changing and line > max(guard_lines)]
    assert not late, f"{function}: shape-changing calls after the MIN AREA guarantee: {late}"
