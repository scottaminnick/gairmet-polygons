"""
tests/test_export_xml.py
--------------------------
Tests pipeline/export_xml.py's simple GeoJSON -> XML conversion.
"""

import sys
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.export_xml import geojson_to_xml


def _make_fc(features):
    return {"type": "FeatureCollection", "features": features}


def _polygon_feature(coords, properties=None):
    return {
        "type": "Feature",
        "properties": properties or {"hazard": "IFR", "valid_time": "2026-07-13T15:00:00Z"},
        "geometry": {"type": "Polygon", "coordinates": coords},
    }


def test_simple_polygon_produces_well_formed_xml_with_correct_attributes():
    fc = _make_fc([_polygon_feature(
        [[[-104.5, 39.2], [-104.3, 39.5], [-104.1, 39.6], [-104.5, 39.2]]],
        properties={
            "hazard": "IFR", "threshold_pct": 50.0, "neighborhood_radius_nm": 50.0,
            "min_area_sq_mi": 3000.0, "valid_time": "2026-07-13T15:00:00Z",
            "model_cycle": "2026-07-13T15:00:00Z", "nbm_source_cycle": "2026-07-13T09:00:00Z",
            "forecast_hour": 0,
        },
    )])

    xml_str = geojson_to_xml(fc)
    root = ET.fromstring(xml_str)  # raises if not well-formed

    assert root.tag == "GAirmetPolygons"
    assert root.attrib["hazard"] == "IFR"
    assert root.attrib["modelCycle"] == "2026-07-13T15:00:00Z"
    assert root.attrib["nbmSourceCycle"] == "2026-07-13T09:00:00Z"

    polygons = root.findall("Polygon")
    assert len(polygons) == 1
    assert polygons[0].attrib["id"] == "1"
    assert polygons[0].find("Exterior") is not None
    assert polygons[0].find("Interior") is None


def test_hole_becomes_a_separate_interior_element():
    """A polygon with one hole should produce one Exterior and exactly one Interior."""
    exterior = [[-100, 40], [-95, 40], [-95, 45], [-100, 45], [-100, 40]]
    hole = [[-99, 41], [-99, 42], [-98, 42], [-98, 41], [-99, 41]]
    fc = _make_fc([_polygon_feature([exterior, hole])])

    xml_str = geojson_to_xml(fc)
    root = ET.fromstring(xml_str)
    poly = root.find("Polygon")

    assert len(poly.findall("Exterior")) == 1
    assert len(poly.findall("Interior")) == 1
    assert poly.find("Interior").text == "-99.0,41.0 -99.0,42.0 -98.0,42.0 -98.0,41.0 -99.0,41.0"


def test_multiple_polygons_get_sequential_ids():
    fc = _make_fc([
        _polygon_feature([[[-100, 40], [-99, 40], [-99, 41], [-100, 40]]]),
        _polygon_feature([[[-90, 35], [-89, 35], [-89, 36], [-90, 35]]]),
        _polygon_feature([[[-80, 30], [-79, 30], [-79, 31], [-80, 30]]]),
    ])
    xml_str = geojson_to_xml(fc)
    root = ET.fromstring(xml_str)
    ids = [p.attrib["id"] for p in root.findall("Polygon")]
    assert ids == ["1", "2", "3"]


def test_missing_properties_are_omitted_not_written_as_none():
    """Older data (e.g. generated before nbm_source_cycle existed) shouldn't produce a literal 'None' attribute."""
    fc = _make_fc([_polygon_feature(
        [[[-100, 40], [-99, 40], [-99, 41], [-100, 40]]],
        properties={"hazard": "IFR", "valid_time": "2026-07-13T15:00:00Z"},
    )])
    xml_str = geojson_to_xml(fc)
    assert "nbmSourceCycle" not in xml_str
    assert "None" not in xml_str
    root = ET.fromstring(xml_str)
    assert "nbmSourceCycle" not in root.attrib


def test_multipolygon_splits_into_separate_polygon_elements():
    """
    Confirmed against real pipeline output (not just a hypothetical):
    smooth_polygon_boundary()'s buffer operations occasionally produce
    a genuine MultiPolygon even after merge_nearby_polygons() already
    flattened its own MultiPolygon results. This constructs one
    directly (rather than relying on buffer operations happening to
    reproduce one) for a deterministic, reliable test.
    """
    multipolygon_feature = {
        "type": "Feature",
        "properties": {"hazard": "IFR", "valid_time": "2026-07-13T15:00:00Z"},
        "geometry": {
            "type": "MultiPolygon",
            "coordinates": [
                [[[-100, 40], [-99, 40], [-99, 41], [-100, 40]]],
                [[[-90, 35], [-89, 35], [-89, 36], [-90, 35]]],
            ],
        },
    }
    fc = _make_fc([multipolygon_feature])
    xml_str = geojson_to_xml(fc)
    root = ET.fromstring(xml_str)
    polygons = root.findall("Polygon")

    assert len(polygons) == 2, "One MultiPolygon feature with 2 parts should produce 2 separate Polygon elements"
    assert [p.attrib["id"] for p in polygons] == ["1", "2"]


def test_empty_feature_collection_produces_valid_xml_with_no_polygons():
    fc = _make_fc([])
    xml_str = geojson_to_xml(fc)
    root = ET.fromstring(xml_str)  # should not raise
    assert root.tag == "GAirmetPolygons"
    assert root.findall("Polygon") == []


if __name__ == "__main__":
    test_simple_polygon_produces_well_formed_xml_with_correct_attributes()
    test_hole_becomes_a_separate_interior_element()
    test_multiple_polygons_get_sequential_ids()
    test_missing_properties_are_omitted_not_written_as_none()
    test_multipolygon_splits_into_separate_polygon_elements()
    test_empty_feature_collection_produces_valid_xml_with_no_polygons()
    print("All manual checks passed.")
