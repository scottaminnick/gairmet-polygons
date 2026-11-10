"""
tests/test_publish_equals_recompute.py
------------------------------------------
The published snapshot must equal the web app's DEFAULT recompute, at every
forecast hour, for IFR and MTN OBSC -- as it already does for TANGO
(tango_common.cache_and_polygonize).

Why this needs saying: the scheduled run used to polygonize the
full-precision grids, while the app recomputes from the cache, which stores
probabilities rounded to whole percent. Revisiting an hour in the viewer
recomputes it, so the map changed at the same settings. Measured on the
2026-11-10 15Z cycle by re-running the pipeline on the original NBM data:
IFR F00 polygonized from full precision reproduced the published file
exactly (3 polygons); the same grids read back from the cache gave 10, the
viewer's revisit. MTN OBSC had the same cause with smaller differences.

The grids here are synthetic but built to be sensitive to exactly that
rounding: a 51.4% block with bands at 50.4% and 49.7%, which the cache reads
back as 51, 50 and 50. So publishing from full precision DOES differ at every
hour (checked below), and the equality is not vacuous.

Published side: the helpers the drivers call (cache_and_polygonize_ifr /
_mtn_obsc), written into a local output directory with a manifest, exactly as
the drivers lay it out. Recompute side: the real web routes, with no query
string, i.e. their defaults.
"""

import json
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import pipeline.hazards.mtn_obsc as mtn  # noqa: E402
from asgi_client import asgi_get  # noqa: E402
from pipeline.hazards.ifr import cache_and_polygonize_ifr, polygonize_ifr_grid_active  # noqa: E402
from pipeline.polygons import GridSpec  # noqa: E402
from webapp import artifacts  # noqa: E402
from webapp.main import app  # noqa: E402

# Utah/Colorado/Wyoming: inside the AOR and on U.S. land (MTN OBSC's land gate).
GRID_SPEC = GridSpec(west=-112.0, north=43.0, dx=0.05, dy=-0.05)
SHAPE = (160, 240)
CYCLE = "2026-11-10T15:00:00Z"
HOURS = (0, 3, 6, 9, 12)
TERRAIN_RADIUS_NM = 12.0


def _cycle():
    return datetime.fromisoformat(CYCLE.rstrip("Z"))


def probability_field(fxx):
    """A different block each hour, with bands the cache's rounding moves across 50%."""
    field = np.zeros(SHAPE, dtype=np.float32)
    top = 20 + 4 * fxx
    field[top:top + 50, 40:140] = 51.4
    field[top + 50:top + 62, 40:140] = 50.4  # in at full precision; 50.0 once cached
    field[top + 62:top + 74, 40:140] = 49.7  # out at full precision; 50.0 once cached
    return field


def mountains():
    """Uniform 5,000 ft relief: every cell mountainous, critical ceiling 5,500 ft AGL."""
    return {
        "baseline_elevation_ft": np.full(SHAPE, 4000.0, dtype=np.float32),
        "ridge_elevation_ft": np.full(SHAPE, 9000.0, dtype=np.float32),
    }


def mtn_prepared(fxx):
    zeros = np.zeros(SHAPE, dtype=np.float32)
    return {
        "ceiling_prob": {t: probability_field(fxx) for t in mtn.CEILING_PROB_THRESHOLDS_FT},
        "precip": zeros, "vis3": zeros, "vis1": zeros,
        "grid_spec": GRID_SPEC, "terrain_radius_nm": TERRAIN_RADIUS_NM,
        **mountains(),
    }


def _features(fc):
    """Comparable form: properties, and geometry as it would be served (JSON)."""
    return [(f["properties"], json.loads(json.dumps(f["geometry"]))) for f in fc["features"]]


@pytest.fixture
def published(tmp_path, monkeypatch):
    """IFR and MTN OBSC published into a local output dir the app prefers."""
    out = tmp_path / "output"
    out.mkdir()
    zeros = np.zeros(SHAPE, dtype=np.float32)
    ifr_snaps, mtn_snaps, ifr_fcs, mtn_fcs = [], [], {}, {}
    for fxx in HOURS:
        cache = f"ifr_f{fxx:02d}_grid.npz"
        fc = cache_and_polygonize_ifr(out / cache, probability_field(fxx), zeros, zeros, zeros,
                                      GRID_SPEC, _cycle(), fxx)
        (out / f"ifr_f{fxx:02d}.geojson").write_text(json.dumps(fc))
        ifr_snaps.append({"requested_forecast_hour": fxx, "actual_forecast_hour": fxx, "substituted": False,
                          "filename": f"ifr_f{fxx:02d}.geojson", "cache_filename": cache,
                          "feature_count": len(fc["features"])})
        ifr_fcs[fxx] = fc

        cache = f"mtn_obsc_f{fxx:02d}_grid.npz"
        fc = mtn.cache_and_polygonize_mtn_obsc(out / cache, mtn_prepared(fxx), _cycle(), fxx)
        (out / f"mtn_obsc_f{fxx:02d}.geojson").write_text(json.dumps(fc))
        mtn_snaps.append({"requested_forecast_hour": fxx, "filename": f"mtn_obsc_f{fxx:02d}.geojson",
                          "cache_filename": cache, "feature_count": len(fc["features"]),
                          "terrain_radius_nm": TERRAIN_RADIUS_NM})
        mtn_fcs[fxx] = fc

    (out / "ifr_manifest.json").write_text(json.dumps({"model_cycle": CYCLE, "snapshots": ifr_snaps}))
    (out / "mtn_obsc_manifest.json").write_text(json.dumps({"model_cycle": CYCLE, "snapshots": mtn_snaps}))
    monkeypatch.setattr(artifacts, "LOCAL_OUTPUT_DIR", out)
    monkeypatch.setattr(artifacts, "CACHE_DIR", tmp_path / "cache")

    # The MTN OBSC route reads terrain from data/terrain/terrain_grid.npz; give
    # it the same synthetic terrain the published side used.
    import pipeline.fetch_terrain as fetch_terrain

    monkeypatch.setattr(fetch_terrain, "load_terrain_grid",
                        lambda path: (mountains(), GRID_SPEC, TERRAIN_RADIUS_NM))
    return {"ifr": ifr_fcs, "mtn_obsc": mtn_fcs}


@pytest.mark.parametrize("fxx", HOURS)
@pytest.mark.parametrize("hazard", ["ifr", "mtn_obsc"])
def test_published_snapshot_equals_the_default_recompute(published, hazard, fxx):
    status, _headers, body = asgi_get(app, f"/api/hazards/{hazard}/{fxx:02d}/recompute")
    assert status == 200, body[:200]
    recompute = json.loads(body)
    assert published[hazard][fxx]["features"], f"{hazard} F{fxx:02d}: nothing published -- the test grid is wrong"
    assert _features(recompute) == _features(published[hazard][fxx]), (
        f"{hazard} F{fxx:02d}: the published snapshot is not what the viewer redraws at the same settings"
    )


@pytest.mark.parametrize("fxx", HOURS)
def test_publishing_from_full_precision_would_differ(fxx):
    """
    The guard on the test above: on these grids, the old publish path -- the
    full-precision grids -- gives different polygons at every hour, so the
    equality cannot pass by the rounding happening not to matter.
    """
    zeros = np.zeros(SHAPE, dtype=np.float32)
    full_ifr = polygonize_ifr_grid_active(probability_field(fxx), zeros, zeros, zeros, GRID_SPEC, _cycle(), fxx)
    prepared = mtn_prepared(fxx)
    full_mtn = mtn.polygonize_mtn_obsc_grid(
        prepared["ceiling_prob"], zeros, zeros, zeros,
        prepared["baseline_elevation_ft"], prepared["ridge_elevation_ft"],
        GRID_SPEC, _cycle(), fxx, terrain_radius_nm=TERRAIN_RADIUS_NM,
    )
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        cached_ifr = cache_and_polygonize_ifr(Path(tmp) / "i.npz", probability_field(fxx), zeros, zeros, zeros,
                                              GRID_SPEC, _cycle(), fxx)
        cached_mtn = mtn.cache_and_polygonize_mtn_obsc(Path(tmp) / "m.npz", prepared, _cycle(), fxx)
    assert _features(full_ifr) != _features(cached_ifr)
    assert _features(full_mtn) != _features(cached_mtn)


def test_the_drivers_publish_from_the_read_back_cache():
    """
    The equality above holds for the helpers; this pins the drivers to them.
    Neither driver may polygonize or write the cache itself any more. Read
    from the parsed source (actual calls, not prose in docstrings), because
    the drivers import the NBM fetch stack, which CI does not install.
    """
    import ast

    def calls(path):
        tree = ast.parse(path.read_text())
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                names.add(func.id if isinstance(func, ast.Name) else getattr(func, "attr", None))
        return names

    for driver, helper, direct in (
        ("generate_latest_ifr.py", "cache_and_polygonize_ifr", "polygonize_ifr_grid_active"),
        ("generate_latest_mtn_obsc.py", "cache_and_polygonize_mtn_obsc", "polygonize_mtn_obsc_grid"),
    ):
        called = calls(REPO_ROOT / "pipeline" / driver)
        assert helper in called, f"{driver} does not publish through {helper}"
        assert direct not in called, f"{driver} still polygonizes directly with {direct}"
        assert "save_grid_cache" not in called, f"{driver} writes the cache itself, outside the helper"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
