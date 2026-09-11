#!/usr/bin/env python3
"""
Say which NBM cycle this run is for and which G-AIRMET package it would
produce -- before anything is installed, fetched, or waited for.

WHY IT IS THE FIRST STEP
------------------------
The publish decision used to happen after the NBM fetch and polygon
generation, so a run with nothing to publish did 15-20 minutes of real
work (tens of minutes for MTN OBSC) and discarded it. Moving the decision
to the front is what makes everything after it affordable -- including
waiting for NBM at all.

This script does the cheap half of that decision: pure arithmetic, ZERO
network. The synoptic hours are fixed and the package is the cycle plus
the +6h lead offset, so "which package would this run produce" is
answerable without asking NOAA anything. The workflow then reads the
data branch's manifest and hands both to should_publish_cycle.py; only if
that says publish does the job go on to poll for the cycle
(await_nbm_cycle.py) and do the work.

Order matters: checking publishability BEFORE polling means an
already-published cycle costs seconds rather than up to an hour of
waiting for data nobody is going to use.

NO FALLBACK. nbm_source_cycle is always the target. A run either builds
the cycle it came for or builds nothing -- the old behaviour of falling
back to the previous NBM run rebuilt the package that was already live,
which cost a full fetch and generation to produce something the publish
guard then skipped.

STDLIB-ONLY, like everything else that runs above `pip install`;
tests/test_workflow_dependencies.py fails if that stops being true.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from pipeline.publish_schedule import (  # noqa: E402
    classify_trigger,
    package_for,
    scheduled_start,
    target_nbm_cycle,
)


def resolve(hazard: str, event_name: str, trigger_input: str | None,
            now: datetime) -> dict:
    """
    The run's identity, as a dict shaped like the front of a hazard
    manifest so should_publish_cycle.py can read it with no special case:
    model_cycle is the G-AIRMET package, nbm_source_cycle the NBM run
    behind it.
    """
    trigger = classify_trigger(event_name, trigger_input)
    target = target_nbm_cycle(now)
    nominal = scheduled_start(now, trigger)
    return {
        "hazard": hazard,
        "trigger": trigger,
        "model_cycle": package_for(target).isoformat() + "Z",
        "nbm_source_cycle": target.isoformat() + "Z",
        "target_nbm_cycle": target.isoformat() + "Z",
        "job_start": now.isoformat() + "Z",
        # How late the trigger was. On a railway-cron run this should be
        # ~0; if it is not, the move off `schedule:` did not achieve what
        # it was meant to. None for a manual run, which has no schedule.
        "start_delay_minutes": (
            round((now - nominal).total_seconds() / 60, 1) if nominal else None
        ),
    }


def _write_step_outputs(result: dict) -> None:
    """
    Publish the two fields later steps need as GitHub Actions step
    outputs, when running inside Actions.

    Done here rather than by a heredoc in the workflow YAML: the same two
    values then come from one place in both hazards' workflows, and a
    shell snippet parsing JSON with an inline Python one-liner is a thing
    nobody can test.
    """
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a") as handle:
        handle.write(f"trigger={result['trigger']}\n")
        handle.write(f"nbm_source_cycle={result['nbm_source_cycle']}\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hazard", required=True, choices=["ifr", "mtn_obsc"])
    parser.add_argument("--out", required=True, help="where to write the result JSON")
    parser.add_argument(
        "--event-name",
        default="workflow_dispatch",
        help="github.event_name -- 'schedule' identifies the backstop",
    )
    parser.add_argument(
        "--trigger-input",
        default="",
        help="the workflow_dispatch `trigger` input, verbatim. Classified rather than "
             "trusted: anyone with the Run workflow button can type anything here",
    )
    parser.add_argument("--now", help="ISO timestamp to treat as job start (testing)")
    args = parser.parse_args(argv)

    now = (
        datetime.fromisoformat(args.now.replace("Z", "+00:00")).replace(tzinfo=None)
        if args.now
        else datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)
    )

    result = resolve(args.hazard, args.event_name, args.trigger_input, now)
    Path(args.out).write_text(json.dumps(result, indent=2))
    _write_step_outputs(result)

    delay = result["start_delay_minutes"]
    print(
        f"Run identity: hazard={result['hazard']} trigger={result['trigger']} "
        f"target NBM {result['target_nbm_cycle']} -> package {result['model_cycle']}"
    )
    print(
        f"Trigger was {delay:+.0f} min from its nominal time."
        if delay is not None
        else "Manual run -- no nominal trigger time to be late against."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
