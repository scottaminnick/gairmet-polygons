"""
Tests for the combined PGEN export (webapp/pgen_export.py) -- the one
document per hazard holding all five forecast hours.

Deliberately built from synthetic FeatureCollections rather than real
pipeline output: this exercises the GeoJSON -> Gfa mapping, tagging and
vertex budgeting, none of which need numpy, shapely or a fetched data
branch. That keeps it runnable in the light CI environment.
"""
import math
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from webapp.pgen_export import PGEN_MAX_POINTS, build_pgen_document

FORECAST_HOURS = (0, 3, 6, 9, 12)
_FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _ring(n, lat0=40.0, lon0=-100.0, radius=2.0):
    """A closed n-vertex ring in GeoJSON [lon, lat] order."""
    pts = [
        [lon0 + radius * math.cos(i * 2 * math.pi / n),
         lat0 + radius * math.sin(i * 2 * math.pi / n)]
        for i in range(n)
    ]
    return pts + [pts[0]]           # GeoJSON closes rings; PGEN must not


def _fc(n_features, vertices=40, hazard="IFR", weather_type="PCPN/BR"):
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {"type": "Polygon",
                             "coordinates": [_ring(vertices, lat0=35.0 + 3 * i)]},
                "properties": {"hazard": hazard, "weather_type": weather_type,
                               "cause": "CIG/VIS"},
            }
            for i in range(n_features)
        ],
    }


def _hours(counts, hazard="IFR", vertices=40):
    return [
        {"forecast_hour": fxx,
         "settings": {"threshold_pct": 40 + i * 5, "neighborhood_radius_nm": 50,
                      "min_area_sq_mi": 3000},
         "feature_collection": _fc(count, vertices=vertices, hazard=hazard)}
        for i, (fxx, count) in enumerate(zip(FORECAST_HOURS, counts))
    ]


def _gfas(xml):
    root = ET.fromstring(xml)
    return root.find("Product").find("Layer").find("DrawableElement").findall("Gfa")


def test_all_forecast_hours_land_in_one_document():
    """Every hour's polygons share one file -- NMAP2's Filter Control needs that."""
    xml, _ = build_pgen_document("IFR", _hours([2, 3, 1, 4, 2]), "21")
    gfas = _gfas(xml)
    assert len(gfas) == 12
    assert sorted({int(g.get("fcstHr")) for g in gfas}) == list(FORECAST_HOURS)


def test_tags_are_unique_and_sequential_across_the_whole_file():
    """Not restarting per hour, and never reused between hours."""
    xml, _ = build_pgen_document("IFR", _hours([2, 3, 1, 4, 2]), "21")
    tags = [int(g.get("tag")) for g in _gfas(xml)]
    assert tags == list(range(1, len(tags) + 1))


def test_rings_are_thinned_to_the_vertex_budget():
    xml, _ = build_pgen_document("IFR", _hours([1, 1, 1, 1, 1], vertices=400), "21")
    counts = [len(g.findall("Point")) for g in _gfas(xml)]
    assert counts, "expected at least one element"
    assert max(counts) <= PGEN_MAX_POINTS


def test_geojson_closing_vertex_is_dropped():
    """GeoJSON repeats the first vertex to close a ring; PGEN does not."""
    xml, _ = build_pgen_document("IFR", _hours([1, 0, 0, 0, 0], vertices=10), "21")
    for gfa in _gfas(xml):
        pts = [(p.get("Lat"), p.get("Lon")) for p in gfa.findall("Point")]
        assert pts[0] != pts[-1]


def test_gfa_attribute_set_matches_the_reference_sample_exactly():
    """
    The whole point of the sidecar: provenance must NOT be smuggled into the
    XML. Their parser was verified against a specific attribute set, so a
    generated Gfa must carry exactly the same attribute NAMES as a real
    NMAP2 export -- no extras recording which threshold produced it.
    """
    reference = ET.parse(_FIXTURES / "21Z_CV.xml").getroot()
    expected = set(
        reference.find("Product").find("Layer")
        .find("DrawableElement").findall("Gfa")[0].attrib
    )
    xml, _ = build_pgen_document("IFR", _hours([1, 0, 0, 0, 0]), "21")
    for gfa in _gfas(xml):
        assert set(gfa.attrib) == expected


def test_mtn_obsc_uses_the_hazard_string_nmap2_expects():
    """The GeoJSON says MTN_OBSC; the samples -- and pgen_xml -- say MT_OBSC."""
    hours = _hours([1, 0, 0, 0, 0], hazard="MTN_OBSC")
    xml, sidecar = build_pgen_document("MT_OBSC", hours, "21")
    assert {g.get("hazard") for g in _gfas(xml)} == {"MT_OBSC"}
    assert sidecar["hazard"] == "MT_OBSC"
    assert all(g.get("type").startswith("MTNS OBSC BY") for g in _gfas(xml))


def test_sidecar_records_each_hours_settings_and_counts():
    counts = [2, 3, 1, 4, 2]
    xml, sidecar = build_pgen_document("IFR", _hours(counts), "21", desk="W",
                                       model_cycle="2026-08-17T21:00:00Z")
    assert sidecar["desk"] == "W"
    assert sidecar["cycle_hour"] == "21"
    assert sidecar["model_cycle"] == "2026-08-17T21:00:00Z"
    assert sidecar["max_points_per_ring"] == PGEN_MAX_POINTS
    assert sidecar["total_gfa_elements"] == len(_gfas(xml)) == sum(counts)

    assert [h["forecast_hour"] for h in sidecar["forecast_hours"]] == list(FORECAST_HOURS)
    assert [h["gfa_elements"] for h in sidecar["forecast_hours"]] == counts
    # each hour's own threshold is recorded, not one shared value
    assert [h["settings"]["threshold_pct"] for h in sidecar["forecast_hours"]] == [40, 45, 50, 55, 60]


def test_multipolygon_parts_each_become_their_own_element():
    """Matches what pipeline/export_xml.py already does with MultiPolygons."""
    hours = _hours([0, 0, 0, 0, 0])
    hours[0]["feature_collection"] = {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "geometry": {"type": "MultiPolygon",
                         "coordinates": [[_ring(8, lat0=35.0)], [_ring(8, lat0=45.0)]]},
            "properties": {"hazard": "IFR", "weather_type": "BR", "cause": "CIG/VIS"},
        }],
    }
    xml, _ = build_pgen_document("IFR", hours, "21")
    assert len(_gfas(xml)) == 2


def test_empty_cycle_produces_a_valid_but_empty_document():
    """A quiet cycle must still yield a loadable file, not a crash."""
    xml, sidecar = build_pgen_document("IFR", _hours([0, 0, 0, 0, 0]), "21")
    assert _gfas(xml) == []
    assert sidecar["total_gfa_elements"] == 0
    assert ET.fromstring(xml) is not None


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
