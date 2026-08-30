"""
tests/test_terrain_mosaic_cache.py
-------------------------------------
The mosaic checkpoint. Assembling the intermediate mosaic means
downloading 1,708 Skadi tiles and is nearly all of the Fetch Terrain Grid
job's wall time; everything after it is a few minutes of array work. A run
that got through the whole download and then died on the last step -- what
a missing dependency in compute_output_grids actually did -- had to redo
the fetch from scratch.

Two things have to hold for a checkpoint to be worth having, and only one
of them is "it makes things faster":

  - Reusing it must produce EXACTLY what refetching would have. A
    checkpoint that quietly changes the terrain is worse than no
    checkpoint, because the output still looks plausible.
  - A checkpoint built for different bounds or a different resolution must
    be ignored, not reused. The Actions cache key is deliberately coarse,
    so this is the check that actually prevents a wrong answer.
"""

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import fetch_terrain  # noqa: E402
from pipeline.fetch_terrain import (  # noqa: E402
    get_mosaic,
    load_mosaic_cache,
    save_mosaic_cache,
)
from pipeline.grid_spec import GridSpec  # noqa: E402

BOUNDS = (-125.0, 44.0, -120.0, 47.0)
FACTOR = 30


def _fake_mosaic(shape=(120, 200)):
    rng = np.random.default_rng(11)
    # Feet, not metres, and non-integral -- the real mosaic is metres
    # scaled by METERS_TO_FEET, so a checkpoint that quietly narrowed the
    # dtype to int16 would lose data. This catches that.
    return (rng.random(shape) * 14000).astype(np.float32)


def _spec():
    return GridSpec(west=BOUNDS[0], north=BOUNDS[3], dx=1 / 120, dy=-1 / 120)


def test_a_checkpoint_round_trips_bit_for_bit(tmp_path):
    path = tmp_path / "mosaic.npz"
    mosaic = _fake_mosaic()
    save_mosaic_cache(path, mosaic, _spec(), BOUNDS, FACTOR)

    loaded, spec = load_mosaic_cache(path, BOUNDS, FACTOR)
    assert np.array_equal(loaded, mosaic), "the checkpoint is not lossless"
    assert loaded.dtype == mosaic.dtype
    assert (spec.west, spec.north, spec.dx, spec.dy) == (
        _spec().west, _spec().north, _spec().dx, _spec().dy)


def test_reusing_a_checkpoint_never_downloads_anything(tmp_path, monkeypatch):
    """
    The whole point. If get_mosaic reaches the fetch path on a checkpoint
    hit, the job re-downloads 1,708 tiles and nothing says so.
    """
    path = tmp_path / "mosaic.npz"
    mosaic = _fake_mosaic()
    save_mosaic_cache(path, mosaic, _spec(), BOUNDS, FACTOR)

    def explode(*args, **kwargs):
        raise AssertionError("assemble_intermediate_mosaic was called on a checkpoint hit")

    monkeypatch.setattr(fetch_terrain, "assemble_intermediate_mosaic", explode)
    loaded, _ = get_mosaic(str(path), BOUNDS, FACTOR)
    assert np.array_equal(loaded, mosaic)


def test_a_miss_fetches_and_then_leaves_a_checkpoint_behind(tmp_path, monkeypatch):
    path = tmp_path / "mosaic.npz"
    mosaic = _fake_mosaic()
    calls = []

    def fake_assemble(bounds, factor):
        calls.append((bounds, factor))
        return mosaic, _spec()

    monkeypatch.setattr(fetch_terrain, "assemble_intermediate_mosaic", fake_assemble)
    monkeypatch.setattr(fetch_terrain, "list_conus_tiles", lambda bounds: [])

    first, _ = get_mosaic(str(path), BOUNDS, FACTOR)
    assert len(calls) == 1 and path.exists(), "a miss should fetch and checkpoint"

    # Second call must be served from the file just written.
    monkeypatch.setattr(fetch_terrain, "assemble_intermediate_mosaic",
                        lambda *a, **k: pytest.fail("refetched despite a fresh checkpoint"))
    second, _ = get_mosaic(str(path), BOUNDS, FACTOR)
    assert np.array_equal(first, second)


@pytest.mark.parametrize("bounds,factor,why", [
    ((-130.0, 44.0, -120.0, 47.0), FACTOR, "different bounds"),
    (BOUNDS, 15, "different resolution"),
])
def test_a_checkpoint_built_for_other_parameters_is_ignored(tmp_path, bounds, factor, why):
    """
    This is the check that stops a coarse cache key from producing a
    wrong terrain grid -- refetching is merely slow, reuse would be wrong.
    """
    path = tmp_path / "mosaic.npz"
    save_mosaic_cache(path, _fake_mosaic(), _spec(), BOUNDS, FACTOR)
    assert load_mosaic_cache(path, bounds, factor) is None, f"reused a checkpoint with {why}"


def test_a_corrupt_or_absent_checkpoint_is_a_miss_not_a_crash(tmp_path):
    """A truncated cache entry should cost a refetch, not the whole run."""
    assert load_mosaic_cache(tmp_path / "nope.npz", BOUNDS, FACTOR) is None

    truncated = tmp_path / "truncated.npz"
    save_mosaic_cache(truncated, _fake_mosaic(), _spec(), BOUNDS, FACTOR)
    data = truncated.read_bytes()
    truncated.write_bytes(data[: len(data) // 2])
    assert load_mosaic_cache(truncated, BOUNDS, FACTOR) is None


def test_grids_from_a_reused_checkpoint_match_grids_from_the_fetch(tmp_path):
    """
    End to end, and the assertion that actually matters: a checkpointed
    run and a fresh run must produce identical output grids. If they can
    differ, the checkpoint is a correctness bug wearing a performance
    costume.
    """
    deg = 1 / 120
    spec = GridSpec(west=-125.0, north=47.0, dx=deg, dy=-deg)
    rng = np.random.default_rng(3)
    mosaic = (rng.random((360, 600)) * 9000).astype(np.float32)

    direct = fetch_terrain.compute_output_grids(
        mosaic, spec, terrain_radius_nm=12.0, output_bounds=(-125.0, 44.0, -121.0, 47.0))

    path = tmp_path / "mosaic.npz"
    save_mosaic_cache(path, mosaic, spec, BOUNDS, FACTOR)
    cached_mosaic, cached_spec = load_mosaic_cache(path, BOUNDS, FACTOR)
    viacache = fetch_terrain.compute_output_grids(
        cached_mosaic, cached_spec, terrain_radius_nm=12.0,
        output_bounds=(-125.0, 44.0, -121.0, 47.0))

    for name, a, b in zip(("baseline", "ridge"), direct, viacache):
        assert np.array_equal(a, b), f"{name} grid differs between a fresh and a checkpointed run"


def test_the_mosaic_stage_stops_before_computing_grids(tmp_path, monkeypatch):
    """
    The workflow runs --stage mosaic first precisely so the checkpoint is
    saved before anything that can fail; if that stage kept going, the
    save step would not run until after the risky part.
    """
    monkeypatch.setattr(fetch_terrain, "assemble_intermediate_mosaic",
                        lambda bounds, factor: (_fake_mosaic(), _spec()))
    monkeypatch.setattr(fetch_terrain, "list_conus_tiles", lambda bounds: [])
    monkeypatch.setattr(fetch_terrain, "compute_output_grids",
                        lambda *a, **k: pytest.fail("--stage mosaic computed the grids"))

    out = tmp_path / "grid.npz"
    fetch_terrain.main(output_path=str(out), mosaic_cache=str(tmp_path / "m.npz"), stage="mosaic")
    assert not out.exists(), "--stage mosaic wrote the output grid"
    assert (tmp_path / "m.npz").exists(), "--stage mosaic did not leave a checkpoint"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
