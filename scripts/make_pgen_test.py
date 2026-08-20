#!/usr/bin/env python3
"""
make_pgen_test.py -- build one NMAP2 PGEN XML file from the GeoJSON the
pipeline has already generated, so there is something real to load into the
dev server without waiting on a live cycle.

Reads output/{ifr,mtn_obsc}_f{00,03,06,09,12}.geojson for a single hazard and
emits every polygon as a <Gfa> element via pipeline/pgen_xml.py.

Example:
    python scripts/make_pgen_test.py --hazard IFR --cycle-hour 21 --out 21Z_IFR.xml

NOTE ON TAGS: every polygon gets its own unique tag by default, with no
reuse across forecast hours (assign_unique_tags). The forecaster renumbers in
NMAP2 anyway, so guessing at AWC's convention buys nothing. The older
tag-reusing assigner is still here behind --naive-tags; see assign_naive_tags.

NOTE ON SIMPLIFICATION: rings are thinned to a vertex budget on the way out
(see simplify_to_budget) ONLY on the v1 vector path. Label-grid output
(pipeline.hazards.ifr.polygonize_ifr_grid_v2) is written through untouched,
because per-ring Douglas-Peucker pulls apart the shared boundaries that
polygonizer exists to produce -- see rings_are_disjoint_by_construction. Either
way this is an export-path concern only: it does not touch polygonization or
the GeoJSON on disk, which keep their full detail.
"""
import argparse
import json
import math
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from pipeline.pgen_xml import assert_rings_disjoint, gfa_element, build_product_xml

# The five snapshots the pipeline writes per cycle.
FORECAST_HOURS = (0, 3, 6, 9, 12)

OUTPUT_DIR = REPO_ROOT / "output"

# --hazard values -> (GeoJSON filename stem, hazard string NMAP2 expects).
# The GeoJSON says "MTN_OBSC" but the reference samples say "MT_OBSC"; the
# XML has to use the latter, which is also what pgen_xml's color table keys on.
HAZARDS = {
    "IFR":     {"stem": "ifr",      "geojson_hazard": "IFR"},
    "MT_OBSC": {"stem": "mtn_obsc", "geojson_hazard": "MTN_OBSC"},
}

# Centroid separation under which a polygon inherits the previous hour's tag.
TAG_MATCH_RADIUS_NM = 300.0

# Ring simplification. Tolerances are in degrees, which is crude near the
# poles but fine over CONUS and keeps this dependency-free.
SIMPLIFY_START_TOLERANCE = 0.005
SIMPLIFY_GROWTH = 1.5
SIMPLIFY_MAX_PASSES = 60
MIN_RING_POINTS = 4

_EARTH_RADIUS_NM = 3440.065


def _distance_nm(a, b):
    """Great-circle distance in nautical miles between two (lat, lon) pairs."""
    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    h = (math.sin((lat2 - lat1) / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2)
    return 2 * _EARTH_RADIUS_NM * math.asin(math.sqrt(h))


def _centroid(points):
    """Unweighted mean of the ring vertices -- same convention pgen_xml uses
    for its default latText/lonText label anchor."""
    return (sum(p[0] for p in points) / len(points),
            sum(p[1] for p in points) / len(points))


def _segment_distance(point, start, end):
    """Distance from `point` to the segment start-end, in degrees."""
    (py, px), (ay, ax), (by, bx) = point, start, end
    dy, dx = by - ay, bx - ax
    if dy == 0.0 and dx == 0.0:
        return math.hypot(py - ay, px - ax)
    # Projection of the point onto the segment, clamped to the segment.
    t = ((py - ay) * dy + (px - ax) * dx) / (dy * dy + dx * dx)
    t = max(0.0, min(1.0, t))
    return math.hypot(py - (ay + t * dy), px - (ax + t * dx))


def _douglas_peucker(points, tolerance):
    """
    Ramer-Douglas-Peucker decimation of an open polyline.

    Iterative rather than recursive -- our rings run to a few hundred
    vertices and recursion depth is not worth risking. The first and last
    vertices are always kept, so applying this to a ring whose duplicate
    closing vertex has already been dropped leaves the closure intact.
    """
    if len(points) < 3:
        return list(points)

    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]

    while stack:
        first, last = stack.pop()
        if last <= first + 1:
            continue
        worst_dist, worst_i = tolerance, None
        for i in range(first + 1, last):
            dist = _segment_distance(points[i], points[first], points[last])
            if dist > worst_dist:
                worst_dist, worst_i = dist, i
        if worst_i is not None:
            keep[worst_i] = True
            stack.append((first, worst_i))
            stack.append((worst_i, last))

    return [p for p, kept in zip(points, keep) if kept]


def simplify_to_budget(ring, max_points=25):
    """
    Thin a ring down to at most `max_points` vertices for the PGEN export.

    Runs Douglas-Peucker repeatedly, starting at SIMPLIFY_START_TOLERANCE and
    growing the tolerance geometrically by SIMPLIFY_GROWTH until the ring fits
    the budget, capped at SIMPLIFY_MAX_PASSES so it cannot spin forever.

    Never returns fewer than MIN_RING_POINTS vertices. Once a tolerance
    collapses the ring past that floor we stop and keep the last result that
    was still above it, which means a ring can come back over budget rather
    than degenerate -- the floor wins. In practice DP steps from "comfortably
    under budget" to "collapsed" in one pass, so this is rare.

    Rings already within budget are returned unchanged.
    """
    if len(ring) <= max_points:
        return list(ring)

    simplified = list(ring)
    tolerance = SIMPLIFY_START_TOLERANCE

    for _ in range(SIMPLIFY_MAX_PASSES):
        candidate = _douglas_peucker(ring, tolerance)
        if len(candidate) < MIN_RING_POINTS:
            break               # overshot; keep the previous, larger result
        simplified = candidate
        if len(simplified) <= max_points:
            break
        tolerance *= SIMPLIFY_GROWTH

    return simplified



def rings_are_disjoint_by_construction(hazard):
    """
    True when this hazard's polygons come from the label-grid
    polygonizer (pipeline.hazards.ifr.polygonize_ifr_grid_v2), which
    partitions the raster and contours each region once, so adjacent
    areas share their boundary exactly and no two areas overlap.

    Two consequences, and they travel together on purpose:

      - NO VERTEX BUDGET. Douglas-Peucker applied per ring moves each
        polygon's copy of a shared edge independently, so two areas that
        traced the same boundary come apart into a gap or an overlap in
        the XML the vendor actually receives. There is no PGEN or NMAP2
        vertex limit to justify paying that -- the 25 was invented, and
        vertex count on this path is governed upstream by
        pipeline.hazards.ifr.CONTOUR_RESOLUTION_DEG instead.
      - THE DISJOINTNESS CHECK RUNS. See
        pipeline.pgen_xml.assert_rings_disjoint().

    Everything else -- MT_OBSC, and IFR with USE_LABEL_GRID_POLYGONIZE
    flipped back to the v1 vector path -- keeps the budget and skips the
    check. Not an oversight: v1 emits overlapping and nested polygons by
    construction, so checking it would turn the one-line revert into a
    hard failure, and its rings are already simplified to ~20 vertices
    with no shared-boundary guarantee left to protect.

    Imported inside the function so this CLI keeps working without the
    numpy/scipy/skimage stack when it is only reading GeoJSON.
    """
    if hazard != "IFR":
        return False
    from pipeline.hazards.ifr import USE_LABEL_GRID_POLYGONIZE

    return USE_LABEL_GRID_POLYGONIZE


def vertex_budget_for(hazard, max_points):
    """The per-ring vertex budget to apply, or None to leave rings alone."""
    return None if rings_are_disjoint_by_construction(hazard) else max_points


def apply_vertex_budget(ring, budget):
    """simplify_to_budget() when there is a budget, identity when there isn't."""
    return list(ring) if budget is None else simplify_to_budget(ring, budget)


def assign_unique_tags(centroids_by_hour):
    """
    DEFAULT tag assignment: one unique integer per polygon, numbered
    sequentially across the whole cycle and never reused between forecast
    hours.

    These are first-guess polygons that the forecaster renumbers in NMAP2, so
    matching AWC's tag convention is not a goal. Reuse is not merely
    unnecessary here, it is harmful: a tag shared across hours tells NMAP2 the
    polygons are one evolving feature, which is what made the naive assigner
    render heavily overlapping smears. Unique tags cannot express that
    relationship, so they cannot get it wrong.

    Accepts centroids_by_hour purely so it is drop-in interchangeable with
    assign_naive_tags. The geometry is not used -- only the per-hour counts.

    centroids_by_hour : list, one entry per forecast hour in order, each a
                        list of (lat, lon) polygon centroids.
    returns           : list of the same shape, holding integer tags.
    """
    tags_by_hour = []
    next_tag = 1

    for centroids in centroids_by_hour:
        tags_by_hour.append(list(range(next_tag, next_tag + len(centroids))))
        next_tag += len(centroids)

    return tags_by_hour


def assign_naive_tags(centroids_by_hour):
    """
    OPT-IN tag assignment, unused by default -- enable with --naive-tags.

    Superseded by assign_unique_tags(), which is now the default. Tag reuse
    turned out to be the wrong thing to approximate: the forecaster renumbers
    these first-guess polygons in NMAP2, so matching AWC's convention was
    never a goal, and reusing a tag across hours made NMAP2 draw the polygons
    as one evolving feature -- 15 overlapping tag pairs in IFR's F00/F03/F06
    window alone. Unique tags avoid that by construction. This is kept only
    in case tag grouping later becomes a real requirement.

    Deliberately naive. Walks the forecast hours in order. For each polygon, if its centroid lies
    within TAG_MATCH_RADIUS_NM of a polygon at the PREVIOUS hour, it reuses
    that polygon's tag; otherwise it takes the next unused integer.

    This is NOT feature tracking. Specifically, it does NOT:
      - test overlap, area, or shape similarity (centroid proximity only),
      - do any geographic/named-area lookup,
      - handle splits or merges,
      - resolve contention -- the first previous-hour polygon within range
        wins by input order, and one predecessor may be claimed by several
        successors, so two polygons at the same hour can share a tag,
      - look back further than one hour, so a feature that drops out for a
        single snapshot comes back with a brand-new tag.

    The resulting tags are plausible, not correct. They exist so the NMAP2
    file carries stable-looking identifiers for the forecaster to redraw
    against. Swap this out when real tracking lands; nothing else in this
    script depends on how the tags are chosen.

    centroids_by_hour : list, one entry per forecast hour in order, each a
                        list of (lat, lon) polygon centroids.
    returns           : list of the same shape, holding integer tags.
    """
    tags_by_hour = []
    previous = []          # [(centroid, tag)] carried from the prior hour
    next_tag = 1

    for centroids in centroids_by_hour:
        tags = []
        current = []
        for centroid in centroids:
            tag = None
            for prev_centroid, prev_tag in previous:
                if _distance_nm(centroid, prev_centroid) <= TAG_MATCH_RADIUS_NM:
                    tag = prev_tag         # first match wins -- see docstring
                    break
            if tag is None:
                tag = next_tag
                next_tag += 1
            tags.append(tag)
            current.append((centroid, tag))
        tags_by_hour.append(tags)
        previous = current

    return tags_by_hour


def weather_type_string(hazard, properties):
    """
    Compose the Gfa `type` string the way the reference samples spell it.

    The pipeline's `weather_type` property holds only the trailing cause list
    ("PCPN/BR", "CLDS", ...); the samples prefix it with the hazard's standing
    threshold phrase:

        IFR       "CIG BLW 010/VIS BLW 3SM " + weather_type
        MT_OBSC   "MTNS OBSC BY "            + weather_type

    IFR polygons whose `cause` is "CIG" alone carry no `weather_type` at all
    (visibility never crossed its threshold), so they get the ceiling clause
    on its own -- the samples have no example of this case.
    """
    wx = properties.get("weather_type")

    if hazard == "MT_OBSC":
        return "MTNS OBSC BY %s" % (wx or "CLDS")

    if wx:
        return "CIG BLW 010/VIS BLW 3SM %s" % wx
    return "CIG BLW 010"


def iter_rings(feature):
    """
    Yield each drawable exterior ring of a feature as [(lat, lon), ...].

    A MultiPolygon becomes one ring per part, all sharing the parent
    feature's properties -- the same split pipeline/export_xml.py makes.
    Interior rings (holes) are dropped: a PGEN Gfa is a single closed
    outline with no way to express one.
    """
    geometry = feature["geometry"]
    if geometry["type"] == "Polygon":
        parts = [geometry["coordinates"]]
    elif geometry["type"] == "MultiPolygon":
        parts = geometry["coordinates"]
    else:
        raise ValueError("unsupported geometry type %r" % geometry["type"])

    for part in parts:
        ring = part[0]                     # exterior ring; part[1:] are holes
        # GeoJSON closes a ring by repeating the first vertex, PGEN does not.
        if len(ring) > 1 and ring[0] == ring[-1]:
            ring = ring[:-1]
        # GeoJSON stores [lon, lat]; pgen_xml wants (lat, lon).
        yield [(lat, lon) for lon, lat in ring]


def load_hazard_polygons(hazard, max_points):
    """
    Read all five snapshots for one hazard, simplifying each ring on the way.

    Returns a list, one entry per forecast hour in FORECAST_HOURS order, of
    lists of (forecast_hour, points, properties, raw_point_count) tuples,
    where raw_point_count is the vertex count before simplification.
    """
    stem = HAZARDS[hazard]["stem"]
    budget = vertex_budget_for(hazard, max_points)
    by_hour = []

    for fcst_hr in FORECAST_HOURS:
        path = OUTPUT_DIR / ("%s_f%02d.geojson" % (stem, fcst_hr))
        if not path.exists():
            raise SystemExit("missing input: %s" % path)

        with open(path, encoding="utf-8") as fh:
            collection = json.load(fh)

        polygons = []
        for feature in collection.get("features", []):
            for points in iter_rings(feature):
                if len(points) < 3:
                    continue           # pgen_xml rejects degenerate outlines
                raw_count = len(points)
                points = apply_vertex_budget(points, budget)
                polygons.append(
                    (fcst_hr, points, feature["properties"], raw_count))

        # Checked per forecast hour, on the rings as they will be
        # serialized -- see pipeline.pgen_xml.assert_rings_disjoint.
        if rings_are_disjoint_by_construction(hazard):
            assert_rings_disjoint([pts for _, pts, _, _ in polygons], hazard, fcst_hr)

        by_hour.append(polygons)

    return by_hour


def _describe(counts, max_points):
    """One-line summary of a vertex-count distribution."""
    if not counts:
        return "none"
    ordered = sorted(counts)
    median = ordered[len(ordered) // 2]
    over = sum(1 for c in counts if c > max_points)
    return ("min=%-4d median=%-4d max=%-4d total=%-6d over budget=%d/%d"
            % (ordered[0], median, ordered[-1], sum(counts),
               over, len(counts)))


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Build an NMAP2 PGEN XML file from generated GeoJSON.")
    parser.add_argument("--hazard", required=True, choices=sorted(HAZARDS),
                        help="hazard to export")
    parser.add_argument("--cycle-hour", required=True,
                        help="G-AIRMET cycle hour, e.g. 21")
    parser.add_argument("--desk", default="E", help="desk letter (default: E)")
    parser.add_argument("--out", required=True, help="output XML path")
    parser.add_argument("--max-points", type=int, default=25,
                        help="vertex budget per ring on the v1 path (default: "
                             "25); ignored for label-grid output, which is "
                             "never simplified")
    parser.add_argument("--naive-tags", action="store_true",
                        help="reuse tags across forecast hours via "
                             "assign_naive_tags (default: unique tags)")
    args = parser.parse_args(argv)

    # Cycles are 03/09/15/21Z; zero-pad so "9" and "09" produce the same file.
    cycle_hour = args.cycle_hour.strip()
    if cycle_hour.isdigit():
        cycle_hour = "%02d" % int(cycle_hour)

    by_hour = load_hazard_polygons(args.hazard, args.max_points)

    assign_tags = assign_naive_tags if args.naive_tags else assign_unique_tags
    tags_by_hour = assign_tags(
        [[_centroid(points) for _, points, _, _ in hour] for hour in by_hour])

    blocks = []
    for hour, tags in zip(by_hour, tags_by_hour):
        for (fcst_hr, points, properties, _raw), tag in zip(hour, tags):
            blocks.append(gfa_element(
                points=points,
                hazard=args.hazard,
                fcst_hr=fcst_hr,
                tag=tag,
                wx_type=weather_type_string(args.hazard, properties),
                cycle_hour=cycle_hour,
                desk=args.desk,
            ))

    out_path = Path(args.out)
    if out_path.parent != Path(""):
        out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(build_product_xml(blocks))

    print("wrote %s -- %d Gfa elements across %d forecast hours"
          % (out_path, len(blocks), len(FORECAST_HOURS)))
    for fcst_hr, hour, tags in zip(FORECAST_HOURS, by_hour, tags_by_hour):
        print("  f%02d  %2d polygons  tags=%s"
              % (fcst_hr, len(hour), ",".join(str(t) for t in tags) or "-"))

    before = [raw for hour in by_hour for _, _, _, raw in hour]
    after = [len(points) for hour in by_hour for _, points, _, _ in hour]
    budget = vertex_budget_for(args.hazard, args.max_points)
    if budget is None:
        # Reporting "over budget" against a budget that was deliberately
        # not applied reads as a warning about nothing.
        print("  vertices         %s" % _describe(after, max(after, default=0)))
        print("  (label-grid output: no vertex budget applied, rings written through whole)")
    else:
        print("  vertices before  %s" % _describe(before, budget))
        print("  vertices after   %s" % _describe(after, budget))


if __name__ == "__main__":
    main()
