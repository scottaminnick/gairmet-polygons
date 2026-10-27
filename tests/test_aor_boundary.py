"""
tests/test_aor_boundary.py
------------------------------
Pins the area-of-responsibility boundary every hazard gates on
(pipeline.boundaries.CONUS_BOUNDARY_PATH, the U.S. domestic FIR -- see
docs/METHODS.md section 3).

Two kinds of check:

- The file itself: no interior ring big enough to matter, because
  load_boundary_mask() fills holes on purpose (see its docstring). The old
  artcc.json union had a 32,500 sq mi hole over NE PA, NJ and S NY that
  came out right only because of that.
- Named places along the Canadian border, the places where the old
  20-ARTCC union and the domestic FIR disagree. Each is checked twice:
  against the vector polygon, and against the rasterized mask the
  hazards actually use.
"""

import json
import sys
from pathlib import Path

import pytest
from shapely.geometry import Point, Polygon, shape
from shapely.ops import unary_union

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.boundaries import CONUS_BOUNDARY_PATH, get_boundary_mask
from pipeline.polygons import MAX_BOUNDARY_HOLE_SQ_MI, GridSpec, geodesic_area_sq_mi

REPO = Path(__file__).resolve().parent.parent
LEGACY_ARTCC_PATH = "data/boundaries/artcc.json"

# The northeast quarter of the production grid: same 0.025 deg cells and
# the same origin as the cached NBM grids on the data-* branches
# (west -138.27326815395995 + 1650 cells, north 56.988589282434305 - 280
# cells), so a point lands in the same cell it does in production.
GRID_SPEC = GridSpec(west=-97.02326815395995, north=49.988589282434305, dx=0.025, dy=-0.025)
SHAPE = (440, 1250)

INSIDE = {
    "Caribou, ME": (-68.01, 46.86),
    "Detroit, MI": (-83.05, 42.33),
    "Cleveland, OH": (-81.69, 41.50),
    "Erie, PA": (-80.09, 42.13),
    "NE PA (old union's hole)": (-75.5, 41.4),
    "U.S. Lake Superior": (-88.0, 47.5),
}
OUTSIDE = {
    "Toronto, ON": (-79.4, 43.7),
    # 0.8 mi from the edge: International Falls is across the Rainy River.
    "Fort Frances, ON": (-93.4, 48.6),
}

# U.S. towns the 1:110m Natural Earth outline is too coarse to keep
# inside (docs/METHODS.md section 3, "Known gaps"). Strict, so a rebuild
# that fixes them fails here and the marker gets removed.
KNOWN_GAPS = {
    "International Falls, MN": (-93.41, 48.60),  # ~1 mi outside; KINL airport is inside
    "Sault Ste. Marie, MI": (-84.35, 46.50),  # ~5.6 mi outside; also outside the old union
}


def _boundary(path):
    with open(REPO / path) as f:
        data = json.load(f)
    return unary_union([shape(feature["geometry"]) for feature in data["features"]])


def _interior_ring_areas_sq_mi(geom):
    parts = list(geom.geoms) if geom.geom_type == "MultiPolygon" else [geom]
    return [geodesic_area_sq_mi(Polygon(ring)) for part in parts for ring in part.interiors]


def _mask_at(lon, lat):
    mask = get_boundary_mask(GRID_SPEC, SHAPE, str(REPO / CONUS_BOUNDARY_PATH))
    row, col = GRID_SPEC.lonlat_to_pixel(lon, lat)
    return bool(mask[round(row), round(col)])


def test_active_boundary_has_no_interior_ring_over_limit():
    areas = _interior_ring_areas_sq_mi(_boundary(CONUS_BOUNDARY_PATH))
    too_big = [round(a) for a in areas if a > MAX_BOUNDARY_HOLE_SQ_MI]
    assert not too_big, (
        f"{CONUS_BOUNDARY_PATH} has interior ring(s) of {too_big} sq mi. load_boundary_mask() "
        f"fills holes, so these would silently count as inside the AOR -- fix the file."
    )


def test_hole_check_catches_the_legacy_union():
    # The guard above is only worth having if it fires on the real case.
    areas = _interior_ring_areas_sq_mi(_boundary(LEGACY_ARTCC_PATH))
    assert max(areas) > 30_000


def test_active_boundary_is_conus_aor():
    assert CONUS_BOUNDARY_PATH == "data/boundaries/conus_aor.json"


@pytest.mark.parametrize("name", INSIDE)
def test_inside(name):
    lon, lat = INSIDE[name]
    assert _boundary(CONUS_BOUNDARY_PATH).contains(Point(lon, lat)), f"{name} polygon"
    assert _mask_at(lon, lat), f"{name} rasterized mask"


@pytest.mark.parametrize("name", OUTSIDE)
def test_outside(name):
    lon, lat = OUTSIDE[name]
    assert not _boundary(CONUS_BOUNDARY_PATH).contains(Point(lon, lat)), f"{name} polygon"
    assert not _mask_at(lon, lat), f"{name} rasterized mask"


@pytest.mark.xfail(strict=True, reason="1:110m U.S. outline too coarse at the border; see METHODS section 3")
@pytest.mark.parametrize("name", KNOWN_GAPS)
def test_known_us_gaps_are_inside(name):
    lon, lat = KNOWN_GAPS[name]
    assert _boundary(CONUS_BOUNDARY_PATH).contains(Point(lon, lat))
