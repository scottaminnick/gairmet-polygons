"""
tests/test_ifr_label_grid.py
-------------------------------
Tests polygonize_ifr_grid_v2() -- the label-grid rewrite of IFR
polygonization (see the LABEL-GRID POLYGONIZATION comment block in
pipeline/hazards/ifr.py).

The properties being pinned here are STRUCTURAL, not cosmetic:
downstream FAA vendors cannot parse overlapping polygons, nested
polygons, or rings with holes, and v1 produces all three because it
closes and contours three layers independently in vector space. v2 is
supposed to make them impossible rather than rare, so these tests assert
the impossibility (and, for the overlap test, include a control proving
the assertion can actually fail).

v1 is deliberately untouched and keeps its own tests
(tests/test_ifr_cause_attribution.py, tests/test_ifr_boundary_clip.py);
nothing here should be read as a statement about v1 except
test_v1_v2_comparison_report(), which just prints the delta between the
two on the same cached grid.

Run with `-s` to see that report.
"""

import sys
from datetime import datetime
from pathlib import Path

import numpy as np
from scipy.ndimage import binary_fill_holes

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import shapely
from shapely.affinity import translate
from shapely.geometry import shape as shapely_shape

from pipeline.boundaries import CONUS_BOUNDARY_PATH, get_boundary_mask
from pipeline.hazards.ifr import (
    CONTOUR_RESOLUTION_DEG,
    DOMINANT_FRACTION,
    IFR_CLASS_LABELS,
    IFR_CLASS_VALUES,
    INCLUDE_FRACTION,
    _build_class_grid,
    _cell_dimensions_km,
    _close_envelope,
    _fully_inside_downsample,
    polygonize_ifr_grid,
    polygonize_ifr_grid_v2,
)
from pipeline.polygons import GridSpec, load_grid_cache, save_grid_cache
from pipeline.smoothing import gaussian_smooth

VALID_DATE = datetime(2026, 7, 14, 15)

# One domain shared by the rule tests (dominance, inclusion floor,
# enclosure, area filter): well inside the ARTCC boundary so the clip
# never interferes, and reused across tests so the boundary mask is
# rasterized once and then served from pipeline.boundaries' memo.
RULES_SPEC = GridSpec(west=-110.0, north=45.0, dx=0.05, dy=-0.05)
RULES_SHAPE = (200, 300)


def _blank_grids(shape=RULES_SHAPE):
    """(ceiling, vis<3SM, vis<1SM, precip), all zero."""
    return tuple(np.zeros(shape) for _ in range(4))


def _polygonize(grids, spec=RULES_SPEC, **kwargs):
    kwargs.setdefault("threshold_pct", 50.0)
    kwargs.setdefault("neighborhood_radius_nm", 50.0)
    kwargs.setdefault("min_area_sq_mi", 3000.0)
    return polygonize_ifr_grid_v2(*grids, spec, VALID_DATE, 0, **kwargs)


def _geometries(feature_collection):
    return [shapely_shape(f["geometry"]) for f in feature_collection["features"]]


def _labels(feature_collection):
    return [
        (f["properties"]["cause"], f["properties"].get("weather_type"))
        for f in feature_collection["features"]
    ]


def _points_in_two_polygons(geometries, spec, shape, step=4) -> int:
    """
    Samples points across the domain on a regular lattice and counts how
    many land inside more than one polygon. The requested formulation of
    the no-overlap check: a forecaster's "these two areas cover the same
    ground" question, asked at every sampled point rather than as a
    pairwise geometry predicate.
    """
    rows = np.arange(0, shape[0], step)
    cols = np.arange(0, shape[1], step)
    lats = spec.north + rows * spec.dy
    lons = spec.west + cols * spec.dx
    grid_lons, grid_lats = np.meshgrid(lons, lats)
    xs, ys = grid_lons.ravel(), grid_lats.ravel()

    hits = np.zeros(xs.shape, dtype=int)
    for geometry in geometries:
        hits += shapely.contains_xy(geometry, xs, ys).astype(int)
    return int((hits > 1).sum())


# ---------------------------------------------------------------------------
# A realistic-ish CONUS field, built once and reused: smoothed noise
# scaled so ~5-10% of cells cross threshold, which is the sort of
# coverage a real IFR morning has. Deterministic (fixed seed) so a
# failure is reproducible.
# ---------------------------------------------------------------------------

REALISTIC_SPEC = GridSpec(west=-125.0, north=50.0, dx=0.05, dy=-0.05)
REALISTIC_SHAPE = (520, 1180)
_realistic_cache: dict = {}


def _realistic_grids(tmp_path):
    """
    The four probability grids, round-tripped through
    pipeline.polygons' save_grid_cache/load_grid_cache -- the same
    cached-grid path the web app's recompute endpoint runs against,
    uint8 quantization included.
    """
    if "grids" not in _realistic_cache:
        rng = np.random.default_rng(20260814)

        def field(sigma=18.0, offset=1.0):
            smoothed = gaussian_smooth(rng.random(REALISTIC_SHAPE), sigma_cells=sigma)
            z = (smoothed - smoothed.mean()) / smoothed.std()
            return np.clip(50.0 + 25.0 * (z - offset), 0, 100)

        ceiling, other, precip = field(), field(), field(offset=1.4)
        # Visibility correlates with ceiling (a stratus deck restricts
        # both) but isn't identical to it, and vis<1SM is by definition
        # a subset of vis<3SM.
        vis3 = np.clip(0.6 * ceiling + 0.4 * other, 0, 100)
        vis1 = np.clip(vis3 - 12.0, 0, 100)

        cache_path = tmp_path / "ifr_realistic_grid.npz"
        save_grid_cache(
            cache_path,
            {"ceiling": ceiling, "visibility_3sm": vis3, "visibility_1sm": vis1, "precipitation": precip},
            REALISTIC_SPEC,
        )
        grids, spec = load_grid_cache(cache_path)
        _realistic_cache["grids"] = (
            grids["ceiling"], grids["visibility_3sm"], grids["visibility_1sm"], grids["precipitation"]
        )
        _realistic_cache["spec"] = spec
    return _realistic_cache["grids"], _realistic_cache["spec"]


def _realistic_v2(tmp_path):
    if "v2" not in _realistic_cache:
        grids, spec = _realistic_grids(tmp_path)
        _realistic_cache["v2"] = _polygonize(grids, spec=spec)
    return _realistic_cache["v2"]


# ---------------------------------------------------------------------------
# 1. No overlap -- the whole point of the rewrite.
# ---------------------------------------------------------------------------

def test_no_sampled_point_falls_inside_two_polygons(tmp_path):
    fc = _realistic_v2(tmp_path)
    geometries = _geometries(fc)
    assert len(geometries) > 1, "need several polygons for an overlap test to mean anything"

    overlapping = _points_in_two_polygons(geometries, REALISTIC_SPEC, REALISTIC_SHAPE)
    assert overlapping == 0, f"{overlapping} sampled points fall inside more than one polygon"


def test_the_overlap_check_actually_catches_an_overlap(tmp_path):
    """
    Control for the test above: break disjointness deliberately and
    confirm the sampler notices. Without this, a bug that returned no
    polygons at all -- or a sampler that never hit one -- would read as
    a pass.
    """
    geometries = _geometries(_realistic_v2(tmp_path))
    largest = max(geometries, key=lambda g: g.area)
    nudged = translate(largest, xoff=0.05, yoff=0.05)  # deliberately overlapping duplicate

    overlapping = _points_in_two_polygons(
        geometries + [nudged], REALISTIC_SPEC, REALISTIC_SHAPE
    )
    assert overlapping > 0, "sampler failed to detect a deliberately overlapping polygon"


# ---------------------------------------------------------------------------
# 2. No interior rings -- a GFA element is a simple ring.
# ---------------------------------------------------------------------------

def test_every_geometry_is_a_simple_single_ring_polygon(tmp_path):
    fc = _realistic_v2(tmp_path)
    assert len(fc["features"]) > 0

    for feature in fc["features"]:
        assert feature["geometry"]["type"] == "Polygon", (
            f"expected Polygon, got {feature['geometry']['type']} -- a MultiPolygon can't be "
            "a single GFA element"
        )
        rings = feature["geometry"]["coordinates"]
        assert len(rings) == 1, f"polygon has {len(rings)} rings; a GFA element cannot carry a hole"


# ---------------------------------------------------------------------------
# 3-5. The absorption rules: enclosure, inclusion floor, dominance.
# ---------------------------------------------------------------------------

def _blob_inside_br_area(precip_fraction_cols: int):
    """
    A large BR (visibility, no precip) area with a PCPN blob entirely
    inside it -- the geometry all three fraction tests share, with only
    the blob's size changing.

    The outer area is 120 x 220 cells; the blob is centred well clear of
    every edge, so it is genuinely enclosed rather than merely adjacent.
    """
    ceiling, vis3, vis1, precip = _blank_grids()
    vis3[40:160, 40:260] = 80.0
    rows = 60  # of 120 -> half the height
    row0 = 100 - rows // 2
    col0 = 150 - precip_fraction_cols // 2
    precip[row0:row0 + rows, col0:col0 + precip_fraction_cols] = 80.0
    return ceiling, vis3, vis1, precip


def test_enclosed_minority_blob_is_absorbed_and_dropped_from_the_label():
    """
    A PCPN blob at ~7% of the area, entirely inside a BR area. It is
    both below the inclusion floor AND enclosed, so it must not become
    its own polygon (which would be a nested ring) and must not reach
    the label.
    """
    grids = _blob_inside_br_area(precip_fraction_cols=30)  # 60x30 of 120x220 = 6.8%
    fc = _polygonize(grids)

    assert len(fc["features"]) == 1, "an enclosed minority blob must not become its own polygon"
    assert _labels(fc) == [("VIS", "BR")]


def test_inclusion_floor_puts_a_large_enough_blob_into_the_label():
    """
    Same geometry, PCPN at ~30% -- still enclosed (so still one polygon,
    since a hole is unrepresentable), but now over INCLUDE_FRACTION, so
    it earns its place in the weather string.
    """
    grids = _blob_inside_br_area(precip_fraction_cols=130)  # 60x130 of 120x220 = 29.5%
    fc = _polygonize(grids)

    assert len(fc["features"]) == 1
    assert _labels(fc) == [("VIS", "PCPN/BR")], (
        f"expected PCPN to clear the {INCLUDE_FRACTION:.0%} floor, got {_labels(fc)}"
    )


def test_dominant_class_collapses_the_component_into_one_area():
    """
    PCPN at 60% of the component, side by side with BR at 40% -- NOT
    enclosed, and the minority is comfortably over the inclusion floor,
    so absorption has no reason to fire. Only the dominance rule can
    produce a single area here, which is what makes this a test of that
    rule rather than of absorption.

    Without dominance this would be two polygons sharing a boundary --
    a legal but needlessly fussy answer to "the whole area is
    precipitation-driven with a drier eastern third".
    """
    ceiling, vis3, vis1, precip = _blank_grids()
    vis3[40:160, 40:260] = 80.0
    precip[40:160, 40:172] = 80.0  # 132 of 220 columns = 60%

    fc = _polygonize((ceiling, vis3, vis1, precip))

    assert len(fc["features"]) == 1, (
        f"one class covers {0.6:.0%} >= DOMINANT_FRACTION ({DOMINANT_FRACTION:.0%}), so the "
        f"component should be drawn whole; got {len(fc['features'])} polygons"
    )
    assert _labels(fc) == [("VIS", "PCPN/BR")]


# ---------------------------------------------------------------------------
# 6. The area filter applies to components, never to sub-regions.
# ---------------------------------------------------------------------------

def test_area_filter_keeps_a_component_whose_sub_regions_are_each_too_small():
    """
    Three bands (CIG 40%, BR 33%, PCPN 27%) in one component: no class
    is dominant and none is below the inclusion floor, so all three
    survive as separate regions. Each band on its own is below
    min_area_sq_mi; together they are well over it.

    v1's failure mode was exactly this: the area filter ran per polygon,
    so a sub-area that failed it left a hole in the middle of a larger
    area. Here the filter has already been applied to the whole
    component, so either all three are drawn or none is.
    """
    ceiling, vis3, vis1, precip = _blank_grids()
    ceiling[90:110, 60:84] = 80.0                       # CIG band, 24 cols
    vis3[90:110, 84:104] = 80.0                         # BR band, 20 cols
    vis3[90:110, 104:120] = 80.0                        # PCPN band, 16 cols
    precip[90:110, 104:120] = 80.0

    # An isolated speck far away, to confirm the filter still bites.
    ceiling[20:30, 250:260] = 80.0

    fc = _polygonize((ceiling, vis3, vis1, precip), neighborhood_radius_nm=0.0, min_area_sq_mi=5000.0)

    causes = sorted(cause for cause, _weather in _labels(fc))
    assert causes == ["CIG", "VIS", "VIS"], (
        f"expected all three sub-regions of the surviving component, got {_labels(fc)}"
    )

    # The speck (~900 sq mi) is far below the 5,000 sq mi filter.
    speck = shapely.Point(RULES_SPEC.west + 255 * RULES_SPEC.dx, RULES_SPEC.north + 25 * RULES_SPEC.dy)
    assert not any(g.contains(speck) for g in _geometries(fc)), "sub-threshold component was not dropped"


# ---------------------------------------------------------------------------
# 7. Editability in NMAP -- vertex count at the default contour
#    resolution.
# ---------------------------------------------------------------------------

def test_conus_scale_polygon_stays_editable(tmp_path):
    """
    A forecaster has to be able to drag this shape around in NMAP. The
    raster is coarsened to CONTOUR_RESOLUTION_DEG before contouring
    precisely so the vertex count stays in that range without any
    per-polygon simplification (which would break the shared-boundary
    guarantee -- see polygonize_ifr_grid_v2()).

    Marching squares emits roughly one vertex per coarse cell of
    perimeter, so this scales with a polygon's PERIMETER, not its area:
    the bound below is generous enough for a genuinely ragged shape and
    still an order of magnitude under what the un-coarsened grid would
    produce.
    """
    geometries = _geometries(_realistic_v2(tmp_path))
    largest = max(geometries, key=lambda g: g.area)
    vertices = len(largest.exterior.coords)

    print(f"\n[vertex count] largest polygon: {vertices} vertices, {largest.area:.1f} sq deg")
    assert vertices < 400, f"largest polygon has {vertices} vertices -- too fiddly to edit by hand"


# ---------------------------------------------------------------------------
# 8. Border spill: the raster closing must not push across the ARTCC
#    boundary.
# ---------------------------------------------------------------------------

def _covered_outside_cells(feature_collection, spec, shape, inside_artcc):
    """(count, max distance from the boundary in cells) for ground outside
    the ARTCC boundary that ended up inside a polygon."""
    from scipy.ndimage import distance_transform_edt

    rows, cols = np.indices(shape)
    lons = spec.west + cols * spec.dx
    lats = spec.north + rows * spec.dy
    covered = np.zeros(shape, dtype=bool)
    for geometry in _geometries(feature_collection):
        covered |= shapely.contains_xy(geometry, lons.ravel(), lats.ravel()).reshape(shape)

    outside_covered = covered & ~inside_artcc
    distance_from_boundary = distance_transform_edt(~inside_artcc)
    distances = distance_from_boundary[outside_covered]
    return int(outside_covered.sum()), float(distances.max()) if distances.size else 0.0


def test_closing_does_not_spill_across_the_artcc_boundary(monkeypatch):
    """
    The gap left open by the ARTCC clip work: that clip masks the grids
    BEFORE polygonization, which is all v1 can do, so a closing applied
    afterwards can still bridge across a concave stretch of the
    boundary. v2 re-applies the mask after the closing, and a second
    time on the coarse grid it actually contours.

    Set up over the Great Lakes -- the worst concavity in the domain --
    with hazard filling the whole US side and the radius at 100 nm. The
    test first confirms the closing genuinely WOULD spill there
    (otherwise it proves nothing), then pins two things: nothing lands
    deep across the line, and the coarse-grid masking is load-bearing
    rather than decorative.

    The residue it does allow -- a couple of cells within one cell of
    the boundary -- is the corner artefact described in
    _fully_inside_downsample(): marching squares cuts a staircase corner
    diagonally, so a sliver of the first excluded block falls inside.
    That is a property of contouring a cell-centred raster at all, not
    of the closing, and it cannot grow with the radius.
    """
    spec = GridSpec(west=-95.0, north=51.0, dx=0.05, dy=-0.05)
    shape = (200, 300)
    inside_artcc = get_boundary_mask(spec, shape, CONUS_BOUNDARY_PATH)

    ceiling = np.where(inside_artcc, 80.0, 0.0)
    zeros = np.zeros(shape)

    # What the closing does BEFORE the mask is re-applied -- i.e. what
    # v1's ordering would leave in the output.
    class_grid = _build_class_grid(ceiling, zeros, zeros, zeros, 50.0)
    closed, _classes = _close_envelope(class_grid, 100.0, _cell_dimensions_km(spec, shape))
    spilled = closed & ~inside_artcc
    assert spilled.sum() > 100, (
        "the closing didn't spill across the boundary here, so this test can't detect the fix "
        f"(only {spilled.sum()} cells)"
    )

    def run():
        return polygonize_ifr_grid_v2(
            ceiling, zeros, zeros, zeros, spec, VALID_DATE, 0,
            threshold_pct=50.0, neighborhood_radius_nm=100.0, min_area_sq_mi=3000.0,
        )

    covered, worst_distance = _covered_outside_cells(run(), spec, shape, inside_artcc)
    print(f"\n[border] {covered} cells outside the boundary covered, worst {worst_distance:.1f} cells out")

    assert worst_distance <= 1.0, (
        f"a polygon reaches {worst_distance:.1f} cells ({worst_distance * spec.dx:.2f} deg) past the "
        "ARTCC boundary -- the closing is getting across the line, not just cutting a corner"
    )
    assert covered <= 5, f"{covered} cells outside the ARTCC boundary are covered; expected a corner sliver at most"

    # Control: without the coarse-grid mask (i.e. the fine-grid clip
    # alone, which is all v1 can manage), the majority reduce pulls in
    # far more foreign ground. If this ever stops being true, the
    # assertions above have stopped testing anything.
    import pipeline.hazards.ifr as ifr_module

    monkeypatch.setattr(
        ifr_module,
        "_fully_inside_downsample",
        lambda mask, factor_y, factor_x: np.ones(
            (mask.shape[0] // factor_y, mask.shape[1] // factor_x), dtype=bool
        ),
    )
    unmasked_covered, _worst = _covered_outside_cells(run(), spec, shape, inside_artcc)
    print(f"[border] without the coarse-grid mask: {unmasked_covered} cells covered")
    assert unmasked_covered > 50, (
        f"only {unmasked_covered} outside cells covered without the coarse-grid mask -- that mask "
        "is supposed to be what keeps this number near zero"
    )


# ---------------------------------------------------------------------------
# Enclosure absorption: exactly which configurations get absorbed, and
# what happens to the one that doesn't. Pinned because the obvious
# simplification of _absorb_holes() -- "is this sub-region's ring made
# entirely of one other sub-region" -- gives a different answer to the
# first of these, and would look like a harmless refactor.
# ---------------------------------------------------------------------------

def _three_region_component(pocket_rows, pocket_cols, divider_col):
    """
    One component split three ways: CIG on the left, VIS/BR on the
    right, and a VIS/PCPN pocket at (pocket_rows, pocket_cols) that
    straddles the divider between them. All three land above
    INCLUDE_FRACTION and none reaches DOMINANT_FRACTION, so neither the
    inclusion floor nor the dominance rule can fire -- absorption is the
    only thing that could merge anything.
    """
    ceiling, vis3, vis1, precip = _blank_grids()
    component = np.zeros(RULES_SHAPE, dtype=bool)
    component[40:160, 40:250] = True
    pocket = np.zeros(RULES_SHAPE, dtype=bool)
    pocket[pocket_rows, pocket_cols] = True
    left = np.zeros(RULES_SHAPE, dtype=bool)
    left[:, :divider_col] = True

    cig = component & left & ~pocket
    br = component & ~left & ~pocket
    ceiling[cig] = 80.0
    vis3[br | pocket] = 80.0
    precip[pocket] = 80.0
    return (ceiling, vis3, vis1, precip), component, cig, br, pocket


def test_pocket_enclosed_jointly_by_two_regions_stays_its_own_area():
    """
    THE SHARED-HOLE CASE: a pocket sitting in a notch that two regions
    form TOGETHER. Neither surrounding region has a hole of its own --
    each just has a bite out of one edge -- so _absorb_holes() finds
    nothing to absorb and the pocket survives as its own area.

    That is the correct outcome, not a miss: the export invariant is
    "no region has an interior ring", and here none does. Absorbing the
    pocket would merge two thirds of the component into one area for no
    reason a forecaster would recognise.

    What this pins is that the DECISION is made by asking each region
    about its own holes. A single-neighbour formulation ("is this
    sub-region surrounded by exactly one other") answers this case the
    same way by accident, but answers
    test_region_wrapping_two_others_absorbs_both() wrongly -- see there.
    """
    grids, component, cig, br, pocket = _three_region_component(
        pocket_rows=slice(60, 140), pocket_cols=slice(100, 195), divider_col=145
    )

    # The premise: neither surrounding region has a hole on its own.
    assert not (binary_fill_holes(cig) & ~cig).any()
    assert not (binary_fill_holes(br) & ~br).any()
    # ...but the pocket is genuinely enclosed by the two of them together.
    surrounding = cig | br
    assert not (binary_fill_holes(surrounding) & ~surrounding & ~pocket).any()

    fc = _polygonize(grids, neighborhood_radius_nm=0.0)

    assert len(fc["features"]) == 3, (
        f"expected the pocket to survive alongside both neighbours, got {_labels(fc)}"
    )
    assert sorted(_labels(fc)) == [("CIG", None), ("VIS", "BR"), ("VIS", "PCPN/BR")]

    # And the thing that actually matters downstream still holds.
    geometries = _geometries(fc)
    for feature in fc["features"]:
        assert len(feature["geometry"]["coordinates"]) == 1
    for i, a in enumerate(geometries):
        for b in geometries[i + 1:]:
            assert a.intersection(b).area < 1e-9
            assert not a.contains(b) and not b.contains(a)


def test_pocket_enclosed_jointly_survives_the_export_disjointness_check():
    """
    The same output through pipeline.pgen_xml.assert_rings_disjoint(),
    which is what the PGEN export runs before serializing. Three areas
    meeting along shared edges must pass -- if the export check were too
    strict about coincident boundaries, this is the shape that would
    expose it.
    """
    from pipeline.pgen_xml import assert_rings_disjoint

    grids, *_ = _three_region_component(
        pocket_rows=slice(60, 140), pocket_cols=slice(100, 195), divider_col=145
    )
    fc = _polygonize(grids, neighborhood_radius_nm=0.0)

    rings = [
        [(lat, lon) for lon, lat in feature["geometry"]["coordinates"][0][:-1]]
        for feature in fc["features"]
    ]
    assert_rings_disjoint(rings, "IFR", 0)  # raises if the guarantee doesn't hold


def test_region_wrapping_two_others_absorbs_both():
    """
    The case _absorb_holes() exists for, and the one a single-neighbour
    formulation gets wrong: one region wraps TWO others together, so its
    own outline has an interior ring while neither enclosed region is
    surrounded by a single neighbour. Both get absorbed; the survivor is
    solid.
    """
    ceiling, vis3, vis1, precip = _blank_grids()
    component = np.zeros(RULES_SHAPE, dtype=bool)
    component[40:160, 40:250] = True
    interior = np.zeros(RULES_SHAPE, dtype=bool)
    interior[55:145, 70:238] = True          # 60% of the component
    left = np.zeros(RULES_SHAPE, dtype=bool)
    left[:, :154] = True

    frame = component & ~interior            # 40% -- under DOMINANT_FRACTION
    ceiling[frame] = 80.0
    vis3[interior] = 80.0
    precip[interior & ~left] = 80.0

    assert (binary_fill_holes(frame) & ~frame).any(), "the wrapping region must have a hole"

    fc = _polygonize((ceiling, vis3, vis1, precip), neighborhood_radius_nm=0.0)

    assert len(fc["features"]) == 1, f"expected one absorbed area, got {_labels(fc)}"
    assert _labels(fc) == [("CIG/VIS", "PCPN/BR")]
    assert len(fc["features"][0]["geometry"]["coordinates"]) == 1


# ---------------------------------------------------------------------------
# The class grid itself -- referenced by name, per the mapping being
# module-level for exactly this reason.
# ---------------------------------------------------------------------------

def test_class_grid_assigns_exactly_one_class_per_cell():
    ceiling = np.array([[80.0, 80.0, 0.0, 0.0, 80.0]])
    vis3 = np.array([[0.0, 80.0, 80.0, 80.0, 80.0]])
    vis1 = np.array([[0.0, 0.0, 80.0, 0.0, 80.0]])
    precip = np.array([[0.0, 0.0, 80.0, 0.0, 0.0]])

    classes = _build_class_grid(ceiling, vis3, vis1, precip, 50.0)

    assert list(classes[0]) == [
        IFR_CLASS_VALUES[("CIG", None)],       # ceiling only
        IFR_CLASS_VALUES[("CIG/VIS", "BR")],   # both, no precip and no fog
        IFR_CLASS_VALUES[("VIS", "PCPN")],     # fog AND precip -> PCPN wins
        IFR_CLASS_VALUES[("VIS", "BR")],       # visibility only, nothing more specific
        IFR_CLASS_VALUES[("CIG/VIS", "FG")],   # both, fog without precip
    ]
    assert set(IFR_CLASS_LABELS) == set(range(1, 8)), "seven classes plus 0 for no hazard"


# ---------------------------------------------------------------------------
# v1 vs v2 on the same cached grid -- REPORT ONLY, no assertions. Run
# the suite with -s to see it.
# ---------------------------------------------------------------------------

def test_v1_v2_comparison_report(tmp_path):
    grids, spec = _realistic_grids(tmp_path)

    print("\n" + "=" * 72)
    print("v1 (vector layers) vs v2 (label grid) -- same cached grid, same parameters")
    print("=" * 72)
    print(f"{'radius':>7} {'impl':>4} {'polys':>6} {'area sq deg':>12} {'overlaps':>9} {'nested':>7} {'max verts':>10}")

    for radius_nm in (0.0, 50.0, 100.0):
        for name, implementation in (("v1", polygonize_ifr_grid), ("v2", polygonize_ifr_grid_v2)):
            fc = implementation(
                *grids, spec, VALID_DATE, 0,
                threshold_pct=50.0, neighborhood_radius_nm=radius_nm, min_area_sq_mi=3000.0,
            )
            geometries = _geometries(fc)
            overlaps = 0
            nested = 0
            for i, a in enumerate(geometries):
                for b in geometries[i + 1:]:
                    if a.intersection(b).area > 1e-9:
                        overlaps += 1
                        if a.contains(b) or b.contains(a):
                            nested += 1
            vertices = [
                len(g.exterior.coords) if g.geom_type == "Polygon"
                else max(len(p.exterior.coords) for p in g.geoms)
                for g in geometries
            ]
            print(
                f"{radius_nm:>7.0f} {name:>4} {len(geometries):>6} {sum(g.area for g in geometries):>12.1f} "
                f"{overlaps:>9} {nested:>7} {max(vertices, default=0):>10}"
            )
    print("=" * 72)


if __name__ == "__main__":
    import pytest

    pytest.main([__file__, "-v", "-s"])
