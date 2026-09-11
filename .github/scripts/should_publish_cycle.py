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

          already-published  this run's target NBM cycle is one whose
                             package is already on the branch. Under the
                             dispatch design this is the ROUTINE outcome
                             for the +3:37 backstop: the Railway-
                             dispatched run did its job an hour earlier,
                             and the backstop correctly does nothing.

          nbm-not-yet        the run would build from an OLDER NBM cycle
                             than the one it should be chasing. The
                             workflows cannot produce this any more --
                             they poll for their target and build nothing
                             if it does not post (await_nbm_cycle.py) --
                             so it means a hand-run pipeline that fell
                             back through find_latest_gairmet_cycle().
                             Warned about, because publishing a rebuild
                             of the already-live package is never what
                             anyone wanted.
    1   error -- the manifest being published is missing or unparseable,
        which means the generate step produced something broken

WHERE THIS RUNS, AND WHY TWICE. It is called at TWO points in each
generate workflow:

  - as the PRE-FLIGHT, against what resolve_target_cycle.py worked out,
    before `pip install`, before the NBM poll, and before any data is
    fetched. A skip there ends the run in seconds. This is the single
    highest-value step in the workflow -- it is what makes both the
    +3:37 backstop and waiting for NBM affordable -- and it is why this
    script and everything it imports must stay stdlib-only.
  - again at publish time, against the manifest actually produced,
    because the pre-flight answer is up to an hour old by then and the
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
    BACKSTOP_OFFSET_MINUTES,
    TRIGGERS,
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


def describe_skip(new_manifest_path, hazard, now, trigger=None):
    """
    Which of the two skips this is, as (label, message, is_warning).

    Decided from the NBM cycle the run would build from (nbm_source_cycle
    in the pre-flight file or the manifest) against the one it should be
    chasing at this time -- not from the G-AIRMET cycle, which is the
    same in both cases and so cannot tell them apart.

    The weighting flipped with the move to a dispatched trigger. The
    routine case is now the +3:37 backstop finding that the Railway
    dispatch already published this cycle an hour ago -- that is the
    backstop working, not a problem. What is NOT routine is a run
    building from an older cycle than its target, which the workflows can
    no longer do.
    """
    found = parse_cycle(_read_field(new_manifest_path, "nbm_source_cycle"))
    if found is None or hazard is None:
        return "unknown", "Could not determine which NBM cycle this run used.", False

    target = target_nbm_cycle(now)
    if found.tzinfo is not None:
        found = found.replace(tzinfo=None)

    trigger_note = f" (trigger={trigger})" if trigger else ""
    if found >= target:
        backstop = f"+{BACKSTOP_OFFSET_MINUTES // 60}:{BACKSTOP_OFFSET_MINUTES % 60:02d}"
        routine = (
            f"That is exactly what the {backstop} backstop is for: it checks, finds the "
            f"Railway-dispatched run already did the work, and stops here having cost "
            f"seconds."
            if trigger == "schedule-backstop"
            else "Routine."
        )
        return (
            "already-published",
            f"The target NBM cycle ({target:%Y-%m-%d %H}Z) is the one this run is chasing "
            f"and its package is already on the branch{trigger_note}. {routine}",
            False,
        )

    return (
        "nbm-not-yet",
        f"This run would build from NBM {found:%Y-%m-%d %H}Z, which is OLDER than the "
        f"{target:%Y-%m-%d %H}Z cycle it should be chasing{trigger_note}, so it would "
        f"rebuild the package that is already live. The scheduled workflows cannot do this "
        f"-- they poll for their target and build nothing if it does not post -- so this is "
        f"a hand-run pipeline that fell back through find_latest_gairmet_cycle(). Nothing "
        f"is published either way; the warning is here because a rebuild of the live "
        f"package is never what anyone wanted.",
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
        "--trigger",
        choices=list(TRIGGERS),
        help="which trigger started this run, as classified by "
             "pipeline.publish_schedule.classify_trigger(). Used only to phrase the skip "
             "message -- a backstop finding the cycle already published is the backstop "
             "working, and should not read like the same event on a dispatched run",
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
        label, explanation, is_warning = describe_skip(args.new, args.hazard, now, args.trigger)
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
