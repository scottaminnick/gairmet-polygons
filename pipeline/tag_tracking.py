"""
pipeline/tag_tracking.py
---------------------------
G-AIRMET feature tracking: which polygons at F00/F03/F06/F09/F12 are the
same hazard, expressed as the `tag` attribute on each Gfa element.

WHAT A TAG IS FOR. The five forecast hours are snapshots of one
time-evolving hazard, and the tag is the only thing that says which
snapshots belong together. NMAP2 draws polygons sharing a tag as one
feature through time, and the BUFR smear -- the swept area between
consecutive snapshots -- is built by walking a tag from hour to hour. A
tag scheme that cannot express "this is the same area, three hours
later" cannot produce a smear at all.

This replaces unique-sequential numbering, which gave every polygon its
own tag and so asserted that nothing was ever the same hazard twice.

THE RULES, from the AWC G-AIRMET snapshot/tagging training document,
which is the source for all five:

  1. CONTINUATION. A polygon at hour N that intersects a polygon at hour
     N-1 inherits that polygon's tag.

  2. ENDING. A hazard ends by ABSENCE: no polygon carries its tag at the
     next hour. There is nothing to write for this -- it falls out of
     rule 1 rather than needing a rule of its own.

  3. SPLIT. One parent, several children: all children take the parent's
     tag. Several polygons sharing a tag within one hour is legal, not a
     collision.

  4. MERGE. Several parents, one child: the child takes the LOWEST
     parent tag. The other parent tags end by absence (rule 2).

  5. NEW. No intersection with any previous-hour polygon: the next
     unused integer. Numbers belonging to ended tags are never reused.

DECISIONS, settled in advance and deliberately NOT configurable:

  - "Intersects" means ANY contact, boundary touching included. This is
    shapely's .intersects, not an area threshold: two areas meeting
    along a shared edge intersect in zero area but are plainly the same
    hazard continuing, and the label-grid polygonizer produces exactly
    that shape of contact routinely.
  - Each hazard layer is INDEPENDENT and starts at tag 1. The IFR file
    and the MT_OBSC file each number from 1; they do not share a
    counter.
  - Only the PREVIOUS hour is consulted. A feature that is absent for
    one snapshot and returns at the next comes back with a new tag,
    which is what the doc specifies -- a three-hour gap is a new hazard,
    not a continuation.
  - Standard hours only (0/3/6/9/12). No specials.
  - `tag` stays NUMERIC. The desk letter is already a separate Gfa
    attribute and does not belong in here.
"""

from __future__ import annotations

from shapely.geometry import Polygon


def _polygon(ring):
    """
    One ring of (lat, lon) vertices as a shapely Polygon in (lon, lat).

    TWO THINGS GUARD AGAINST RAISING MID-EXPORT, because this runs while
    a PGEN document is being assembled and a crash here loses the whole
    file rather than one polygon:

      - buffer(0), the standard shapely repair. A self-intersecting ring
        -- which simplification upstream can produce -- makes every
        predicate on it undefined behaviour or an exception. buffer(0)
        turns it into a valid geometry (possibly a MultiPolygon, which
        .intersects handles the same way).
      - fewer than three vertices cannot be a polygon at all, so those
        become an EMPTY polygon. An empty geometry intersects nothing,
        so such a ring simply takes a new tag instead of stopping the
        export. Callers already drop these before they get here; this
        is the belt to that braces.
    """
    if len(ring) < 3:
        return Polygon()
    return Polygon([(lon, lat) for lat, lon in ring]).buffer(0)


def assign_tracked_tags(rings_by_hour):
    """
    Assign a tag to every polygon, tracking features across forecast hours.

    rings_by_hour : list, one entry per forecast hour in ASCENDING order,
                    each a list of rings, each ring a sequence of
                    (lat, lon) vertices.
    returns       : a list of the same shape holding integer tags.

    The first hour is numbered 1..n in input order -- there is no earlier
    hour for anything to continue from, so every polygon there is new by
    definition.

    Each later hour compares only against the hour immediately before it
    (see the module docstring on why), so this is a single forward pass
    carrying one hour of state.

    Note that min() over the intersecting tags is what implements BOTH
    rule 3 and rule 4, and it does so without either case being detected.
    A split is several children each finding the same single parent tag,
    so min() of one candidate returns it; a merge is one child finding
    several parent tags, so min() picks the lowest. Nothing has to
    classify which situation it is looking at, which is worth more than
    it sounds: the messy real cases are simultaneous splits and merges,
    and a classifier would have to pick one label for them.
    """
    tags_by_hour = []
    previous = []          # [(polygon, tag)] carried from the prior hour
    next_tag = 1

    for rings in rings_by_hour:
        polygons = [_polygon(ring) for ring in rings]
        tags = []

        for polygon in polygons:
            candidates = [tag for prev, tag in previous if polygon.intersects(prev)]
            if candidates:
                tags.append(min(candidates))      # rules 1, 3 and 4
            else:
                tags.append(next_tag)             # rule 5
                next_tag += 1

        tags_by_hour.append(tags)
        # Only this hour carries forward, so a hazard absent here cannot
        # be continued from the hour before it -- rule 2, and the
        # one-snapshot-gap decision, in one line.
        previous = list(zip(polygons, tags))

    return tags_by_hour
