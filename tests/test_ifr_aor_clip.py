"""
tests/test_ifr_aor_clip.py
-----------------------------
IFR's final clip to the AOR (holes filled) buffered by AOR_CLIP_BUFFER_DEG.

Why it exists: IFR v2's shared-arc simplification (0.15 deg) can draw a
straight edge across a notch in the boundary. On 2026-11-12 09Z that put IFR
3.97 mi into Canada at Haro Strait / the Strait of Georgia, and the worst
case is ~0.15 deg (~10 mi). The clip BOUNDS the overshoot at
AOR_CLIP_BUFFER_DEG; it does not trace the border.

The synthetic AOR here reproduces that failure: a rectangle with a shallow
notch (0.12 deg deep, 0.6 deg wide) cut into its east side, along one class's edge. The notch is
deeper than one 0.1-deg coarse contour cell, so the mask and the coarse
contour follow it, and shallower than the 0.15-deg simplification
tolerance, so the simplification cuts straight across it.

  (a) IFR output stays within buffer(hole-filled AOR, AOR_CLIP_BUFFER_DEG)
      -- and without the clip it did not (the test is not vacuous);
  (b) MTN OBSC and TANGO are NOT clipped: they never build the clip;
  (c) no gap and no overlap between IFR classes after the clip;
  (d) the clip is an exact no-op when nothing exceeds the buffer;
  and the PR #25 invariant: the published snapshot equals the default
  recompute route with the clip active.
"""

import json
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import shapely
import pytest
from shapely.geometry import Polygon, shape
from shapely.ops import unary_union

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

import pipeline.boundaries as boundaries  # noqa: E402
import pipeline.hazards.ifr as ifr  # noqa: E402
import pipeline.hazards.mtn_obsc as mtn  # noqa: E402
import pipeline.hazards.tango_common as tango  # noqa: E402
from pipeline.hazards.llws import LLWS  # noqa: E402
from pipeline.polygons import GridSpec  # noqa: E402

GRID_SPEC = GridSpec(west=-112.0, north=46.0, dx=0.025, dy=-0.025)
SHAPE = (160, 240)  # lon -112..-106, lat 46..42
CYCLE = datetime(2026, 11, 12, 9)
ZEROS = np.zeros(SHAPE, dtype=np.float32)

# The AOR: everything west of -108, with a notch 0.12 deg deep and 0.6 deg
# wide cut into its east edge -- the Strait of Georgia, in miniature.
NOTCH = Polygon([(-113, 41), (-108, 41), (-108, 44.9), (-108.12, 45.0), (-108.12, 45.4),
                 (-108, 45.5), (-108, 47), (-113, 47)])
FILLED = NOTCH  # no holes to fill in this one
BUFFER = ifr.AOR_CLIP_BUFFER_DEG
EPS = 1e-9


@pytest.fixture
def notch_aor(tmp_path, monkeypatch):
    path = tmp_path / "notch_aor.json"
    path.write_text(json.dumps({"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {}, "geometry": json.loads(json.dumps(NOTCH.__geo_interface__))}]}))
    for module in (ifr, mtn, tango, boundaries):
        monkeypatch.setattr(module, "CONUS_BOUNDARY_PATH", str(path))
    return str(path)


def bands():
    """Three IFR classes stacked north to south, none dominant, all running
    east past the AOR edge. The notch lies along the CIG band's east edge
    alone, as Haro Strait lies along one class's edge: a junction between
    classes at the notch would pin the simplified edge into it."""
    ceiling, vis3, precip = ZEROS.copy(), ZEROS.copy(), ZEROS.copy()
    rows = lambda lat0, lat1: slice(int((46 - lat1) / 0.025), int((46 - lat0) / 0.025))
    cols = slice(int((-111 + 112) / 0.025), SHAPE[1])  # -111 east to the grid edge
    ceiling[rows(44.5, 45.6), cols] = 80.0           # CIG, 1.1 deg
    vis3[rows(43.6, 44.5), cols] = 80.0              # VIS (BR), 0.9 deg
    vis3[rows(42.8, 43.6), cols] = 80.0              # VIS + PCPN, 0.8 deg
    precip[rows(42.8, 43.6), cols] = 80.0
    return ceiling, vis3, ZEROS, precip


def run_ifr(grids, min_area=500.0):
    return ifr.polygonize_ifr_grid_active(*grids, GRID_SPEC, CYCLE, 0,
                                          neighborhood_radius_nm=0.0, min_area_sq_mi=min_area)


def geoms(fc):
    return [shape(f["geometry"]) for f in fc["features"]]


def overshoot_deg(fc):
    """
    Farthest any polygon reaches outside the filled AOR, in degrees. Sampled
    densely (the piece's boundary every 0.001 deg, plus a 0.002-deg grid
    inside it), because the piece's own vertices can all lie ON the AOR line
    -- a chord across a notch meets the line at both ends -- while the
    farthest point is partway along the chord.
    """
    worst = 0.0
    for g in geoms(fc):
        out = g.difference(FILLED)
        for piece in getattr(out, "geoms", [out]):
            if piece.is_empty or piece.geom_type != "Polygon":
                continue
            edge = np.asarray(shapely.segmentize(piece, 0.001).exterior.coords)
            minx, miny, maxx, maxy = piece.bounds
            gx, gy = np.meshgrid(np.arange(minx, maxx, 0.002), np.arange(miny, maxy, 0.002))
            gx, gy = gx.ravel(), gy.ravel()
            inside = shapely.contains_xy(piece, gx, gy)
            cand = np.vstack([edge, np.column_stack([gx[inside], gy[inside]])])
            worst = max(worst, float(shapely.distance(FILLED, shapely.points(cand)).max()))
    return worst


# --- (a) bounded, and not vacuously ---------------------------------------------

def test_without_the_clip_the_simplification_cuts_across_the_notch(notch_aor, monkeypatch):
    monkeypatch.setattr(ifr, "_clip_to_aor", lambda p, props, g: (p, props, g))
    fc = run_ifr(bands())
    assert overshoot_deg(fc) > 1.5 * BUFFER, "the synthetic notch no longer reproduces the failure"


def test_ifr_output_stays_within_the_buffered_aor(notch_aor):
    fc = run_ifr(bands())
    allowed = FILLED.buffer(BUFFER + EPS)
    assert fc["features"], "nothing drawn"
    for g in geoms(fc):
        assert allowed.contains(g), f"IFR reaches {overshoot_deg(fc):.4f} deg past the AOR"
    assert overshoot_deg(fc) <= BUFFER + EPS


def test_the_buffer_is_a_documented_constant_under_one_nbm_cell():
    assert BUFFER == 0.02
    assert BUFFER * 69.0 < 2.5 / 1.609344, "0.02 deg (~1.4 mi) must stay under one 2.5 km NBM cell"


# --- (c) classes stay seamless ------------------------------------------------------

def test_no_gap_or_overlap_between_classes_after_the_clip(notch_aor, monkeypatch):
    clipped = geoms(run_ifr(bands()))
    assert len(clipped) >= 2, "the test needs adjacent classes"
    for i in range(len(clipped)):
        for j in range(i + 1, len(clipped)):
            assert clipped[i].intersection(clipped[j]).area < 1e-10, "classes overlap after the clip"

    # Anything inside the buffer before the clip must still be covered after
    # it. The only allowed difference is output rounding: the GeoJSON writer
    # keeps 6 decimals (1e-6 deg, ~10 cm), which shaves the curved clip edge
    # by a sliver a few cm wide. A real gap between classes would be a cell
    # wide (0.025 deg) or more.
    monkeypatch.setattr(ifr, "_clip_to_aor", lambda p, props, g: (p, props, g))
    unclipped = unary_union(geoms(run_ifr(bands())))
    expected = unclipped.intersection(FILLED.buffer(BUFFER))
    gap = expected.difference(unary_union(clipped))
    for piece in getattr(gap, "geoms", [gap]):
        if piece.is_empty:
            continue
        width = piece.area / max(piece.length / 2, 1e-15)
        assert width < 1e-5, f"the clip opened a gap {width:.2e} deg wide at {piece.bounds}"
    assert gap.area < 1e-6


def test_shared_arcs_stay_shared_across_the_clip_line(notch_aor):
    """Adjacent classes still share their edge after the clip, rather than each ending it separately."""
    clipped = geoms(run_ifr(bands()))
    shared = [clipped[i].intersection(clipped[j]) for i in range(len(clipped)) for j in range(i + 1, len(clipped))]
    assert any(s.length > 1.0 for s in shared), "adjacent classes no longer share their edge"


def test_no_interior_rings_after_the_clip(notch_aor):
    for g in geoms(run_ifr(bands())):
        assert not list(g.interiors)


# --- (d) exact no-op inside ---------------------------------------------------------

def test_the_clip_is_a_no_op_when_nothing_exceeds_the_buffer(notch_aor, monkeypatch):
    ceiling = ZEROS.copy()
    ceiling[40:120, 20:120] = 80.0  # lon -111.5..-109, well inside the AOR
    with_clip = json.dumps(run_ifr((ceiling, ZEROS, ZEROS, ZEROS)))
    monkeypatch.setattr(ifr, "_clip_to_aor", lambda p, props, g: (p, props, g))
    without = json.dumps(run_ifr((ceiling, ZEROS, ZEROS, ZEROS)))
    assert with_clip == without


# --- MIN AREA still applies after the clip -------------------------------------------

def test_min_area_still_drops_only_split_fragments_after_the_clip(notch_aor):
    """Clipping can split; the fragment rule runs after it, and whole classes survive."""
    fc = run_ifr(bands(), min_area=500.0)
    causes = sorted(f["properties"]["cause"] for f in fc["features"])
    assert causes.count("CIG") >= 1 and causes.count("VIS") >= 1


# --- (b) MTN OBSC and TANGO are not clipped -------------------------------------------

def _refuse(*args, **kwargs):
    raise AssertionError("MTN OBSC / TANGO must not build or apply the IFR clip")


def test_mtn_obsc_and_tango_never_build_the_clip(notch_aor, monkeypatch):
    monkeypatch.setattr(boundaries, "get_aor_clip_geometry", _refuse)
    monkeypatch.setattr(ifr, "get_aor_clip_geometry", _refuse)
    field = ZEROS.copy()
    field[40:120, 40:200] = 80.0  # crosses the notch
    mtn.polygonize_mtn_obsc_grid({t: field for t in mtn.CEILING_PROB_THRESHOLDS_FT}, ZEROS, ZEROS, ZEROS,
                                 np.full(SHAPE, 4000.0, dtype=np.float32), np.full(SHAPE, 9000.0, dtype=np.float32),
                                 GRID_SPEC, CYCLE, 0, neighborhood_radius_nm=0.0, min_area_sq_mi=100.0,
                                 terrain_radius_nm=12.0)
    tango.polygonize_tango_grid(field, GRID_SPEC, LLWS, CYCLE, 0, smooth_sigma_cells=0.0,
                                neighborhood_radius_nm=0.0, min_area_sq_mi=100.0)


@pytest.mark.parametrize("path, function", [
    ("pipeline/hazards/mtn_obsc.py", "polygonize_mtn_obsc_grid"),
    ("pipeline/hazards/tango_common.py", "polygonize_tango_grid"),
])
def test_mtn_obsc_and_tango_have_no_clip_step(path, function):
    import ast
    tree = ast.parse((REPO / path).read_text())
    (fn,) = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == function]
    names = {getattr(n.func, "id", getattr(n.func, "attr", "")) for n in ast.walk(fn) if isinstance(n, ast.Call)}
    assert not names & {"_clip_to_aor", "get_aor_clip_geometry"}


# --- the PR #25 invariant, with the clip active ------------------------------------------

def test_published_snapshot_equals_the_default_recompute_with_the_clip(notch_aor, tmp_path, monkeypatch):
    from asgi_client import asgi_get
    from webapp import artifacts
    from webapp.main import app

    out = tmp_path / "output"
    out.mkdir()
    grids = bands()
    fc = ifr.cache_and_polygonize_ifr(out / "ifr_f00_grid.npz", *grids, GRID_SPEC, CYCLE, 0)
    (out / "ifr_f00.geojson").write_text(json.dumps(fc))
    (out / "ifr_manifest.json").write_text(json.dumps({"model_cycle": "2026-11-12T09:00:00Z", "snapshots": [
        {"requested_forecast_hour": 0, "actual_forecast_hour": 0, "substituted": False, "filename": "ifr_f00.geojson",
         "cache_filename": "ifr_f00_grid.npz", "feature_count": len(fc["features"])}]}))
    monkeypatch.setattr(artifacts, "LOCAL_OUTPUT_DIR", out)
    monkeypatch.setattr(artifacts, "CACHE_DIR", tmp_path / "cache")

    status, _headers, body = asgi_get(app, "/api/hazards/ifr/00/recompute")
    assert status == 200, body[:200]
    recompute = json.loads(body)
    assert fc["features"], "nothing published"
    assert overshoot_deg(fc) <= BUFFER + EPS, "the published snapshot is not clipped"
    as_served = lambda c: [(f["properties"], json.loads(json.dumps(f["geometry"]))) for f in c["features"]]
    assert as_served(recompute) == as_served(fc)
