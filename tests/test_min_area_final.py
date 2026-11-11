"""
tests/test_min_area_final.py
-------------------------------
MIN AREA judges a WHOLE area once, at the fine-grid stage, before coarsening
and simplification. Afterwards only the fragments that splitting created are
removed: no piece split off a larger area, and no part of an output
MultiPolygon other than its largest, is below the layer's minimum. A whole
area stays drawn even if its emitted shape came out under the line.

Why both halves. Each layer used to filter by area and then run steps that
split polygons, so sub-minimum pieces were published. Measured:
  - IFR v2: the 0.1-degree majority reduce erases a neck narrower than one
    coarse cell and the relabel splits the region (2026-11-10 09Z F00: 7
    pieces of 17-339 sq mi under a 3,000 minimum), and the class partition
    split a passing 3,128 sq mi component into 1,538 + 1,589.
  - MTN OBSC and TANGO: boundary smoothing splits a polygon into a
    MultiPolygon with a small part (17, 22 and 58 sq mi).
And the other way round: that 3,128 sq mi area, drawn as one polygon, comes
out at 2,923 sq mi after the coarse trace and simplification. Judged on the
emitted shape it would be dropped; judged at the fine-grid stage, as the
forecaster asked, it is drawn.

The synthetic "dumbbell" reproduces the split for every layer: a large block
joined to a small blob by a neck one fine cell wide. The whole shape passes
the minimum, the blob alone does not, and the neck is too thin to survive
either IFR's coarsening or the others' smoothing.
"""

import ast
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pytest
from shapely import affinity
from shapely.geometry import MultiPolygon, Polygon, box
from shapely.geometry import shape as shapely_shape

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import pipeline.hazards.ifr as ifr  # noqa: E402
import pipeline.hazards.mtn_obsc as mtn  # noqa: E402
import pipeline.hazards.tango_common as tango  # noqa: E402
from pipeline.hazards.ifr import polygonize_ifr_grid, polygonize_ifr_grid_v2  # noqa: E402
from pipeline.hazards.llws import LLWS  # noqa: E402
from pipeline.hazards.sfc_wind import SFC_WIND  # noqa: E402
from pipeline.polygons import GridSpec, cell_areas_sq_mi, drop_split_fragments, geodesic_area_sq_mi  # noqa: E402

# Production fine resolution (0.025 deg), over Utah/Colorado/Wyoming: inside
# the AOR and on U.S. land, so neither boundary mask touches the shape.
GRID_SPEC = GridSpec(west=-112.0, north=43.0, dx=0.025, dy=-0.025)
SHAPE = (320, 480)
CYCLE = datetime(2026, 11, 11, 9)


def dumbbell(value: float = 80.0) -> np.ndarray:
    """~58,000 sq mi block, a 60-cell neck one cell wide, a ~900 sq mi blob."""
    field = np.zeros(SHAPE, dtype=np.float32)
    field[40:200, 40:200] = value    # block
    field[120, 200:260] = value      # neck: one fine cell wide
    field[110:130, 260:280] = value  # blob
    return field


def block(value: float = 80.0) -> np.ndarray:
    """One plain ~4,600 sq mi block: a whole area, nothing to split."""
    field = np.zeros(SHAPE, dtype=np.float32)
    field[100:150, 100:140] = value
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


# --- each layer, on a given field -------------------------------------------

ZEROS = np.zeros(SHAPE, dtype=np.float32)


def run_ifr_v2(field, min_area):
    return polygonize_ifr_grid_v2(field, ZEROS, ZEROS, ZEROS, GRID_SPEC, CYCLE, 0,
                                  neighborhood_radius_nm=0.0, min_area_sq_mi=min_area)


def run_ifr_v1(field, min_area):
    return polygonize_ifr_grid(field, ZEROS, ZEROS, ZEROS, GRID_SPEC, CYCLE, 0,
                               neighborhood_radius_nm=0.0, min_area_sq_mi=min_area)


def run_mtn_obsc(field, min_area):
    return mtn.polygonize_mtn_obsc_grid(
        {t: field for t in mtn.CEILING_PROB_THRESHOLDS_FT}, ZEROS, ZEROS, ZEROS,
        np.full(SHAPE, 4000.0, dtype=np.float32), np.full(SHAPE, 9000.0, dtype=np.float32),
        GRID_SPEC, CYCLE, 0, neighborhood_radius_nm=0.0, min_area_sq_mi=min_area, terrain_radius_nm=12.0)


def tango_runner(layer):
    def run(field, min_area):
        return tango.polygonize_tango_grid(field, GRID_SPEC, layer, CYCLE, 0,
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


# --- 1. the guard: no split fragment below the minimum -----------------------

@pytest.mark.parametrize("min_area", [1500.0, 3000.0])
@pytest.mark.parametrize("layer", sorted(LAYERS))
def test_no_split_fragment_is_below_min_area(layer, min_area):
    """
    The whole dumbbell passes MIN AREA; the blob the neck's loss splits off
    does not, and must not be emitted -- not as its own polygon, not as a
    part of a MultiPolygon. The block itself survives.
    """
    pieces = parts(LAYERS[layer](dumbbell(), min_area))
    areas = sorted(round(geodesic_area_sq_mi(p)) for p in pieces)
    assert areas and max(areas) > 50_000, f"{layer}: the block itself was lost ({areas})"
    small = [a for a in areas if a < min_area]
    assert not small, f"{layer} at MIN AREA {min_area:g}: split fragments below it: {small} (all: {areas})"


@pytest.mark.parametrize("layer", sorted(LAYERS))
def test_the_dumbbell_really_splits(layer):
    """
    Not vacuous: with MIN AREA well below the blob, every layer emits the
    blob as a separate polygon or part. So at 1,500 / 3,000 above it is
    the fragment rule that removes it, not some step that never split.
    """
    pieces = parts(LAYERS[layer](dumbbell(), 100.0))
    assert len(pieces) >= 2, f"{layer}: the neck did not split the shape ({len(pieces)} piece)"
    blob_pieces = [p for p in pieces if p.intersection(BLOB).area > 0.5 * p.area]
    assert blob_pieces and all(geodesic_area_sq_mi(p) < 1500 for p in blob_pieces), (
        f"{layer}: expected a split-off blob below 1,500 sq mi"
    )


# --- 2. a whole area is judged once, before the steps that shrink it ---------

# The step in each polygonizer that runs AFTER the area judgement and can
# shrink a shape. Patched to shrink everything to 80% of its area, so the
# emitted shape lands under a minimum the whole area clearly passed.
SHRINK_POINT = {
    "ifr": (ifr, "_simplify_regions", lambda polys, tol: [affinity.scale(p, 0.894, 0.894) for p in polys]),
    "ifr_v1": (ifr, "smooth_polygon_boundary", lambda p, **kw: affinity.scale(p, 0.894, 0.894)),
    "mtn_obsc": (mtn, "smooth_polygon_boundary", lambda p, **kw: affinity.scale(p, 0.894, 0.894)),
    "sfc_wind": (tango, "smooth_polygon_boundary", lambda p, **kw: affinity.scale(p, 0.894, 0.894)),
    "llws": (tango, "smooth_polygon_boundary", lambda p, **kw: affinity.scale(p, 0.894, 0.894)),
}


@pytest.mark.parametrize("layer", sorted(LAYERS))
def test_a_whole_area_that_passed_stays_drawn_though_its_emitted_shape_shrank(layer, monkeypatch):
    run = LAYERS[layer]
    (whole,) = parts(run(block(), 0.0))
    passed_at = geodesic_area_sq_mi(whole)
    min_area = 0.9 * passed_at  # the whole area clears this by 10%

    module, name, shrink = SHRINK_POINT[layer]
    monkeypatch.setattr(module, name, shrink)
    pieces = parts(run(block(), min_area))

    assert len(pieces) == 1, f"{layer}: a whole area that passed MIN AREA was not drawn ({len(pieces)} pieces)"
    emitted = geodesic_area_sq_mi(pieces[0])
    assert emitted < min_area, (
        f"{layer}: the shrink did not take the shape under MIN AREA ({emitted:.0f} >= {min_area:.0f}), "
        "so this test checks nothing"
    )


def test_ifr_area_split_by_class_is_drawn_as_one_polygon_even_under_the_line():
    """
    The 2026-11-11 09Z F00 case: one component over MIN AREA, three classes,
    none dominant, each below MIN AREA. The sub-regions are absorbed into one
    polygon -- and that polygon is drawn even when the coarse trace and
    simplification leave it a few percent under the minimum (3,128 -> 2,923
    on the real cycle).
    """
    ceiling, vis3, precip = ZEROS.copy(), ZEROS.copy(), ZEROS.copy()
    ceiling[100:130, 100:124] = 80.0  # CIG, 24 columns
    vis3[100:130, 124:164] = 80.0     # VIS: BR for 20 columns, then PCPN for 20
    precip[100:130, 144:164] = 80.0
    fine_area = float(np.broadcast_to(cell_areas_sq_mi(GRID_SPEC, SHAPE), SHAPE)[100:130, 100:164].sum())

    def run(min_area):
        return polygonize_ifr_grid_v2(ceiling, vis3, ZEROS, precip, GRID_SPEC, CYCLE, 0,
                                      neighborhood_radius_nm=0.0, min_area_sq_mi=min_area)

    # Below every band, the split by class happens as before.
    split = run(500.0)
    assert len(parts(split)) == 3 and max(geodesic_area_sq_mi(p) for p in parts(split)) < 0.5 * fine_area

    # Just under the whole area's fine-grid size: the component passes, no band does.
    min_area = 0.999 * fine_area
    fc = run(min_area)
    (polygon,) = parts(fc)
    assert fc["features"][0]["properties"]["cause"] == "CIG/VIS"
    emitted = geodesic_area_sq_mi(polygon)
    assert emitted < min_area, f"emitted {emitted:.0f} is not under {min_area:.0f}, so this checks nothing"
    assert emitted > 0.9 * fine_area


# --- 3. the helper ------------------------------------------------------------

def test_drop_split_fragments_keeps_each_areas_largest_piece():
    big = box(-110, 40, -108, 42)         # ~19,000 sq mi
    small = box(-105, 40, -104.9, 40.1)   # ~59 sq mi
    other_big = box(-100, 40, -98, 42)
    tiny = box(-95, 40, -94.95, 40.05)    # ~15 sq mi
    kept, sources = drop_split_fragments(
        [small, MultiPolygon([big, small]), Polygon(), MultiPolygon([small, tiny]), MultiPolygon([big, other_big])],
        1000.0,
    )
    # A lone polygon below the minimum is a whole area judged earlier: kept.
    # A MultiPolygon loses its small parts but always keeps its largest.
    assert sources == [0, 1, 3, 4]
    assert kept[0].equals(small)
    assert kept[1].geom_type == "Polygon" and kept[1].equals(big)
    assert kept[2].geom_type == "Polygon" and kept[2].equals(small)
    assert kept[3].geom_type == "MultiPolygon" and len(kept[3].geoms) == 2


def test_drop_split_fragments_judges_a_group_together():
    """IFR v2: the pieces of one fine-grid region are one set, whatever polygons they are in."""
    big, small, lone = box(-110, 40, -108, 42), box(-105, 40, -104.9, 40.1), box(-100, 40, -99.9, 40.1)
    kept, sources = drop_split_fragments([big, small, lone], 1000.0, groups=[7, 7, 9])
    assert sources == [0, 2], "the sliver of region 7 goes; region 9's only piece stays"


# --- 4. every polygonizer ends with the fragment rule -------------------------

@pytest.mark.parametrize("path, function", [
    ("pipeline/hazards/ifr.py", "polygonize_ifr_grid"),
    ("pipeline/hazards/ifr.py", "polygonize_ifr_grid_v2"),
    ("pipeline/hazards/mtn_obsc.py", "polygonize_mtn_obsc_grid"),
    ("pipeline/hazards/tango_common.py", "polygonize_tango_grid"),
])
def test_the_fragment_rule_runs_after_every_splitting_step(path, function):
    """
    Structural: each polygonizer calls drop_split_fragments, and no
    smoothing, simplification or contouring call comes after it. A new
    step added after it would quietly reopen the gap.
    """
    tree = ast.parse((REPO_ROOT / path).read_text())
    (fn,) = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == function]
    calls = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
            calls.append((node.lineno, name))
    guard_lines = [line for line, name in calls if name == "drop_split_fragments"]
    assert guard_lines, f"{function} never drops its split fragments"
    shape_changing = {"smooth_polygon_boundary", "simplify", "_simplify_regions", "_contour_region",
                      "grid_to_polygons", "merge_nearby_polygons", "buffer"}
    late = [(line, name) for line, name in calls if name in shape_changing and line > max(guard_lines)]
    assert not late, f"{function}: shape-changing calls after the fragment rule: {late}"
