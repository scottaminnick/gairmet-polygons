"""
tests/test_hand_run_cycle_search.py
--------------------------------------
A hand run (`python3 pipeline/generate_latest_ifr.py` with NBM_SOURCE_CYCLE
unset) falls back to gairmet_cycle.find_latest_gairmet_cycle(), which logs
an NBM-ARRIVAL line for the cycle it found. That call used to pass only
(target, hazard) to log_nbm_arrival(), which also requires trigger and
job_start, so a hand run died with a TypeError the moment it found a cycle.

The workflows never reach this path (they set NBM_SOURCE_CYCLE from the
poller), which is why it went unnoticed.
"""

import importlib
import sys
import types
from datetime import datetime, timedelta

import pytest

from pipeline.publish_schedule import log_nbm_arrival


@pytest.fixture
def gairmet_cycle(monkeypatch):
    """
    pipeline.gairmet_cycle with pipeline.fetch_nbm (which imports `requests`,
    not installed in CI) replaced by a stand-in: the newest candidate cycle
    is "not yet posted", the next one is.
    """
    probed = []

    def fetch_idx(cycle, fxx):
        probed.append(cycle)
        if len(probed) == 1:
            raise RuntimeError("not posted")
        return "idx"

    swapped = ("pipeline.fetch_nbm", "pipeline.gairmet_cycle")
    saved = {name: sys.modules.get(name) for name in swapped}
    stub = types.ModuleType("pipeline.fetch_nbm")
    stub.fetch_idx = fetch_idx
    sys.modules["pipeline.fetch_nbm"] = stub
    sys.modules.pop("pipeline.gairmet_cycle", None)
    monkeypatch.delenv("NBM_SOURCE_CYCLE", raising=False)
    try:
        module = importlib.import_module("pipeline.gairmet_cycle")
        module.probed = probed
        yield module
    finally:
        for name, original in saved.items():
            if original is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = original


def _arrival_line(out: str) -> str:
    (line,) = [l for l in out.splitlines() if l.startswith("NBM-ARRIVAL ")]
    return line


def test_a_hand_run_without_nbm_source_cycle_finds_a_cycle_and_logs_it(gairmet_cycle, capsys):
    cycle = gairmet_cycle.resolve_nbm_cycle(hazard="ifr")

    assert cycle == gairmet_cycle.probed[1], "the first posted candidate is the one used"
    fields = dict(pair.split("=", 1) for pair in _arrival_line(capsys.readouterr().out).split()[1:])
    assert fields["hazard"] == "ifr"
    assert fields["target"] == f"{cycle:%Y-%m-%dT%H:%M:%S}Z"
    assert fields["trigger"] == "manual", "a hand run has no schedule"
    assert fields["start_delay_min"] == "-", "so no start delay to report"
    assert fields["posted"] == "yes"
    assert fields["arrival_measured"] == "no", "the search did not watch the cycle appear"


def test_the_hand_run_line_has_the_same_format_as_every_other(gairmet_cycle, capsys):
    """Same keys in the same order as the line the workflows' poller writes."""
    gairmet_cycle.resolve_nbm_cycle(hazard="tango")
    hand_run = _arrival_line(capsys.readouterr().out)

    target = datetime(2026, 11, 12, 3)
    log_nbm_arrival(target, "ifr", "railway-cron", target + timedelta(minutes=80),
                    now=target + timedelta(minutes=81))
    workflow = _arrival_line(capsys.readouterr().out)

    def keys(line):
        return [pair.split("=", 1)[0] for pair in line.split()[1:]]

    assert keys(hand_run) == keys(workflow)


def test_the_env_var_path_is_unchanged(gairmet_cycle, monkeypatch, capsys):
    """With NBM_SOURCE_CYCLE set (the workflows), nothing is probed or logged here."""
    monkeypatch.setenv("NBM_SOURCE_CYCLE", "2026-11-12T03:00:00Z")
    assert gairmet_cycle.resolve_nbm_cycle(hazard="ifr") == datetime(2026, 11, 12, 3)
    assert gairmet_cycle.probed == []
    assert "NBM-ARRIVAL" not in capsys.readouterr().out
