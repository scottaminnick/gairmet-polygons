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

from pipeline.pgen_xml import OverlappingRingsError
from webapp.pgen_export import PGEN_MAX_POINTS, build_pgen_document

FORECAST_HOURS = (0, 3, 6, 9, 12)
_FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _ring(n, lat0=40.0, lon0=-100.0, radius=1.0):
    """
    A closed n-vertex ring in GeoJSON [lon, lat] order.

    The default radius is deliberately smaller than the 3 deg spacing
    _fc() puts between features: IFR rings now go through
    pipeline.pgen_xml.assert_rings_disjoint() on the way out, and a
    fixture whose own features overlap would fail the export before it
    tested anything. (It used to be 2.0, i.e. every fixture overlapped
    its neighbour -- which is exactly what the new check is for.)
    """
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


def test_v1_rings_are_thinned_to_the_vertex_budget():
    """
    MT_OBSC is still on the v1 vector path: its rings are already
    simplified and carry no shared-boundary guarantee, so thinning them
    to the budget costs nothing.
    """
    xml, _ = build_pgen_document("MT_OBSC", _hours([1, 1, 1, 1, 1], vertices=400), "21")
    counts = [len(g.findall("Point")) for g in _gfas(xml)]
    assert counts, "expected at least one element"
    assert max(counts) <= PGEN_MAX_POINTS


def test_label_grid_rings_are_written_through_untouched():
    """
    The v2 path must NOT be simplified. Douglas-Peucker per ring moves
    each polygon's copy of a shared edge independently, so two adjacent
    areas that traced the same boundary come apart into a gap or an
    overlap in the file the vendor receives -- and there is no PGEN or
    NMAP2 vertex limit that would justify paying that. Vertex count on
    this path is governed by CONTOUR_RESOLUTION_DEG upstream instead.
    """
    xml, _ = build_pgen_document("IFR", _hours([1, 1, 1, 1, 1], vertices=400), "21")
    counts = [len(g.findall("Point")) for g in _gfas(xml)]
    assert counts, "expected at least one element"
    assert min(counts) == 400, f"IFR rings were thinned to {min(counts)}; they must pass through whole"


def test_reverting_to_v1_restores_the_budget_for_ifr(monkeypatch):
    """
    USE_LABEL_GRID_POLYGONIZE is the forecaster's one-line revert, and it
    has to take the export path back with it -- v1 output has no shared
    boundaries to protect and every reason to stay thin.
    """
    import pipeline.hazards.ifr as ifr_module

    monkeypatch.setattr(ifr_module, "USE_LABEL_GRID_POLYGONIZE", False)
    xml, sidecar = build_pgen_document("IFR", _hours([1, 1, 1, 1, 1], vertices=400), "21")
    counts = [len(g.findall("Point")) for g in _gfas(xml)]
    assert max(counts) <= PGEN_MAX_POINTS
    assert sidecar["max_points_per_ring"] == PGEN_MAX_POINTS
    assert sidecar["rings_verified_disjoint"] is False


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
    # No budget on the label-grid path -- see PGEN_MAX_POINTS' comment.
    assert sidecar["max_points_per_ring"] is None
    assert sidecar["rings_verified_disjoint"] is True
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


# ---------------------------------------------------------------------------
# The disjointness check at the export boundary. The polygonizer already
# guarantees this upstream; the point of re-checking here is that three
# separate times a vector operation between polygonization and the XML has
# quietly undone it, and the artifact is what a vendor actually parses.
# ---------------------------------------------------------------------------

def _square(lat0, lon0, half=1.0):
    """A closed square ring in GeoJSON [lon, lat] order."""
    return [
        [lon0 - half, lat0 - half], [lon0 + half, lat0 - half],
        [lon0 + half, lat0 + half], [lon0 - half, lat0 + half],
        [lon0 - half, lat0 - half],
    ]


def _hours_from_rings(rings, hazard="IFR"):
    """One forecast hour holding exactly these rings, the rest empty."""
    features = [
        {"type": "Feature",
         "geometry": {"type": "Polygon", "coordinates": [ring]},
         "properties": {"hazard": hazard, "weather_type": "BR", "cause": "CIG/VIS"}}
        for ring in rings
    ]
    hours = _hours([0, 0, 0, 0, 0], hazard=hazard)
    hours[0]["feature_collection"] = {"type": "FeatureCollection", "features": features}
    return hours


def test_adjacent_rings_sharing_an_edge_are_accepted():
    """
    The normal label-grid case, and the one thing the check must not
    reject: two regions tracing the same boundary from opposite sides.
    They intersect in a LINE -- zero area -- which is not an overlap.
    """
    xml, _ = build_pgen_document(
        "IFR", _hours_from_rings([_square(40.0, -100.0), _square(40.0, -98.0)]), "21"
    )
    assert len(_gfas(xml)) == 2


def test_overlapping_rings_are_rejected_at_export():
    hours = _hours_from_rings([_square(40.0, -100.0), _square(40.0, -99.0)])
    with pytest.raises(OverlappingRingsError, match="share"):
        build_pgen_document("IFR", hours, "21")


def test_nested_rings_are_rejected_at_export():
    hours = _hours_from_rings([_square(40.0, -100.0, half=2.0), _square(40.0, -100.0, half=0.5)])
    with pytest.raises(OverlappingRingsError, match="nested"):
        build_pgen_document("IFR", hours, "21")


def test_rings_at_different_forecast_hours_may_overlap():
    """
    The invariant is per hazard and forecast hour. The same area at F00
    and F03 is one hazard evolving, not two elements claiming the same
    ground -- NMAP2's Filter Control shows one hour at a time.
    """
    hours = _hours([0, 0, 0, 0, 0])
    for index in (0, 1):
        hours[index]["feature_collection"] = {
            "type": "FeatureCollection",
            "features": [{
                "type": "Feature",
                "geometry": {"type": "Polygon", "coordinates": [_square(40.0, -100.0)]},
                "properties": {"hazard": "IFR", "weather_type": "BR", "cause": "CIG"},
            }],
        }
    xml, _ = build_pgen_document("IFR", hours, "21")
    assert len(_gfas(xml)) == 2


def test_v1_output_is_not_checked_so_the_revert_still_exports(monkeypatch):
    """
    Deliberate asymmetry, and worth pinning so nobody "fixes" it: v1
    emits overlapping and nested polygons BY CONSTRUCTION (that is the
    defect v2 exists to remove). Enforcing the invariant there would turn
    the forecaster's one-line revert into an export that always raises.
    MT_OBSC, still on v1, is in the same position.
    """
    import pipeline.hazards.ifr as ifr_module

    monkeypatch.setattr(ifr_module, "USE_LABEL_GRID_POLYGONIZE", False)
    overlapping = _hours_from_rings([_square(40.0, -100.0), _square(40.0, -99.0)])
    xml, sidecar = build_pgen_document("IFR", overlapping, "21")
    assert len(_gfas(xml)) == 2
    assert sidecar["rings_verified_disjoint"] is False

    mt_obsc = _hours_from_rings([_square(40.0, -100.0), _square(40.0, -99.0)], hazard="MT_OBSC")
    xml, sidecar = build_pgen_document("MT_OBSC", mt_obsc, "21")
    assert len(_gfas(xml)) == 2
    assert sidecar["rings_verified_disjoint"] is False


def test_empty_cycle_produces_a_valid_but_empty_document():
    """A quiet cycle must still yield a loadable file, not a crash."""
    xml, sidecar = build_pgen_document("IFR", _hours([0, 0, 0, 0, 0]), "21")
    assert _gfas(xml) == []
    assert sidecar["total_gfa_elements"] == 0
    assert ET.fromstring(xml) is not None


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
