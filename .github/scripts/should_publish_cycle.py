#!/usr/bin/env python3
"""
Cycle monotonicity guard for the hazard publish steps.

Each generate_*.yml workflow FORCE-pushes its data branch, replacing it
outright. That is deliberate (it's what keeps the branch one commit deep
forever), but it means a run publishing an OLD model cycle silently
overwrites a newer one, and the web app -- which just follows
model_cycle -- would follow it backwards to stale data with no error
anywhere.

Each workflow's `concurrency:` group covers the common case by never
letting two runs of the same hazard overlap. It cannot cover re-running
an old workflow run from the Actions tab days later: that run regenerates
nothing, it republishes whatever artifacts its original execution
produced. This guard is the backstop for exactly that.

Exit codes:
    0   publish -- the new cycle is strictly newer, or there is nothing
        to compare against
    10  skip -- the branch already holds a cycle newer than or equal to
        this one. A correct outcome, not a failure; the caller turns
        this into a clean exit.

        There are TWO ways to reach it, and the log has to say which:

          already-published  the target NBM had arrived, this run built
                             the right cycle, and something got there
                             first -- a manual run, a re-run, or the
                             cycle simply having been published already.
                             Routine.

          nbm-not-yet        the target NBM had NOT arrived, so the run
                             fell back to an older one and would rebuild
                             the cycle that is already live. Under the
                             polling schedule this can only happen after
                             the poll ran all the way to its +3:30
                             deadline (see pipeline/publish_schedule.py),
                             so there is no later attempt behind it: the
                             cycle is being MISSED, not delayed, and it
                             logs at warning level. A manual run, which
                             does not poll, is the one exception and is
                             reported as such rather than alarmed about.
    1   error -- the manifest being published is missing or unparseable,
        which means the generate step produced something broken

WHERE THIS RUNS, AND WHY TWICE. It is called at TWO points in each
generate workflow:

  - as a PRE-FLIGHT, against the poller's result file
    (.github/scripts/await_nbm_cycle.py writes a model_cycle /
    nbm_source_cycle pair shaped exactly like the front of a manifest),
    before `pip install` and before any NBM data is fetched. A skip
    there ends the run in seconds. This is what makes polling
    affordable, and it is why this script and everything it imports must
    stay stdlib-only.
  - again at publish time, against the manifest actually produced,
    because the pre-flight answer is minutes old by then and the
    force-push has to be guarded on what is true at the moment it
    happens.

Deliberately a repo script rather than inline YAML in both workflows:
one copy of the comparison, and it can be unit-tested (see
tests/test_should_publish_cycle.py) instead of only ever being exercised
by a real scheduled production run.
"""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# publish_schedule is stdlib-only precisely so this guard can share the
# schedule numbers without dragging requests/numpy into the publish step.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from pipeline.publish_schedule import (  # noqa: E402
    POLL_DEADLINE_OFFSET_MINUTES,
    poll_start_offset_minutes,
    target_nbm_cycle,
)


def _read_cycle(path):
    """
    The model_cycle in a manifest file, or None if it can't be read.

    None means "no usable comparison" and is ALWAYS treated as
    permission to publish: an absent branch (first run ever), an
    unreadable or partially-written manifest, or a manifest from an
    older pipeline version that predates this field must never be able
    to wedge publishing shut. The guard exists to stop a specific,
    narrow mistake -- not to become a new way for the pipeline to stall.
    """
    if not path:
        return None
    try:
        with open(path) as f:
            manifest = json.load(f)
    except (OSError, ValueError):
        return None
    return parse_cycle(manifest.get("model_cycle"))


def parse_cycle(value):
    """
    Parse a manifest model_cycle ("2026-08-19T09:00:00Z") to a datetime,
    or None if it isn't a timestamp we recognize.

    Deliberately not a plain string comparison: those happen to sort
    correctly for this exact format, and would silently stop doing so
    the moment an offset or fractional seconds appeared.
    """
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None


def _read_field(path, field):
    if not path:
        return None
    try:
        with open(path) as f:
            return json.load(f).get(field)
    except (OSError, ValueError):
        return None


def _did_poll(new_manifest_path, polled_flag):
    """
    Whether this run actually waited for its target cycle.

    A scheduled run polls to the +3:30 deadline; workflow_dispatch probes
    once and proceeds, because a person pressing the button wants output
    now, not a runner held for three hours. The distinction decides
    whether falling back to an older NBM is an alarm or a shrug, so it is
    never guessed: the workflow passes --polled explicitly, and the
    poller's own result file carries deadline_reached as a cross-check
    for the pre-flight call. Absent both -- an older manifest, a hand
    invocation -- the conservative answer is "yes", which alarms rather
    than staying quiet about a possibly-missed cycle.
    """
    if polled_flag is not None:
        return polled_flag == "yes"
    reached = _read_field(new_manifest_path, "deadline_reached")
    if reached is None:
        return True
    return bool(reached)


def describe_skip(new_manifest_path, hazard, now, polled_flag=None):
    """
    Which of the two skips this is, as (label, message, is_warning).

    Decided from the NBM cycle the run actually used (nbm_source_cycle in
    the manifest or pre-flight file it just built) against the one it
    should have been looking for at this time -- not from the G-AIRMET
    cycle, which is the same in both cases and so cannot tell them apart.

    Under the polling schedule the second case carries much more weight
    than it used to. There is one job per hazard per cycle, so a run that
    fell back to an older NBM has already polled to its deadline and
    given up; nothing else is coming.
    """
    found = parse_cycle(_read_field(new_manifest_path, "nbm_source_cycle"))
    if found is None or hazard is None:
        return "unknown", "Could not determine which NBM cycle this run used.", False

    target = target_nbm_cycle(now)
    if found.tzinfo is not None:
        found = found.replace(tzinfo=None)

    if found >= target:
        return (
            "already-published",
            f"The target NBM cycle ({target:%Y-%m-%d %H}Z) had arrived and this run built from "
            f"it; that package is already on the branch. Routine.",
            False,
        )

    # A run that never polled -- workflow_dispatch, which probes once and
    # proceeds rather than holding a runner for hours. Falling back is
    # then just what the button does before NBM has posted, and no cycle
    # is being missed: the scheduled job for it is still to come, or
    # still waiting.
    if not _did_poll(new_manifest_path, polled_flag):
        return (
            "nbm-not-yet",
            f"NBM {target:%Y-%m-%d %H}Z is not posted yet and this run did not poll for it "
            f"(a manual run); it fell back to {found:%Y-%m-%d %H}Z, which is already live. "
            f"The scheduled polling job for this cycle is unaffected.",
            False,
        )

    window = f"+{poll_start_offset_minutes(hazard) // 60}:{poll_start_offset_minutes(hazard) % 60:02d}"
    deadline = f"+{POLL_DEADLINE_OFFSET_MINUTES // 60}:{POLL_DEADLINE_OFFSET_MINUTES % 60:02d}"
    return (
        "nbm-not-yet",
        f"NBM {target:%Y-%m-%d %H}Z was STILL not posted when the poll hit its {deadline} "
        f"deadline; this run fell back to {found:%Y-%m-%d %H}Z and would rebuild the cycle "
        f"that is already live. There is no further run scheduled for this cycle, so the "
        f"G-AIRMET package seeded by NBM {target:%H}Z is being MISSED, not delayed. If this "
        f"repeats, the poll window ({window} to {deadline}) closes too early for NBM's real "
        f"arrival -- see the NBM-ARRIVAL lines in this run's log, whose arrival_min field is "
        f"exactly the number to set it from.",
        True,
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--new", required=True, help="manifest about to be published")
    parser.add_argument(
        "--existing",
        help="manifest currently on the data branch; may be absent or empty if the branch has none",
    )
    parser.add_argument(
        "--hazard",
        choices=["ifr", "mtn_obsc"],
        help="which hazard's schedule this run belongs to; without it a skip cannot be "
             "classified and is reported as unknown rather than guessed at",
    )
    parser.add_argument(
        "--polled",
        choices=["yes", "no"],
        help="whether this run waited for its target NBM cycle. The workflows pass 'yes' for "
             "scheduled runs and 'no' for workflow_dispatch; omitted, it is read from the "
             "poller's result file, and failing that assumed 'yes'",
    )
    parser.add_argument("--now", help="ISO timestamp to evaluate against (testing)")
    args = parser.parse_args(argv)

    now = (
        datetime.fromisoformat(args.now.replace("Z", "+00:00")).replace(tzinfo=None)
        if args.now
        else datetime.now(timezone.utc).replace(tzinfo=None)
    )

    new_cycle = _read_cycle(args.new)
    if new_cycle is None:
        print(f"ERROR: no usable model_cycle in the manifest being published ({args.new})", file=sys.stderr)
        return 1

    existing_cycle = _read_cycle(args.existing)
    if existing_cycle is None:
        print(f"No comparable cycle on the branch yet -- publishing {new_cycle.isoformat()}.")
        return 0

    if existing_cycle >= new_cycle:
        label, explanation, is_warning = describe_skip(args.new, args.hazard, now, args.polled)
        header = "WARNING -- SKIPPING PUBLISH" if is_warning else "SKIPPING PUBLISH"
        message = (
            f"{header} [{label}]: the branch already holds {existing_cycle.isoformat()}, which is "
            f"newer than or equal to this run's {new_cycle.isoformat()}. Force-pushing would walk "
            f"the live site's data BACKWARDS.\n  {explanation}"
        )
        # Warnings go to stderr so they stand out in a job log that is
        # otherwise all routine skips, and so ::warning:: style tooling can
        # pick them up later without reparsing everything.
        print(message, file=sys.stderr if is_warning else sys.stdout)
        return 10

    print(f"Branch holds {existing_cycle.isoformat()}; publishing newer {new_cycle.isoformat()}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
