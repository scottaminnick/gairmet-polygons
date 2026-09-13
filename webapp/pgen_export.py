"""
webapp/pgen_export.py
---------------------
Assembles ONE NMAP2 PGEN XML document covering all five forecast hours,
each polygonized at that hour's own forecaster-chosen settings.

Why this exists separately from the per-hour export_xml.py download:
NMAP2's Filter Control steps through forecast hours WITHIN a single file,
so a forecaster needs F00-F12 together in one document. Splitting them
into five files breaks that workflow. The existing per-hour
export_xml.py route is untouched and still does its own thing.

Nothing here re-implements the PGEN format or the GeoJSON->Gfa mapping.
The writer is pipeline/pgen_xml.py (proven byte-identical against real
NMAP2 exports -- see tests/test_pgen_roundtrip.py), and the element
mapping is imported wholesale from scripts/make_pgen_test.py, which is
the working CLI reference for exactly this conversion. Keeping one
implementation means the CLI and the web app cannot drift apart.

PROVENANCE: deliberately NO extra attributes are written onto the Gfa
elements to record which threshold produced them. Their parser accepted
a specific attribute set and perturbing it is not worth the risk. The
per-hour settings travel alongside, in the sidecar JSON that
build_pgen_document() returns as its second value and that the frontend
downloads next to the XML.
"""

from pipeline.pgen_xml import assert_rings_disjoint, build_product_xml, gfa_element
from pipeline.tag_tracking import assign_tracked_tags

# The element mapping, reused rather than re-implemented.
from scripts.make_pgen_test import (
    FORECAST_HOURS,
    apply_vertex_budget,
    iter_rings,
    rings_are_disjoint_by_construction,
    vertex_budget_for,
    weather_type_string,
)

# Vertex budget per ring on the v1 vector path. Real hand-drawn G-AIRMETs
# run 6-26 points per element, and v1's rings are already simplified, so
# thinning them further costs nothing it hasn't already lost.
#
# NOT applied to label-grid output. Douglas-Peucker run per ring separates
# the boundaries adjacent label-grid regions share, which is the one thing
# that polygonizer exists to guarantee. See
# rings_are_disjoint_by_construction() for the full reasoning. Vertex
# count on that path is governed upstream instead, by
# pipeline.hazards.ifr.CONTOUR_RESOLUTION_DEG and the shared-arc
# simplification behind ARC_SIMPLIFY_TOLERANCE_DEG, which thins rings
# WITHOUT unsharing their edges -- the NMAP2 VG converter did turn out to
# choke on several-hundred-vertex rings.
PGEN_MAX_POINTS = 25


def build_pgen_document(hazard, hours, cycle_hour, desk="E",
                        max_points=PGEN_MAX_POINTS, model_cycle=None):
    """
    Build the combined PGEN document and its provenance sidecar.

    hazard      : "IFR" or "MT_OBSC" -- the string NMAP2 expects, which for
                  mountain obscuration is NOT the "MTN_OBSC" the GeoJSON
                  carries. Also decides whether rings are thinned and
                  whether disjointness is enforced; see
                  rings_are_disjoint_by_construction().
    hours       : list of dicts, one per forecast hour, ascending:
                    {"forecast_hour": int,
                     "settings": {...},              # echoed into the sidecar
                     "feature_collection": {...}}    # GeoJSON from recompute
    cycle_hour  : "21" etc.
    model_cycle : the manifest's model_cycle, recorded in the sidecar.

    Returns (xml_text, sidecar_dict).
    """
    # Flatten each hour to its drawable rings. A MultiPolygon contributes
    # one ring per part, sharing the parent feature's properties --
    # iter_rings handles that.
    budget = vertex_budget_for(hazard, max_points)
    verify_disjoint = rings_are_disjoint_by_construction(hazard)

    rings_by_hour = []
    for entry in hours:
        rings = []
        for feature in entry["feature_collection"].get("features", []):
            for points in iter_rings(feature):
                if len(points) < 3:
                    continue            # pgen_xml rejects degenerate outlines
                rings.append((apply_vertex_budget(points, budget), feature["properties"]))

        # On the rings as they will be serialized, per forecast hour --
        # after any thinning, not before it, because thinning is one of
        # the things that has broken this invariant before. Raises.
        if verify_disjoint:
            assert_rings_disjoint(
                [points for points, _properties in rings], hazard, entry["forecast_hour"]
            )

        rings_by_hour.append(rings)

    # Feature-tracked tags: a polygon inherits the tag of a previous-hour
    # polygon it intersects, so the five snapshots read as one evolving
    # hazard rather than five unrelated sets. That relationship is what
    # NMAP2 draws through time and what the BUFR smear is built from --
    # see pipeline/tag_tracking.py for the five rules and their source.
    tags_by_hour = assign_tracked_tags(
        [[points for points, _ in hour] for hour in rings_by_hour]
    )

    blocks = []
    hour_summaries = []
    for entry, rings, tags in zip(hours, rings_by_hour, tags_by_hour):
        for (points, properties), tag in zip(rings, tags):
            blocks.append(gfa_element(
                points=points,
                hazard=hazard,
                fcst_hr=entry["forecast_hour"],
                tag=tag,
                wx_type=weather_type_string(hazard, properties),
                cycle_hour=cycle_hour,
                desk=desk,
            ))
        hour_summaries.append({
            "forecast_hour": entry["forecast_hour"],
            "settings": entry["settings"],
            "gfa_elements": len(rings),
            # The distinct tags present this hour. Was [first, last] --
            # which only ever meant anything while tags were unique and
            # sequential. With tracking a tag can repeat within an hour
            # (a split) and recur across hours (a continuation), so the
            # endpoints of the list describe nothing.
            "tags": sorted(set(tags)),
        })

    sidecar = {
        "hazard": hazard,
        "cycle_hour": cycle_hour,
        "desk": desk,
        "model_cycle": model_cycle,
        "max_points_per_ring": budget,
        "rings_verified_disjoint": verify_disjoint,
        "tagging": "feature-tracked: intersection with previous hour, lowest tag wins",
        "total_gfa_elements": len(blocks),
        "forecast_hours": hour_summaries,
        "note": (
            "Settings that produced the accompanying PGEN XML. Recorded here "
            "rather than as Gfa attributes so the XML's attribute set stays "
            "exactly what NMAP2's parser was verified against."
        ),
    }

    return build_product_xml(blocks), sidecar
