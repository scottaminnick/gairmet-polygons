"""
tests/test_boundary_mask_warm.py
-----------------------------------
The boundary-mask memoization's lock, and the web app's start-up warm-up.

Why both exist: rasterizing an area-of-responsibility mask onto a CONUS grid
takes ~38 s, nearly all of it in skimage.draw.polygon, and holds the GIL the
whole time -- so the first APPLY after a deploy stalled every request on the
server. Measured on the real artifacts after this change: health-check
latency stayed under 0.02 s through an 87 s warm-up in a child process, and
the first recompute per hazard took 0.35-1.4 s instead of ~38 s.

The real rasterization is replaced here by a slow, counting stand-in, so
these tests measure the locking and caching, not skimage.
"""

import json
import sys
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pipeline.boundaries as boundaries  # noqa: E402
from pipeline.polygons import GridSpec, save_grid_cache  # noqa: E402

SPEC = GridSpec(west=-100.0, north=45.0, dx=0.5, dy=-0.5)
SHAPE = (4, 6)
OTHER_SPEC = GridSpec(west=-90.0, north=40.0, dx=0.5, dy=-0.5)


@pytest.fixture
def slow_loader(monkeypatch):
    """A fresh, empty memo and a stand-in rasterizer that is slow and counts calls."""
    monkeypatch.setattr(boundaries, "_boundary_mask_cache", {})
    monkeypatch.setattr(boundaries, "_boundary_mask_locks", {})
    calls = []

    def fake(path, grid_spec, shape):
        calls.append((path, grid_spec.west, tuple(shape)))
        time.sleep(0.2)
        return np.ones(shape, dtype=bool)

    monkeypatch.setattr(boundaries, "load_boundary_mask", fake)
    return calls


def test_concurrent_first_callers_compute_the_mask_once(slow_loader):
    results = []
    threads = [threading.Thread(target=lambda: results.append(boundaries.get_boundary_mask(SPEC, SHAPE, "a.json")))
               for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(slow_loader) == 1, f"computed {len(slow_loader)} times"
    assert all(r is results[0] for r in results), "callers got different arrays"


def test_different_masks_do_not_wait_for_each_other(slow_loader):
    """One lock per key, not one global lock: two different masks overlap."""
    started = time.monotonic()
    threads = [threading.Thread(target=boundaries.get_boundary_mask, args=(spec, SHAPE, "a.json"))
               for spec in (SPEC, OTHER_SPEC)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(slow_loader) == 2
    assert time.monotonic() - started < 0.35, "the two masks were computed one after the other"


def test_warmed_masks_are_served_from_the_cache(slow_loader):
    with ThreadPoolExecutor(max_workers=1) as pool:
        requests = [(SPEC, SHAPE, "a.json"), (SPEC, SHAPE, "b.json")]
        assert boundaries.warm_boundary_masks(requests, pool) == 2
        assert boundaries.warm_boundary_masks(requests, pool) == 0, "an already-warm mask was recomputed"
    before = len(slow_loader)
    boundaries.get_boundary_mask(SPEC, SHAPE, "a.json")
    boundaries.get_boundary_mask(SPEC, SHAPE, "b.json")
    assert len(slow_loader) == before, "a request recomputed a warmed mask"


def test_a_request_during_warm_up_waits_for_the_warm_result(slow_loader):
    """
    The point of holding the key's lock while the child process works: a
    request that needs the same mask meanwhile must not start its own
    (GIL-holding) computation.
    """
    release = threading.Event()

    class HeldExecutor:
        """Runs the work only once released, as a busy child process would."""

        def submit(self, fn, *args):
            future = Future()

            def run():
                release.wait(5)
                future.set_result(fn(*args))

            threading.Thread(target=run).start()
            return future

    warm = threading.Thread(target=boundaries.warm_boundary_masks, args=([(SPEC, SHAPE, "a.json")], HeldExecutor()))
    warm.start()
    time.sleep(0.05)  # the warm-up now holds the key's lock
    got = []
    request = threading.Thread(target=lambda: got.append(boundaries.get_boundary_mask(SPEC, SHAPE, "a.json")))
    request.start()
    time.sleep(0.1)
    assert not got, "the request did not wait for the warm-up"
    release.set()
    warm.join()
    request.join()
    assert len(slow_loader) == 1, "the request computed the mask itself"
    assert got[0] is boundaries._boundary_mask_cache[boundaries._cache_key(SPEC, SHAPE, "a.json")]


# --- which grids the web app warms -------------------------------------------

@pytest.fixture
def published(tmp_path, monkeypatch):
    from webapp import artifacts

    out = tmp_path / "output"
    out.mkdir()
    grid = np.zeros(SHAPE, dtype=np.float32)
    save_grid_cache(out / "ifr_f00_grid.npz", {"ceiling": grid, "visibility_3sm": grid,
                                               "visibility_1sm": grid, "precipitation": grid}, SPEC)
    (out / "ifr_manifest.json").write_text(json.dumps(
        {"snapshots": [{"requested_forecast_hour": 0, "cache_filename": "ifr_f00_grid.npz"}]}))
    save_grid_cache(out / "llws_f00_grid.npz", {"speed_kt": grid}, OTHER_SPEC, scale=2.0)
    (out / "llws_manifest.json").write_text(json.dumps(
        {"snapshots": [{"requested_forecast_hour": 0, "cache_filename": "llws_f00_grid.npz"}]}))
    monkeypatch.setattr(artifacts, "LOCAL_OUTPUT_DIR", out)
    monkeypatch.setattr(artifacts, "CACHE_DIR", tmp_path / "cache")
    return out


def test_the_warm_up_reads_each_hazards_grid_from_its_published_cache(published):
    from pipeline.boundaries import CONUS_BOUNDARY_PATH
    from webapp.main import boundary_mask_requests

    [(spec, shape, path)] = boundary_mask_requests("ifr")
    assert (spec.west, spec.north, spec.dx, spec.dy, shape, path) == (-100.0, 45.0, 0.5, -0.5, SHAPE, CONUS_BOUNDARY_PATH)
    [(spec, shape, path)] = boundary_mask_requests("llws")  # a scaled cache reads the same way
    assert (spec.west, shape) == (-90.0, SHAPE)
    assert boundary_mask_requests("mtn_obsc") is None, "a hazard with nothing published should wait, not fail"


def test_the_warm_up_covers_every_mask_a_recompute_rasterizes():
    """
    MTN OBSC gates on land as well as the AOR; the others on the AOR only.
    A mask missing here is a ~38 s, server-wide stall on that hazard's
    first APPLY.
    """
    from pipeline.boundaries import CONUS_BOUNDARY_PATH, LAND_BOUNDARY_PATH
    from webapp.main import _mask_paths_by_hazard

    paths = _mask_paths_by_hazard()
    assert set(paths) == {"ifr", "mtn_obsc", "sfc_wind", "llws"}
    assert set(paths["mtn_obsc"]) == {LAND_BOUNDARY_PATH, CONUS_BOUNDARY_PATH}
    for key in ("ifr", "sfc_wind", "llws"):
        assert paths[key] == (CONUS_BOUNDARY_PATH,)


def test_the_warm_up_runs_in_a_child_process_not_a_thread():
    """The rasterization holds the GIL; in a thread it would stall the server exactly as before."""
    import inspect

    from webapp.main import _warm_boundary_masks_when_ready

    source = inspect.getsource(_warm_boundary_masks_when_ready)
    assert "ProcessPoolExecutor" in source and 'get_context("spawn")' in source


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
