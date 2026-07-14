"""
pipeline/export_xml.py
------------------------
Converts our GeoJSON hazard polygon output into a simple, generic XML
representation -- NOT the official NWS USWX/GML standard (that's a much
heavier lift involving GML geometry encoding, OM_Observation wrapping,
and AIXM aviation-specific typing), and deliberately so: the goal here
is a "first guess" G-AIRMET draft that gets injected into N-AWIPS as a
starting point for a forecaster to refine, not a publicly-disseminated,
standards-compliant final product. N-AWIPS' own existing tooling
handles that final VGF -> USWX conversion downstream; this module's
job is just getting our polygons into SOME xml form simple enough for
that intermediate XML -> VGF conversion step to consume.

Uses only Python's standard library (xml.etree.ElementTree) --
deliberately no new dependency, since this is a small, self-contained
serialization step bolted onto an already-working pipeline.

Design, kept intentionally plain:
  - One <Polygon> element per hazard polygon, with a simple integer id.
  - Each polygon's outer boundary is one <Exterior>; each hole (if any)
    is its own <Interior> -- both hold coordinates as a single
    space-separated "lon,lat lon,lat ..." string, ring closed (first
    point repeated at the end), matching GeoJSON's own convention.
  - Shared metadata (hazard type, valid time, model cycle, the three
    forecaster-adjustable parameters used) lives as attributes on the
    root element, since currently every feature in one of our
    FeatureCollections shares identical properties.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from xml.dom import minidom

COORD_DECIMAL_PLACES = 6  # ~11cm precision at the equator -- plenty for hazard polygon boundaries


def _format_ring(coords: list) -> str:
    """Formats a ring's coordinates as a space-separated 'lon,lat lon,lat ...' string.

    Explicitly casts to float before rounding: plain Python round()
    preserves int type when given an int (round(-99, 6) == -99, not
    -99.0), which would format inconsistently ("-99" vs "-99.0")
    depending on whether input coordinates happened to be int or float.
    Real pipeline data is always genuine floats, but formatting
    shouldn't depend on that -- caught by a test that used integer
    literals for convenience.
    """
    return " ".join(f"{round(float(lon), COORD_DECIMAL_PLACES)},{round(float(lat), COORD_DECIMAL_PLACES)}" for lon, lat in coords)


def _add_polygon_element(parent: ET.Element, polygon_id: int, geometry: dict, cause: str | None = None) -> None:
    """
    Adds one <Polygon> element for a single GeoJSON Polygon geometry
    (exterior ring + any interior/hole rings). cause ("CIG", "VIS", or
    "CIG/VIS" -- see pipeline.hazards.ifr._determine_cause) is a
    PER-POLYGON attribute, unlike the shared root-level ones, since
    different polygons in the same output can have different causes.
    """
    attrs = {"id": str(polygon_id)}
    if cause:
        attrs["cause"] = cause
    poly_el = ET.SubElement(parent, "Polygon", attrs)
    rings = geometry["coordinates"]
    if not rings:
        return
    ET.SubElement(poly_el, "Exterior").text = _format_ring(rings[0])
    for hole_ring in rings[1:]:
        ET.SubElement(poly_el, "Interior").text = _format_ring(hole_ring)


def geojson_to_xml(feature_collection: dict) -> str:
    """
    Converts a GeoJSON FeatureCollection (as produced by
    pipeline.polygons.polygons_to_feature_collection) into a simple XML
    string.

    Handles both "Polygon" and "MultiPolygon" geometry types. This
    isn't just defensive/theoretical: confirmed against real pipeline
    output that MultiPolygon results genuinely occur today (most likely
    smooth_polygon_boundary()'s buffer-based opening step occasionally
    pinching a thin-necked shape into two separate pieces, even though
    merge_nearby_polygons() earlier in the pipeline already flattens
    ITS OWN MultiPolygon results back into simple Polygons).

    Root-level attributes come from the FIRST feature's SHARED
    properties (things like hazard/threshold/valid_time are currently
    identical across every feature in one of our FeatureCollections).
    PER-POLYGON properties -- currently just "cause" ("CIG", "VIS", or
    "CIG/VIS", see pipeline.hazards.ifr._determine_cause) -- are read
    from each feature individually instead, since different polygons in
    the same output can have different causes. Either way, any expected
    attribute that's simply missing (e.g. older data generated before a
    field existed) is silently omitted rather than written as a literal
    "None".

    Returns a pretty-printed XML string (UTF-8, with declaration).
    """
    features = feature_collection.get("features", [])
    props = features[0]["properties"] if features else {}

    root_attrs = {}
    for key, xml_name in [
        ("hazard", "hazard"),
        ("model_cycle", "modelCycle"),
        ("nbm_source_cycle", "nbmSourceCycle"),
        ("valid_time", "validTime"),
        ("forecast_hour", "forecastHour"),
        ("threshold_pct", "thresholdPct"),
        ("neighborhood_radius_nm", "neighborhoodRadiusNm"),
        ("min_area_sq_mi", "minAreaSqMi"),
    ]:
        if key in props and props[key] is not None:
            root_attrs[xml_name] = str(props[key])

    root = ET.Element("GAirmetPolygons", root_attrs)

    polygon_id = 1
    for feature in features:
        geometry = feature["geometry"]
        cause = feature.get("properties", {}).get("cause")
        if geometry["type"] == "Polygon":
            _add_polygon_element(root, polygon_id, geometry, cause=cause)
            polygon_id += 1
        elif geometry["type"] == "MultiPolygon":
            # A MultiPolygon's parts all share the ONE cause computed
            # for the whole feature (cause attribution runs on the
            # final, already-possibly-split shape -- see
            # pipeline.hazards.ifr.polygonize_ifr_grid) -- reasonable,
            # since parts of a single MultiPolygon typically arose from
            # one contiguous hazard area getting pinched into pieces by
            # boundary smoothing, not from genuinely different causes.
            for part_coords in geometry["coordinates"]:
                _add_polygon_element(root, polygon_id, {"coordinates": part_coords}, cause=cause)
                polygon_id += 1

    raw = ET.tostring(root, encoding="unicode")
    pretty = minidom.parseString(raw).toprettyxml(indent="  ")
    # minidom's toprettyxml adds its own XML declaration (with an
    # unwanted standalone newline quirk) -- normalize to a single clean
    # UTF-8 declaration line.
    lines = [line for line in pretty.split("\n") if line.strip()]
    lines[0] = '<?xml version="1.0" encoding="UTF-8"?>'
    return "\n".join(lines) + "\n"
