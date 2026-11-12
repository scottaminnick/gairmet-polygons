"""
tests/test_railway_settings_doc.py
-------------------------------------
Railway stops reading Config as Code (railway.json / railway.toml, and a
custom path like railway.dispatch.json) on 2026-12-01, so the two dispatch
services are configured by their service settings in the Railway UI, and
RAILWAY_DISPATCH.md's settings table is the one written record of them.

These tests keep that table honest against the code it describes: the cron
lines against pipeline.publish_schedule, the start commands against the
dispatcher's own --trigger flag, and the settings whose absence breaks a
cron service (an explicit start command, restart policy Never).
"""

import importlib.util
import re
import shlex
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from pipeline.publish_schedule import RAILWAY_TRIGGER_OFFSETS, dispatch_cron_entry  # noqa: E402

DOC = (REPO_ROOT / "RAILWAY_DISPATCH.md").read_text()
SERVICES = {"primary": "railway-cron", "backstop": "railway-backstop"}


def _settings_table() -> dict:
    """{setting label: {"primary": value, "backstop": value}} from the doc's settings table."""
    start = DOC.index("| Setting (Railway UI) | Primary (+1:20) | Backstop (+2:30) |")
    rows = {}
    for line in DOC[start:].splitlines()[2:]:
        if not line.startswith("|"):
            break
        cells = [c.strip() for c in line.strip("|").split("|")]
        rows[cells[0]] = {"primary": cells[1], "backstop": cells[2]}
    return rows


def _code(cell: str) -> str:
    match = re.fullmatch(r"`([^`]+)`", cell)
    assert match, f"expected a single code span, got {cell!r}"
    return match.group(1)


TABLE = _settings_table()


def _row(label_fragment: str) -> dict:
    (label,) = [k for k in TABLE if label_fragment in k]
    return TABLE[label]


@pytest.mark.parametrize("service, trigger", SERVICES.items())
def test_the_cron_schedule_matches_the_schedule_module(service, trigger):
    assert _code(_row("Cron Schedule")[service]) == dispatch_cron_entry(trigger)


@pytest.mark.parametrize("service, trigger", SERVICES.items())
def test_the_start_command_runs_the_dispatcher_with_the_right_trigger(service, trigger):
    """
    Explicit, because without one Railpack falls back to the Procfile's
    `web` entry -- the web app -- and the cron service would never dispatch.
    """
    argv = shlex.split(_code(_row("Custom Start Command")[service]))
    assert argv[:2] == ["python3", "scripts/dispatch_workflows.py"]
    flags = argv[2:]
    sent = flags[flags.index("--trigger") + 1] if "--trigger" in flags else "railway-cron"
    assert sent == trigger
    assert sent in RAILWAY_TRIGGER_OFFSETS, "the dispatcher would reject this --trigger"


def test_the_procfile_fallback_is_really_the_web_app():
    """Why the start command must be explicit: the fallback is uvicorn, not the dispatcher."""
    procfile = (REPO_ROOT / "Procfile").read_text()
    assert "uvicorn" in procfile and "dispatch_workflows" not in procfile


@pytest.mark.parametrize("service", SERVICES)
def test_restart_policy_is_never_and_the_builder_is_railpack(service):
    """A cron service that restarts on exit re-POSTs the dispatches in a loop; Nixpacks is deprecated."""
    assert _code(_row("Restart Policy")[service]) == "Never"
    assert _code(_row("Builder")[service]) == "Railpack"


def test_neither_service_points_at_a_config_file():
    """Config as Code stops being read on 2026-12-01; the field is cleared, not repointed."""
    row = _row("Railway Config File")
    assert row["primary"] == row["backstop"] == "*(empty)*"


def test_the_retired_config_files_still_agree_while_they_exist():
    """
    railway.dispatch.json / railway.backstop.json are kept for now as a
    record. While they exist they must not contradict the settings table,
    so a reader comparing the two never finds different schedules.
    """
    import json

    for service, path in (("primary", "railway.dispatch.json"), ("backstop", "railway.backstop.json")):
        file = REPO_ROOT / path
        if not file.exists():
            continue
        deploy = json.loads(file.read_text())["deploy"]
        assert deploy["cronSchedule"] == _code(_row("Cron Schedule")[service])
        assert deploy["startCommand"] == _code(_row("Custom Start Command")[service])
