"""
tests/test_tag_tracking.py
-----------------------------
Feature tracking across forecast hours (pipeline/tag_tracking.py) -- the
five rules from the AWC G-AIRMET snapshot/tagging training doc.

Deliberately axis-aligned lon/lat boxes and no real data. Every rule in
the doc is a statement about which polygons touch which, so a fixture
that is anything more than "these boxes overlap and these do not" is
testing the polygonizer instead. Boxes also make the expected answer
readable from the test body, which matters here: the failure mode of a
tagging bug is not a crash, it is a plausible-looking file in which the
smear is drawn between the wrong two areas.

Each case names the rule it covers.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.tag_tracking import assign_tracked_tags


def box(lat0, lon0, half=1.0):
    """
    A square ring of (lat, lon) vertices centred on (lat0, lon0).

    (lat, lon) because that is the order the export path carries -- PGEN
    Point elements are Lat/Lon, and the module under test is what flips
    them to (lon, lat) for shapely. A fixture written the other way round
    would pass while the real call site was transposed.
    """
    return [
        (lat0 - half, lon0 - half),
        (lat0 - half, lon0 + half),
        (lat0 + half, lon0 + half),
        (lat0 + half, lon0 - half),
    ]


# --- rule 1: continuation --------------------------------------------------

def test_a_polygon_overlapping_the_previous_hour_keeps_its_tag():
    """The base case the other four are variations on."""
    assert assign_tracked_tags([
        [box(40.0, -100.0)],
        [box(40.5, -100.5)],          # shifted, still overlapping
        [box(41.0, -101.0)],
    ]) == [[1], [1], [1]]


def test_touching_along_an_edge_counts_as_intersecting():
    """
    "Intersects" is any contact, boundary touching included -- shapely's
    .intersects, not an area threshold. Two boxes sharing only an edge
    overlap in ZERO area, and an area-based rule would call them
    unrelated. The label-grid polygonizer produces exactly this shape of
    contact routinely, so getting it wrong would break tracking on the
    most common case rather than an exotic one.
    """
    assert assign_tracked_tags([
        [box(40.0, -100.0)],
        [box(40.0, -98.0)],           # shares the lon=-99 edge, no area in common
    ]) == [[1], [1]]


# --- rule 3: split ---------------------------------------------------------

def test_a_split_gives_every_child_the_parents_tag():
    """
    The doc's PA-to-SC IFR example: one area at F00 breaks into two by
    F03, and one of those persists to F06. All of it is one hazard, so
    all of it carries tag 1 -- including the two polygons that share the
    tag within F03, which is legal rather than a collision.
    """
    parent = box(40.0, -100.0, half=4.0)
    assert assign_tracked_tags([
        [parent],
        [box(37.5, -102.5, half=1.0), box(42.5, -97.5, half=1.0)],
        [box(37.5, -102.5, half=0.5)],
    ]) == [[1], [1, 1], [1]]


# --- rule 4: merge ---------------------------------------------------------

def test_a_merge_takes_the_lowest_parent_tag():
    """
    Two separate areas at F00 become one at F03. The child takes tag 1;
    tag 2 ends by absence (rule 2), which needs no code of its own --
    nothing carries it at the next hour and that is the whole
    representation of "ended".
    """
    assert assign_tracked_tags([
        [box(40.0, -105.0), box(40.0, -95.0)],
        [box(40.0, -100.0, half=8.0)],          # covers both
    ]) == [[1, 2], [1]]


def test_the_lowest_tag_wins_regardless_of_input_order():
    """
    The rule is lowest tag, not first match. Input order is an artifact
    of how the polygonizer happened to emit features, and letting it
    decide would make the tag on a merged area unstable between runs
    that produced the same geometry.
    """
    # Two parents whose tags are 1 and 2; the child is built so the
    # HIGHER-tagged parent is the one it meets first in input order.
    tags = assign_tracked_tags([
        [box(40.0, -95.0), box(40.0, -105.0)],   # tag 1 east, tag 2 west
        [box(40.0, -100.0, half=8.0)],
    ])
    assert tags == [[1, 2], [1]]


# --- rule 5: new, and no reuse of ended numbers ----------------------------

def test_a_polygon_touching_nothing_takes_the_next_unused_number():
    assert assign_tracked_tags([
        [box(40.0, -100.0)],
        [box(20.0, -60.0)],           # nowhere near it
    ]) == [[1], [2]]


def test_a_feature_absent_for_one_hour_returns_with_a_new_tag():
    """
    Only the PREVIOUS hour is consulted, so a gap ends the hazard even if
    the same ground is covered again later. Per the doc: a three-hour
    absence is a new hazard, not a continuation, and the smear must not
    be drawn across the gap.
    """
    same = box(40.0, -100.0)
    assert assign_tracked_tags([[same], [], [same]]) == [[1], [], [2]]


def test_an_ended_tag_number_is_never_recycled():
    """
    Tag 2 ends at F03 and its NUMBER stays retired: the new area at F06
    takes 3. Recycling would make one number mean two different hazards
    within a single file, which is exactly the kind of thing that reads
    as correct right up until somebody builds a smear from it.
    """
    assert assign_tracked_tags([
        [box(40.0, -100.0), box(30.0, -80.0)],            # A -> 1, B -> 2
        [box(40.2, -100.2)],                              # A' over A; B ends
        [box(40.4, -100.4), box(10.0, -60.0)],            # A'' over A', plus new C
    ]) == [[1, 2], [1], [1, 3]]


# --- shape, ordering and robustness ---------------------------------------

def test_the_first_hour_is_numbered_in_input_order():
    """
    Nothing precedes it, so every polygon there is new by definition.
    Input order is the only ordering available and the caller zips the
    result straight back onto its rings.
    """
    assert assign_tracked_tags([
        [box(40.0, -100.0), box(30.0, -80.0), box(20.0, -60.0)]
    ]) == [[1, 2, 3]]


def test_the_result_has_exactly_the_shape_of_the_input():
    """
    build_pgen_document zips tags onto rings positionally. A length
    mismatch anywhere would silently attach tags to the wrong polygons
    rather than raise.
    """
    rings_by_hour = [
        [box(40.0, -100.0), box(30.0, -80.0)],
        [],
        [box(40.0, -100.0)],
        [box(40.0, -100.0), box(41.0, -101.0), box(10.0, -60.0)],
        [],
    ]
    tags = assign_tracked_tags(rings_by_hour)
    assert [len(hour) for hour in tags] == [len(hour) for hour in rings_by_hour]
    assert all(isinstance(tag, int) for hour in tags for tag in hour)


def test_an_empty_cycle_produces_an_empty_result():
    assert assign_tracked_tags([[], [], [], [], []]) == [[], [], [], [], []]
    assert assign_tracked_tags([]) == []


def test_a_first_hour_with_nothing_in_it_still_numbers_from_one():
    """
    A cycle whose hazard does not appear until F03. next_tag must not
    have been advanced by the empty hour.
    """
    assert assign_tracked_tags([[], [box(40.0, -100.0)]]) == [[], [1]]


def test_a_self_intersecting_ring_is_repaired_rather_than_raising():
    """
    buffer(0) is there because this runs while a PGEN document is being
    assembled: an exception loses the whole file, not one polygon. A
    bowtie is the shape simplification upstream can produce, and every
    shapely predicate on an invalid geometry is undefined behaviour
    otherwise.
    """
    bowtie = [(40.0, -101.0), (41.0, -99.0), (40.0, -99.0), (41.0, -101.0)]
    tags = assign_tracked_tags([[bowtie], [box(40.5, -100.0, half=0.4)]])
    assert tags == [[1], [1]], "the repaired ring should still track"


def test_a_degenerate_ring_takes_a_tag_instead_of_stopping_the_export():
    """
    Fewer than three vertices cannot be a polygon. Callers drop these
    before they get here, so this is the belt to that braces -- it must
    keep the result's shape and not raise.
    """
    tags = assign_tracked_tags([[box(40.0, -100.0)], [[(40.0, -100.0), (40.1, -100.1)]]])
    assert tags == [[1], [2]], "a degenerate ring intersects nothing, so it is new"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
