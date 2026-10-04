"""
tests/test_tango_routes.py
----------------------------
The TANGO registry entries (webapp/artifacts.py) and routes
(webapp/tango_routes.py), driven through the real ASGI app against
SYNTHETIC caches written to a temporary "local output" directory. No
network, nothing from output/, and -- like test_cache_headers.py -- no
fastapi TestClient, which needs httpx and CI does not install it.

The synthetic hours are chosen so every error path has a real hour behind it:

    F00  good: a 35 kt block (with a 45 kt core) inside the ARTCC area
    F03  good but quiet: all zeros -> a valid, EMPTY FeatureCollection
    F06  cache present but wrong grid key        -> 409
    F09  cache saved with a different scale      -> 409
    F12  manifest names a cache that is not on disk -> 503
    (anything else, e.g. F99                     -> 404)
"""

import asyncio
import json
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.hazards.llws import LLWS  # noqa: E402
from pipeline.hazards.sfc_wind import SFC_WIND  # noqa: E402
from pipeline.hazards.tango_common import polygonize_tango_grid  # noqa: E402
from pipeline.polygons import GridSpec, geodesic_area_sq_mi, load_grid_cache, save_grid_cache  # noqa: E402
from webapp import artifacts  # noqa: E402
from webapp.main import app  # noqa: E402

CYCLE = "2026-10-04T09:00:00Z"

# Kansas / Nebraska: inside the ARTCC area of responsibility.
SPEC = GridSpec(west=-105.0, north=42.0, dx=0.025, dy=-0.025)
SHAPE = (200, 400)  # 5 deg x 10 deg


def block(grid, lat0, lat1, lon0, lon1, value):
    rows = sorted(int(round((SPEC.north - lat) / -SPEC.dy)) for lat in (lat0, lat1))
    cols = sorted(int(round((lon - SPEC.west) / SPEC.dx)) for lon in (lon0, lon1))
    grid[rows[0]:rows[1], cols[0]:cols[1]] = value
    return grid


def main_field():
    """35 kt block, 45 kt core, plus 2x2-cell specks of 45 kt away from it."""
    grid = np.zeros(SHAPE, dtype=np.float32)
    block(grid, 41.0, 39.0, -103.0, -99.0, 35.0)
    block(grid, 40.5, 39.5, -102.0, -100.0, 45.0)
    for r, c in ((10, 20), (10, 60), (150, 30), (150, 300), (30, 350)):
        grid[r:r + 2, c:c + 2] = 45.0
    return grid


def twin_field(gap_deg):
    """Two big 45 kt blocks separated by gap_deg of longitude."""
    grid = np.zeros(SHAPE, dtype=np.float32)
    block(grid, 41.0, 39.0, -104.0, -102.0, 45.0)
    block(grid, 41.0, 39.0, -102.0 + gap_deg, -100.0 + gap_deg, 45.0)
    return grid


def small_field():
    """One ~1,500 sq mi block."""
    grid = np.zeros(SHAPE, dtype=np.float32)
    block(grid, 40.5, 40.0, -102.0, -101.0, 45.0)
    return grid


def _write_layer(out, layer, grids_by_hour):
    snapshots = []
    for hour, spec in grids_by_hour.items():
        name = f"{layer.key}_f{hour:02d}"
        cache_name = f"{name}_grid.npz"
        kind, grid = spec
        if kind == "good":
            save_grid_cache(out / cache_name, {"speed_kt": grid}, SPEC, scale=2)
            loaded, loaded_spec = load_grid_cache(out / cache_name, scale=2)
            stored = polygonize_tango_grid(loaded["speed_kt"], loaded_spec, layer, _cycle(), hour)
            (out / f"{name}.geojson").write_text(json.dumps(stored))
        elif kind == "wrong_key":
            save_grid_cache(out / cache_name, {"ceiling": grid}, SPEC)
            (out / f"{name}.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": []}))
        elif kind == "wrong_scale":
            save_grid_cache(out / cache_name, {"speed_kt": grid}, SPEC, scale=1)
            (out / f"{name}.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": []}))
        elif kind == "missing_cache":
            (out / f"{name}.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": []}))
        snapshots.append(
            {
                "requested_forecast_hour": hour,
                "valid_time": CYCLE,
                "filename": f"{name}.geojson",
                "cache_filename": cache_name,
                "feature_count": 0,
            }
        )
    (out / f"{layer.key}_manifest.json").write_text(
        json.dumps(
            {
                "category": "TANGO",
                "phenomenon": layer.phenomenon,
                "model_cycle": CYCLE,
                "nbm_source_cycle": "2026-10-04T03:00:00Z",
                "speed_threshold_kt": layer.default_threshold_kt,
                "snapshots": snapshots,
            }
        )
    )


def _cycle():
    from datetime import datetime

    return datetime.fromisoformat(CYCLE.rstrip("Z"))


HOURS = {
    0: ("good", main_field()),
    3: ("good", np.zeros(SHAPE, dtype=np.float32)),
    6: ("wrong_key", main_field()),
    9: ("wrong_scale", main_field()),
    12: ("missing_cache", None),
}


@pytest.fixture
def served(tmp_path, monkeypatch):
    """Both layers published to a local output dir that the app prefers."""
    out = tmp_path / "output"
    out.mkdir()
    for layer in (SFC_WIND, LLWS):
        _write_layer(out, layer, HOURS)
    monkeypatch.setattr(artifacts, "LOCAL_OUTPUT_DIR", out)
    monkeypatch.setattr(artifacts, "CACHE_DIR", tmp_path / "cache")
    return out


@pytest.fixture
def nothing_loaded(tmp_path, monkeypatch):
    monkeypatch.setattr(artifacts, "LOCAL_OUTPUT_DIR", tmp_path / "empty-output")
    monkeypatch.setattr(artifacts, "CACHE_DIR", tmp_path / "empty-cache")


def get(path):
    """One GET through the ASGI app -> (status, headers, parsed JSON or None)."""
    raw_path, _, query = path.partition("?")
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": "GET", "scheme": "http", "path": raw_path, "raw_path": raw_path.encode(),
        "query_string": query.encode(), "root_path": "",
        "headers": [(b"host", b"testserver")],
        "client": ("127.0.0.1", 12345), "server": ("testserver", 80),
    }
    captured = {"body": b""}

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        if message["type"] == "http.response.start":
            captured["status"] = message["status"]
            captured["headers"] = {k.decode().lower(): v.decode() for k, v in message["headers"]}
        elif message["type"] == "http.response.body":
            captured["body"] += message.get("body", b"")

    asyncio.run(app(scope, receive, send))
    try:
        body = json.loads(captured["body"])
    except ValueError:
        body = None
    return captured["status"], captured["headers"], body


def areas(fc):
    return [f["properties"]["area_sq_mi"] for f in fc["features"]]


LAYERS = [SFC_WIND, LLWS]
KEYS = [layer.key for layer in LAYERS]


# --- registry ---------------------------------------------------------------


def test_new_hazards_appear_in_status_on_the_shared_branch():
    hazards = artifacts.status()["hazards"]
    for key, manifest in (("sfc_wind", "sfc_wind_manifest.json"), ("llws", "llws_manifest.json")):
        assert key in hazards
        assert artifacts.HAZARDS[key].manifest_name == manifest
        assert hazards[key]["branch"] == "data-tango"
    assert {"ifr", "mtn_obsc"} <= set(hazards)  # the existing ones are still there


def test_branch_env_overrides_follow_the_existing_pattern(monkeypatch):
    import importlib

    monkeypatch.setenv("ARTIFACT_BRANCH_SFC_WIND", "staging-wind")
    monkeypatch.setenv("ARTIFACT_BRANCH_LLWS", "staging-llws")
    try:
        importlib.reload(artifacts)
        assert artifacts.HAZARDS["sfc_wind"].branch == "staging-wind"
        assert artifacts.HAZARDS["llws"].branch == "staging-llws"
    finally:
        monkeypatch.undo()
        importlib.reload(artifacts)


def test_refresh_picks_up_the_new_keys_from_one_shared_branch(tmp_path, monkeypatch):
    """
    refresh_all() and the status dict are driven by HAZARDS, so both layers
    are fetched without any code naming them. Published to ONE branch, with
    one manifest each; llws' manifest is withheld to show the two fail
    independently.
    """
    branch_dir = tmp_path / "remote" / "o" / "r" / "data-tango"
    branch_dir.mkdir(parents=True)
    _write_layer(branch_dir, SFC_WIND, {0: ("good", main_field())})
    monkeypatch.setattr(artifacts, "RAW_BASE_URL", (tmp_path / "remote").as_uri())
    monkeypatch.setattr(artifacts, "REPO_OWNER", "o")
    monkeypatch.setattr(artifacts, "REPO_NAME", "r")
    monkeypatch.setattr(artifacts, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(artifacts, "LOCAL_OUTPUT_DIR", tmp_path / "no-local")
    for key in ("sfc_wind", "llws"):
        monkeypatch.setattr(artifacts, "_STATE", {**artifacts._STATE, key: artifacts.HazardState()})

    artifacts.refresh_all()
    hazards = artifacts.status()["hazards"]
    assert hazards["sfc_wind"]["model_cycle"] == CYCLE
    assert hazards["sfc_wind"]["nbm_source_cycle"] == "2026-10-04T03:00:00Z"
    assert hazards["sfc_wind"]["source"] == "remote"
    assert (tmp_path / "cache" / "sfc_wind" / "sfc_wind_f00_grid.npz").exists()
    assert hazards["llws"]["model_cycle"] is None and hazards["llws"]["last_error"]

    _write_layer(branch_dir, LLWS, {0: ("good", main_field())})
    artifacts.refresh_all()
    hazards = artifacts.status()["hazards"]
    assert hazards["llws"]["model_cycle"] == CYCLE and hazards["llws"]["source"] == "remote"
    assert (tmp_path / "cache" / "llws" / "llws_f00.geojson").exists()
    assert not (tmp_path / "cache" / "sfc_wind" / "llws_f00.geojson").exists()  # no cross-contamination


# --- routes: manifest and stored snapshot -------------------------------------


@pytest.mark.parametrize("key", KEYS)
def test_manifest_route(served, key):
    status, _h, body = get(f"/api/hazards/{key}/manifest")
    assert status == 200
    assert body["category"] == "TANGO" and body["model_cycle"] == CYCLE


@pytest.mark.parametrize("key", KEYS)
def test_manifest_is_a_literal_route_not_taken_for_an_hour(served, key):
    # If /{fxx} matched first, "manifest" would be looked up as an hour -> 404.
    assert get(f"/api/hazards/{key}/manifest")[0] == 200


@pytest.mark.parametrize("key", KEYS)
def test_stored_snapshot(served, key):
    status, _h, body = get(f"/api/hazards/{key}/00")
    assert status == 200 and len(body["features"]) == 1
    assert get(f"/api/hazards/{key}/0")[0] == 200  # hour matching is by number, as for the other hazards


@pytest.mark.parametrize("key", KEYS)
def test_an_empty_hour_is_a_valid_empty_feature_collection(served, key):
    status, _h, body = get(f"/api/hazards/{key}/03")
    assert status == 200
    assert body["type"] == "FeatureCollection" and body["features"] == []


@pytest.mark.parametrize("key", KEYS)
@pytest.mark.parametrize("suffix", ["/99", "/99/recompute", "/abc"])
def test_unknown_hour_is_404(served, key, suffix):
    assert get(f"/api/hazards/{key}{suffix}")[0] == 404


@pytest.mark.parametrize("key", KEYS)
@pytest.mark.parametrize("suffix", ["/manifest", "/00", "/00/recompute"])
def test_nothing_loaded_is_503_with_an_explanation(nothing_loaded, key, suffix):
    status, _h, body = get(f"/api/hazards/{key}{suffix}")
    assert status == 503
    assert "not loaded yet" in body["detail"]


@pytest.mark.parametrize("key", KEYS)
def test_snapshot_named_in_the_manifest_but_missing_on_disk_is_503(served, key):
    (served / f"{key}_f00.geojson").unlink()
    assert get(f"/api/hazards/{key}/00")[0] == 503


# --- routes: recompute ------------------------------------------------------


@pytest.mark.parametrize("layer", LAYERS, ids=KEYS)
def test_recompute_defaults_reproduce_the_known_polygon(served, layer):
    status, _h, fc = get(f"/api/hazards/{layer.key}/00/recompute")
    assert status == 200 and fc["type"] == "FeatureCollection"
    # Both layers' defaults are met by the 45 kt core and the block at the
    # sfc_wind default; for llws (40 kt) only the core qualifies, so compare
    # against the block that each layer's default threshold actually selects.
    if layer is SFC_WIND:
        expected = geodesic_block_area(41.0, 39.0, -103.0, -99.0)
    else:
        expected = geodesic_block_area(40.5, 39.5, -102.0, -100.0)
    assert len(fc["features"]) == 1  # the 2x2-cell specks are smoothed away
    [feature] = fc["features"]
    assert 0.8 * expected <= feature["properties"]["area_sq_mi"] <= 1.3 * expected
    props = feature["properties"]
    assert props["category"] == "TANGO" and props["phenomenon"] == layer.phenomenon
    assert props["speed_threshold_kt"] == layer.default_threshold_kt
    assert props["neighborhood_radius_nm"] == layer.default_neighborhood_radius_nm
    assert props["peak_speed_kt"] == 45.0
    assert props["valid_time"] == "2026-10-04T09:00:00Z" and props["forecast_hour"] == 0


def geodesic_block_area(lat0, lat1, lon0, lon1):
    from shapely.geometry import box

    return geodesic_area_sq_mi(box(lon0, lat1, lon1, lat0))


def test_recompute_at_defaults_equals_polygonizing_the_loaded_cache(served):
    """The route adds nothing of its own: same grid, same function, same answer."""
    loaded, spec = load_grid_cache(served / "sfc_wind_f00_grid.npz", scale=2)
    expected = polygonize_tango_grid(loaded["speed_kt"], spec, SFC_WIND, _cycle(), 0)
    _s, _h, fc = get("/api/hazards/sfc_wind/00/recompute")
    assert [f["properties"] for f in fc["features"]] == [f["properties"] for f in expected["features"]]
    assert [f["geometry"] for f in fc["features"]] == json.loads(json.dumps([f["geometry"] for f in expected["features"]]))


@pytest.mark.parametrize("key", KEYS)
def test_recompute_of_an_empty_hour_is_an_empty_collection(served, key):
    status, _h, fc = get(f"/api/hazards/{key}/03/recompute")
    assert status == 200 and fc["features"] == []


@pytest.mark.parametrize("key", KEYS)
@pytest.mark.parametrize("hour", ["06", "09"])
def test_unusable_cache_is_409_with_a_clear_message(served, key, hour):
    status, _h, body = get(f"/api/hazards/{key}/{hour}/recompute")
    assert status == 409
    assert "outdated format" in body["detail"] and "workflow" in body["detail"]


@pytest.mark.parametrize("key", KEYS)
def test_cache_named_in_the_manifest_but_missing_is_503(served, key):
    status, _h, body = get(f"/api/hazards/{key}/12/recompute")
    assert status == 503 and "isn't on disk" in body["detail"]


@pytest.mark.parametrize(
    "query",
    [
        "speed_threshold_kt=5",
        "speed_threshold_kt=500",
        "smooth_sigma_cells=50",
        "smooth_sigma_cells=-1",
        "neighborhood_radius_nm=151",
        "neighborhood_radius_nm=-1",
        "min_area_sq_mi=10001",
        "min_area_sq_mi=-1",
        "speed_threshold_kt=abc",
    ],
)
@pytest.mark.parametrize("key", KEYS)
def test_out_of_range_parameters_are_422(served, key, query):
    assert get(f"/api/hazards/{key}/00/recompute?{query}")[0] == 422


@pytest.mark.parametrize("layer", LAYERS, ids=KEYS)
def test_each_layers_threshold_bound_is_its_own_range(served, layer):
    lo, hi = layer.threshold_range_kt
    base = f"/api/hazards/{layer.key}/00/recompute"
    assert get(f"{base}?speed_threshold_kt={lo}")[0] == 200
    assert get(f"{base}?speed_threshold_kt={hi}")[0] == 200
    assert get(f"{base}?speed_threshold_kt={lo - 0.5}")[0] == 422
    assert get(f"{base}?speed_threshold_kt={hi + 0.5}")[0] == 422


def test_the_documented_extremes_are_accepted(served):
    q = "smooth_sigma_cells=3&neighborhood_radius_nm=150&min_area_sq_mi=10000"
    assert get(f"/api/hazards/sfc_wind/00/recompute?{q}")[0] == 200
    q = "smooth_sigma_cells=0&neighborhood_radius_nm=0&min_area_sq_mi=0"
    assert get(f"/api/hazards/sfc_wind/00/recompute?{q}")[0] == 200


# --- each parameter moves the result in the expected direction ----------------


def total(fc):
    return sum(areas(fc))


def test_raising_the_threshold_shrinks_then_empties_the_result(served):
    base = "/api/hazards/sfc_wind/00/recompute"
    low = get(f"{base}?speed_threshold_kt=30")[2]    # selects the 35 kt block
    high = get(f"{base}?speed_threshold_kt=40")[2]   # selects only the 45 kt core
    none = get(f"{base}?speed_threshold_kt=50")[2]
    assert total(low) > 2 * total(high) > 0
    assert none["features"] == []


def test_the_threshold_is_greater_than_or_equal(served):
    # A block at exactly 35.0 kt is selected by 35 and not by 35.5.
    base = "/api/hazards/sfc_wind/00/recompute"
    assert len(get(f"{base}?speed_threshold_kt=35&smooth_sigma_cells=0")[2]["features"]) >= 1
    only_core = get(f"{base}?speed_threshold_kt=35.5&smooth_sigma_cells=0")[2]
    assert total(only_core) < total(get(f"{base}?speed_threshold_kt=35&smooth_sigma_cells=0")[2])


def test_smoothing_removes_specks_that_no_smoothing_keeps(served):
    base = "/api/hazards/sfc_wind/00/recompute?neighborhood_radius_nm=0&min_area_sq_mi=0&speed_threshold_kt=40"
    unsmoothed = get(f"{base}&smooth_sigma_cells=0")[2]
    smoothed = get(f"{base}&smooth_sigma_cells=1.5")[2]
    assert len(unsmoothed["features"]) > len(smoothed["features"])


def test_a_larger_radius_joins_nearby_areas(tmp_path, monkeypatch):
    out = tmp_path / "output"
    out.mkdir()
    _write_layer(out, SFC_WIND, {0: ("good", twin_field(gap_deg=0.5))})  # ~23 nm gap
    monkeypatch.setattr(artifacts, "LOCAL_OUTPUT_DIR", out)
    base = "/api/hazards/sfc_wind/00/recompute?smooth_sigma_cells=0"
    separate = get(f"{base}&neighborhood_radius_nm=0")[2]
    joined = get(f"{base}&neighborhood_radius_nm=25")[2]
    assert len(separate["features"]) == 2
    assert len(joined["features"]) == 1
    assert total(joined) > total(separate)  # the bridge adds area


def test_a_larger_minimum_area_drops_small_areas(tmp_path, monkeypatch):
    out = tmp_path / "output"
    out.mkdir()
    _write_layer(out, SFC_WIND, {0: ("good", small_field())})
    monkeypatch.setattr(artifacts, "LOCAL_OUTPUT_DIR", out)
    base = "/api/hazards/sfc_wind/00/recompute?smooth_sigma_cells=0&neighborhood_radius_nm=0"
    assert len(get(f"{base}&min_area_sq_mi=500")[2]["features"]) == 1
    assert get(f"{base}&min_area_sq_mi=5000")[2]["features"] == []


def test_llws_defaults_come_from_its_own_config(served):
    # llws: 40 kt and 25 nm; sfc_wind: 30 kt and 50 nm. The same cached
    # field therefore gives different defaults.
    llws = get("/api/hazards/llws/00/recompute")[2]["features"][0]["properties"]
    wind = get("/api/hazards/sfc_wind/00/recompute")[2]["features"][0]["properties"]
    assert (llws["speed_threshold_kt"], llws["neighborhood_radius_nm"]) == (40.0, 25.0)
    assert (wind["speed_threshold_kt"], wind["neighborhood_radius_nm"]) == (30.0, 50.0)


# --- the existing hazards are not affected ------------------------------------


def test_existing_routes_are_still_the_existing_handlers():
    names = {
        getattr(r, "path", ""): getattr(r, "name", "")
        for r in app.routes
        if "GET" in (getattr(r, "methods", None) or set())
    }
    assert names["/api/hazards/ifr/{fxx}/recompute"] == "recompute_ifr_snapshot"
    assert names["/api/hazards/mtn_obsc/{fxx}/recompute"] == "recompute_mtn_obsc_snapshot"
    assert names["/api/hazards/ifr/{fxx}"] == "get_ifr_snapshot"
    assert names["/api/hazards/mtn_obsc/{fxx}"] == "get_mtn_obsc_snapshot"


def test_tango_routes_are_all_literal_and_registered_before_the_static_mount():
    paths = [getattr(r, "path", "") for r in app.routes]
    tango = [p for p in paths if "/sfc_wind" in p or "/llws" in p]
    assert len(tango) == 6
    assert all("{key}" not in p for p in tango)
    mount = paths.index("")  # the StaticFiles mount at "/" has an empty path
    assert all(paths.index(p) < mount for p in tango)
    for key in KEYS:  # /manifest strictly before /{fxx}
        assert paths.index(f"/api/hazards/{key}/manifest") < paths.index(f"/api/hazards/{key}/{{fxx}}")


def test_there_is_no_export_for_the_new_layers(served):
    posts = {getattr(r, "path", "") for r in app.routes if "POST" in (getattr(r, "methods", None) or set())}
    assert not any("sfc_wind" in p or "llws" in p for p in posts)
    # No `format` parameter exists, so asking for XML still returns GeoJSON.
    status, headers, body = get("/api/hazards/sfc_wind/00?format=xml")
    assert status == 200 and body["type"] == "FeatureCollection"
    status, headers, body = get("/api/hazards/sfc_wind/00/recompute?format=xml")
    assert status == 200 and body["type"] == "FeatureCollection"
