"""
tests/test_tango_schedule.py
------------------------------
TANGO's place in the publish schedule: which NBM cycle a scheduled run
resolves to, which NBM forecast hours its F00-F12 snapshots come from, and
that it runs under its own hazard name.

The schedule itself (cron, timeout, workflow shape) is covered for every
hazard by tests/test_publish_schedule.py, which walks HAZARD_WORKFLOWS and so
picks TANGO up with no TANGO-specific code. What is here is what that file
cannot know: TANGO's forecast-hour mapping and its manifest.
"""

import importlib
import importlib.util
import json
import sys
import types
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from pipeline.publish_schedule import (  # noqa: E402
    BACKSTOP_OFFSET_MINUTES,
    DISPATCH_OFFSET_MINUTES,
    HAZARD_WORKFLOWS,
    NBM_LEAD_TIME_OFFSET_HOURS,
)


def _script(name):
    """Imported by path: .github/scripts/ is not (and shouldn't be) an importable package."""
    path = REPO_ROOT / ".github" / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


resolve_target_cycle = _script("resolve_target_cycle")

# (NBM source cycle, the G-AIRMET package built from it). The four NBM runs
# TANGO builds from are 21/03/09/15Z; each package is six hours later.
CYCLES = [
    (datetime(2026, 11, 9, 21), datetime(2026, 11, 10, 3)),
    (datetime(2026, 11, 10, 3), datetime(2026, 11, 10, 9)),
    (datetime(2026, 11, 10, 9), datetime(2026, 11, 10, 15)),
    (datetime(2026, 11, 10, 15), datetime(2026, 11, 10, 21)),
]


def test_tango_is_scheduled():
    assert HAZARD_WORKFLOWS["tango"] == "generate_tango.yml"


@pytest.mark.parametrize("nbm_cycle, package", CYCLES)
@pytest.mark.parametrize(
    "event_name, trigger_input, offset_minutes",
    [
        ("workflow_dispatch", "railway-cron", DISPATCH_OFFSET_MINUTES),
        ("schedule", "", BACKSTOP_OFFSET_MINUTES),
    ],
    ids=["railway-dispatch", "backstop"],
)
def test_a_scheduled_run_resolves_to_the_right_nbm_cycle(
    nbm_cycle, package, event_name, trigger_input, offset_minutes
):
    # The +3:37 backstop after the 21Z cycle starts at 00:37Z the next day,
    # so this also covers the day rollover.
    now = nbm_cycle + timedelta(minutes=offset_minutes)
    result = resolve_target_cycle.resolve("tango", event_name, trigger_input, now)

    assert result["hazard"] == "tango"
    assert result["nbm_source_cycle"] == nbm_cycle.isoformat() + "Z"
    assert result["model_cycle"] == package.isoformat() + "Z"
    assert result["start_delay_minutes"] == 0


def test_the_preflight_scripts_accept_tango(tmp_path):
    out = tmp_path / "preflight.json"
    assert resolve_target_cycle.main(
        ["--hazard", "tango", "--out", str(out), "--now", "2026-11-10T03:45:00Z",
         "--event-name", "workflow_dispatch", "--trigger-input", "railway-cron"]
    ) == 0
    assert json.loads(out.read_text())["nbm_source_cycle"] == "2026-11-10T03:00:00Z"

    assert _script("should_publish_cycle").main(
        ["--new", str(out), "--existing", str(tmp_path / "absent.json"), "--hazard", "tango"]
    ) == 0


def test_the_workflow_runs_the_preflight_as_tango():
    lines = (REPO_ROOT / ".github" / "workflows" / "generate_tango.yml").read_text().splitlines()
    commands = "\n".join(line for line in lines if not line.lstrip().startswith("#"))
    # resolve_target_cycle, the pre-flight should_publish_cycle, and the
    # publish step's should_publish_cycle.
    assert commands.count("--hazard tango") == 3
    assert "--hazard mtn_obsc" not in commands


@pytest.fixture
def generate_latest_tango(monkeypatch, tmp_path):
    """
    pipeline.generate_latest_tango, importable under CI's light dependency
    set. It reaches pipeline.fetch_nbm (which imports `requests`, not in
    requirements.txt) through gairmet_cycle, for fetch_idx -- used only to
    poll NOAA, which nothing here does. A stand-in module takes its place
    for the duration of the test.

    Only the NBM fetch (prepare_tango_grid) is replaced, per test, by
    _fake_fetch below. Everything after it -- the cache write, the reload,
    the polygonize, the snapshot and manifest files -- is the driver's own
    code, run for real into a temporary OUTPUT_DIR.
    """
    swapped = ("pipeline.fetch_nbm", "pipeline.gairmet_cycle", "pipeline.generate_latest_tango")
    saved = {name: sys.modules.get(name) for name in swapped}
    stub = types.ModuleType("pipeline.fetch_nbm")
    stub.fetch_idx = lambda *a, **k: pytest.fail("this test must not poll NOAA")
    sys.modules["pipeline.fetch_nbm"] = stub
    sys.modules.pop("pipeline.gairmet_cycle", None)
    sys.modules.pop("pipeline.generate_latest_tango", None)
    try:
        module = importlib.import_module("pipeline.generate_latest_tango")
        monkeypatch.setattr(module, "OUTPUT_DIR", tmp_path)
        yield module
    finally:
        # Put back exactly what was there, so no other test sees the stub
        # or a gairmet_cycle bound to it.
        for name, original in saved.items():
            if original is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = original


def _fake_fetch(fetched):
    """
    Stands in for prepare_tango_grid, the NBM download + regrid. Returns a
    real speed grid at the production 0.025 deg spacing: 10 x 10 deg over the
    central Plains, well inside the AOR, calm except a 2.5 x 2.5 deg block
    (~22,000 sq mi) of 60 kt -- above both layers' default thresholds and
    minimum areas, so every hour yields exactly one polygon. The domain is
    deliberately much larger than the block: on a 2 deg domain the smoothing
    and closing run into the grid edge and a solid block comes back as two
    edge strips, which is a property of the tiny domain, not of the driver.
    """
    from pipeline.polygons import GridSpec

    def fetch(date, fxx, layer):
        fetched.append((date, fxx))
        speed_kt = np.zeros((400, 400), dtype=np.float32)
        speed_kt[150:250, 150:250] = 60.0
        grid_spec = GridSpec(west=-104.0, north=45.0, dx=0.025, dy=-0.025)
        return speed_kt, grid_spec, f"idx line for f{fxx:03d}"

    return fetch


@pytest.mark.parametrize("nbm_cycle, package", CYCLES)
def test_f00_to_f12_come_from_nbm_f006_to_f018_of_one_cycle(
    generate_latest_tango, monkeypatch, nbm_cycle, package
):
    from pipeline.hazards.llws import LLWS

    fetched = []
    monkeypatch.setattr(generate_latest_tango, "prepare_tango_grid", _fake_fetch(fetched))

    # 5, not just "a manifest exists": generate_layer() catches any per-hour
    # exception and skips that hour, so a broken cache/polygonize step would
    # otherwise show up only as fewer snapshots.
    assert generate_latest_tango.generate_layer(LLWS, nbm_cycle, package) == 5

    assert [fxx for _date, fxx in fetched] == [6, 9, 12, 15, 18]
    assert {date for date, _fxx in fetched} == {nbm_cycle}, "every hour from the SAME NBM run"
    assert NBM_LEAD_TIME_OFFSET_HOURS == 6

    manifest = json.loads((generate_latest_tango.OUTPUT_DIR / "llws_manifest.json").read_text())
    assert manifest["nbm_source_cycle"] == nbm_cycle.isoformat() + "Z"
    assert manifest["model_cycle"] == package.isoformat() + "Z"
    assert [s["requested_forecast_hour"] for s in manifest["snapshots"]] == [0, 3, 6, 9, 12]
    assert [s["valid_time"] for s in manifest["snapshots"]] == [
        (package + timedelta(hours=h)).isoformat() + "Z" for h in (0, 3, 6, 9, 12)
    ]

    # The real cache-and-polygonize path ran for every hour: each wrote its
    # cache, and each published snapshot holds the 60 kt block's polygon.
    out = generate_latest_tango.OUTPUT_DIR
    for snapshot in manifest["snapshots"]:
        assert (out / snapshot["cache_filename"]).exists(), snapshot["cache_filename"]
        assert snapshot["feature_count"] == 1, snapshot
        published = json.loads((out / snapshot["filename"]).read_text())
        assert len(published["features"]) == 1


def test_both_manifests_say_they_are_uncalibrated(generate_latest_tango, monkeypatch):
    from pipeline.hazards.llws import LLWS
    from pipeline.hazards.sfc_wind import SFC_WIND

    monkeypatch.setattr(generate_latest_tango, "prepare_tango_grid", _fake_fetch([]))

    nbm_cycle, package = CYCLES[1]
    for layer in (SFC_WIND, LLWS):
        assert generate_latest_tango.generate_layer(layer, nbm_cycle, package) == 5, layer.key
        manifest = json.loads((generate_latest_tango.OUTPUT_DIR / f"{layer.key}_manifest.json").read_text())
        assert manifest["uncalibrated"] is True, f"{layer.key} manifest"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
