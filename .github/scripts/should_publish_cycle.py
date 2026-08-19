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
    1   error -- the manifest being published is missing or unparseable,
        which means the generate step produced something broken

Deliberately a repo script rather than inline YAML in both workflows:
one copy of the comparison, and it can be unit-tested (see
tests/test_should_publish_cycle.py) instead of only ever being exercised
by a real scheduled production run.
"""
import argparse
import json
import sys
from datetime import datetime


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


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--new", required=True, help="manifest about to be published")
    parser.add_argument(
        "--existing",
        help="manifest currently on the data branch; may be absent or empty if the branch has none",
    )
    args = parser.parse_args(argv)

    new_cycle = _read_cycle(args.new)
    if new_cycle is None:
        print(f"ERROR: no usable model_cycle in the manifest being published ({args.new})", file=sys.stderr)
        return 1

    existing_cycle = _read_cycle(args.existing)
    if existing_cycle is None:
        print(f"No comparable cycle on the branch yet -- publishing {new_cycle.isoformat()}.")
        return 0

    if existing_cycle >= new_cycle:
        print(
            f"SKIPPING PUBLISH: the branch already holds {existing_cycle.isoformat()}, which is "
            f"newer than or equal to this run's {new_cycle.isoformat()}. Force-pushing would walk "
            f"the live site's data BACKWARDS. This is the expected outcome when an old workflow "
            f"run is re-run from the Actions tab; to publish a fresh cycle, trigger a NEW run."
        )
        return 10

    print(f"Branch holds {existing_cycle.isoformat()}; publishing newer {new_cycle.isoformat()}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
