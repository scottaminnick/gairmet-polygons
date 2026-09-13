"""
tests/test_ifr_arc_simplify.py
-------------------------------
Shared-arc simplification of IFR v2 output (pipeline.polygons.
simplify_shared_arcs, applied by pipeline.hazards.ifr._simplify_regions).

WHAT WENT WRONG: the label-grid polygonizer deliberately skipped every
kind of simplification because per-ring Douglas-Peucker unshares the
boundaries adjacent areas trace together. That left the staircase from
marching squares intact -- 484 vertices across F03 and 1,064 across F09
on the 13/03Z cycle -- and the NMAP2 VG converter fell over on the
count, while MTN OBSC (thinned to 25 per ring on export) went through.

The contract, each half with a control:

  - a shared edge is vertex-identical from both sides AFTER
    simplification (per-ring DP is the control: it is not)
  - a ring that shares nothing still simplifies (closed-ring split)
  - no gaps and no overlaps are introduced: the union of the areas
    keeps its area and its pairwise intersections stay at zero
  - tolerance 0 is the identity, and the v2 polygonizer returns the
    unsimplified rings when simplification cannot be made sound
"""

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.polygons import simplify_shared_arcs  # noqa: E402


# ---------------------------------------------------------------------------
# The arc operation on its own (no shapely).
# ---------------------------------------------------------------------------

def _wiggle(x, y0, y1, n=21, amplitude=0.02):
    """A vertical edge from (x, y0) to (x, y1) with a zig-zag on it."""
    ys = np.linspace(y0, y1, n)
    return [(x + (amplitude if i % 2 else -amplitude), float(y)) for i, y in enumerate(ys)]


def _two_squares_sharing_an_edge():
    """Left square A and right square B meeting along a wiggly x=1 edge."""
    shared = _wiggle(1.0, 0.0, 2.0)
    shared[0] = (1.0, 0.0)
    shared[-1] = (1.0, 2.0)
    a = [(0.0, 0.0), (1.0, 0.0)] + shared[1:-1] + [(1.0, 2.0), (0.0, 2.0)]
    b = [(1.0, 0.0), (2.0, 0.0), (2.0, 2.0), (1.0, 2.0)] + shared[1:-1][::-1]
    return a, b, shared


def _shared_vertices(ring_a, ring_b):
    return sorted(set(ring_a) & set(ring_b))


def test_shared_edge_stays_vertex_identical_from_both_sides():
    a, b, shared = _two_squares_sharing_an_edge()
    assert len(_shared_vertices(a, b)) == len(shared), "premise: the input rings share the whole edge"

    out_a, out_b = simplify_shared_arcs([a, b], tolerance_deg=0.05)

    edge_a = sorted(v for v in out_a if abs(v[0] - 1.0) < 0.05)
    edge_b = sorted(v for v in out_b if abs(v[0] - 1.0) < 0.05)
    assert edge_a == edge_b, f"shared edge differs after simplification: {edge_a} vs {edge_b}"
    assert edge_a == [(1.0, 0.0), (1.0, 2.0)], "the wiggle should have simplified to its two junctions"
    assert len(out_a) == 4 and len(out_b) == 4


def test_per_ring_simplification_is_the_control_that_unshares_the_edge():
    """
    Douglas-Peucker on each ring independently starts from a different
    vertex and keeps different wiggle points, so the two copies of the
    edge diverge. This is the failure the arc approach exists to avoid;
    if it ever stops happening the test above proves nothing.
    """
    from pipeline.polygons import _douglas_peucker

    a, b, _ = _two_squares_sharing_an_edge()
    # A mid-sized tolerance keeps some wiggle vertices, which is what
    # exposes the divergence.
    dp_a = _douglas_peucker(np.asarray(a + [a[0]], float), 0.015)
    dp_b = _douglas_peucker(np.asarray(b + [b[0]], float), 0.015)
    edge_a = sorted(tuple(v) for v in dp_a if abs(v[0] - 1.0) < 0.05)
    edge_b = sorted(tuple(v) for v in dp_b if abs(v[0] - 1.0) < 0.05)
    assert edge_a != edge_b, "per-ring DP kept the edge shared here, so the control is void"


def test_three_regions_meeting_at_a_junction():
    """
    A T-junction: C sits under both A and B. The junction vertices are
    kept by every ring that passes through them, so all three still
    meet at a point rather than a sliver.
    """
    a, b, shared = _two_squares_sharing_an_edge()
    bottom = [(x, 0.0) for x in np.linspace(0.0, 2.0, 41)]
    c = bottom + [(2.0, -1.0), (0.0, -1.0)]
    a2 = [(0.0, 0.0)] + [(x, 0.0) for x in np.linspace(0.0, 1.0, 21)][1:] + shared[1:-1] + [(1.0, 2.0), (0.0, 2.0)]
    b2 = [(1.0, 0.0)] + [(x, 0.0) for x in np.linspace(1.0, 2.0, 21)][1:] + [(2.0, 2.0), (1.0, 2.0)] + shared[1:-1][::-1]
    # Rounded so the bottom edge is bit-identical between B and C: exact
    # sharing is the contract, and linspace(0, 2, 41) vs linspace(1, 2, 21)
    # differ in the last bit otherwise.
    a2 = [(round(float(x), 6), round(float(y), 6)) for x, y in a2]
    b2 = [(round(float(x), 6), round(float(y), 6)) for x, y in b2]
    c = [(round(float(x), 6), round(float(y), 6)) for x, y in c]

    out = simplify_shared_arcs([a2, b2, c], tolerance_deg=0.05)
    for ring in out:
        assert (1.0, 0.0) in ring, "the three-way junction must survive in every ring that touches it"
    assert [len(r) for r in out] == [4, 4, 5], [len(r) for r in out]


def test_ring_sharing_nothing_still_simplifies():
    circle = [(np.cos(t), np.sin(t)) for t in np.linspace(0, 2 * np.pi, 200, endpoint=False)]
    circle = [(float(x), float(y)) for x, y in circle]
    out, = simplify_shared_arcs([circle], tolerance_deg=0.02)
    assert 8 <= len(out) < 60, f"a lone ring came back with {len(out)} vertices"
    assert len(out) < len(circle)


def test_tolerance_zero_is_identity_and_tiny_rings_never_collapse():
    a, b, _ = _two_squares_sharing_an_edge()
    same = simplify_shared_arcs([a, b], tolerance_deg=0.0)
    assert same == [a, b]

    tiny = [(0.0, 0.0), (0.001, 0.0), (0.001, 0.001), (0.0, 0.001)]
    out, = simplify_shared_arcs([tiny], tolerance_deg=1.0)
    assert len(out) >= 3, "a ring must never be reduced below three vertices"


# ---------------------------------------------------------------------------
# Through polygonize_ifr_grid_v2(): shapely and the boundary files needed.
# ---------------------------------------------------------------------------

shapely = pytest.importorskip("shapely")

import pipeline.hazards.ifr as ifr  # noqa: E402
from test_ifr_label_grid import _geometries, _polygonize, _three_region_component  # noqa: E402

# Captured before any monkeypatching, so a test that first runs at 0 can
# still ask for the shipped default afterwards.
DEFAULT_TOLERANCE = ifr.ARC_SIMPLIFY_TOLERANCE_DEG


def _v2(monkeypatch, tolerance):
    monkeypatch.setattr(ifr, "ARC_SIMPLIFY_TOLERANCE_DEG", tolerance)
    grids, *_ = _three_region_component(
        pocket_rows=slice(60, 140), pocket_cols=slice(100, 195), divider_col=145
    )
    return _polygonize(grids, neighborhood_radius_nm=0.0)


def test_v2_output_is_thinned_without_gaps_or_overlaps(monkeypatch):
    raw = _geometries(_v2(monkeypatch, 0.0))
    thin = _geometries(_v2(monkeypatch, DEFAULT_TOLERANCE))
    assert len(raw) == len(thin) == 3

    before = sum(len(g.exterior.coords) for g in raw)
    after = sum(len(g.exterior.coords) for g in thin)
    print(f"\n[arc simplify] three-region scenario: {before} -> {after} vertices")
    assert after < before, "simplification changed nothing"

    # No gaps beyond what the tolerance allows. Douglas-Peucker replaces
    # runs of vertices with chords that can sit up to the tolerance off
    # the original line, so the union's area can move by at most
    # tolerance x perimeter (in practice much less: on this rectangular
    # scenario the half-cell corner chamfers tilt every edge inward by
    # ~0.05 deg, about 1.6% of the area). A GAP between neighbours would
    # show up here as well, since it subtracts from the union.
    union_before = shapely.unary_union(raw)
    union_after = shapely.unary_union(thin)
    allowed = DEFAULT_TOLERANCE * union_before.length
    assert abs(union_after.area - union_before.area) <= allowed, (
        f"union area moved by {abs(union_after.area - union_before.area):.3f} sq deg, "
        f"more than tolerance x perimeter ({allowed:.3f}) can explain"
    )
    # And nothing moved further than the tolerance: every simplified ring
    # lies within a tolerance-wide band of the raw outline.
    for g_raw, g_thin in zip(raw, thin):
        assert g_thin.hausdorff_distance(g_raw) <= DEFAULT_TOLERANCE * 1.01

    # No overlaps, and every ring valid and hole-free.
    for g in thin:
        assert g.is_valid and not list(g.interiors)
    for i, a in enumerate(thin):
        for b in thin[i + 1:]:
            assert a.intersection(b).area < 1e-9


def test_v2_neighbours_still_share_their_boundary_exactly(monkeypatch):
    thin = _geometries(_v2(monkeypatch, DEFAULT_TOLERANCE))
    coords = [set(map(tuple, g.exterior.coords)) for g in thin]
    # Every pair that touches shares at least two vertices (a whole edge),
    # and the shared stretch is identical from both sides by construction:
    # the vertices are the same tuples, not merely close.
    touching = 0
    for i in range(len(thin)):
        for j in range(i + 1, len(thin)):
            if thin[i].touches(thin[j]) or thin[i].intersection(thin[j]).length > 0:
                touching += 1
                assert len(coords[i] & coords[j]) >= 2, "touching regions no longer share vertices"
    assert touching >= 2, "scenario premise: the three regions touch each other"


def test_v2_falls_back_to_raw_rings_when_simplification_is_unsound(monkeypatch):
    """
    The back-off: if every tolerance fails validation, the raw rings are
    returned rather than an overlapping or invalid set.
    """
    monkeypatch.setattr(ifr, "_regions_are_sound", lambda polygons: False)
    raw = _geometries(_v2(monkeypatch, 0.0))
    monkeypatch.setattr(ifr, "_regions_are_sound", lambda polygons: False)
    fallback = _geometries(_v2(monkeypatch, 0.15))
    assert [len(g.exterior.coords) for g in fallback] == [len(g.exterior.coords) for g in raw]
