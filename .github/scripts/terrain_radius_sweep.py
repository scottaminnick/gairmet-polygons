#!/usr/bin/env python3
"""
.github/scripts/terrain_radius_sweep.py
----------------------------------------
Driver for the manual-only Terrain Radius Sweep workflow
(.github/workflows/terrain_radius_sweep.yml).

WHY A SEPARATE WORKFLOW, AND WHY THIS SHELLS OUT PER RADIUS.
TERRAIN_RADIUS_NM is baked into ridge_elevation_ft at FETCH time, not
applied live like IFR's neighborhood_radius_nm -- see
pipeline/fetch_terrain.py's module docstring. Comparing several radii by
dispatching .github/workflows/fetch_terrain.yml once per value would
overwrite the production grid on main (which the webapp reads live) once
per radius tried, trigger a Railway redeploy each time, and add a ~3.4 MB
binary commit to git history per radius. This workflow instead builds
each radius's grid into $RUNNER_TEMP and never touches data/terrain/ or
git at all.

pipeline/fetch_terrain.py reads MTN_OBSC_TERRAIN_RADIUS_NM into a
MODULE-LEVEL constant at import time (TERRAIN_RADIUS_NM), so calling its
main() function repeatedly in one long-lived Python process would use
whichever radius was read on the FIRST import for every subsequent call.
Running it as `python3 pipeline/fetch_terrain.py` in a fresh subprocess
per radius, exactly as the workflow_dispatch input says to, is what makes
each radius actually take effect -- confirmed, not assumed: this script
also checks the terrain_radius_nm the grid reports for itself against
what was requested, and fails loudly on a mismatch.

Also confirms scripts/mtn_obsc_area_sweep.py's --tsv output against a
known-good sweep run from 2026-09-11, at 12 nm -- report only, since 12
nm is one candidate among several this workflow exists to question, not
a fixed answer to enforce.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

TERRAIN_RADIUS_RE = re.compile(r"terrain_radius_nm=([0-9.]+)")

# The relief sweep run on 2026-09-11 against this same cached grid.
# "mi2" here is the MASK area ("mountainous" in the sweep script's own
# table -- computed after the land/CONUS/elevation gates and before any
# closing, gap-fill or area filter, so it does not depend on which of the
# filtered/unfiltered views below is compared) and "largest" is the
# UNFILTERED view's largest connected component. Confirmed against a real
# run of this same terrain grid at min-area-sq-mi=0: both figures matched
# to within a fraction of a square mile. They do NOT match the filtered
# (default min-area-sq-mi) largest, which the enclosed-gap fill that
# min-area filtering also drives can inflate past the raw component size
# -- that filtered figure is expected to diverge from this table, not a
# sign of a bug.
SEP_11_REFERENCE_12NM = {
    500.0: {"mask_sq_mi": 1_459_895, "unfiltered_largest_sq_mi": 1_138_753},
    1000.0: {"mask_sq_mi": 1_119_658, "unfiltered_largest_sq_mi": 957_473},
    1500.0: {"mask_sq_mi": 915_073, "unfiltered_largest_sq_mi": 830_523},
    2000.0: {"mask_sq_mi": 731_159, "unfiltered_largest_sq_mi": 660_926},
}
COMPARISON_RADIUS_NM = "12"

TSV_COLUMNS = [
    "relief_ft", "mountainous_sq_mi", "pct_conus", "pct_legacy",
    "unfiltered_components", "unfiltered_median_sq_mi", "unfiltered_largest_sq_mi",
    "unfiltered_largest_over_total",
    "filtered_components", "filtered_total_sq_mi", "filtered_median_sq_mi",
    "filtered_largest_sq_mi", "filtered_largest_over_total",
]

DISPLAY_HEADER = [
    "radius (nm)", "relief (ft)", "mountainous mi²", "% CONUS", "% legacy",
    "unfiltered comps", "unfiltered median mi²", "unfiltered largest mi²",
    "unfiltered largest/total",
    "filtered comps", "filtered total mi²", "filtered median mi²",
    "filtered largest mi²", "filtered largest/total",
]


def run_fetch_grids(radius_nm: str, output_path: Path) -> None:
    """
    A FRESH subprocess per radius -- see module docstring for why this
    can't be an in-process call to main().
    """
    env = {**os.environ, "MTN_OBSC_TERRAIN_RADIUS_NM": radius_nm}
    subprocess.run(
        [sys.executable, "pipeline/fetch_terrain.py", "--stage", "grids",
         "--output", str(output_path)],
        cwd=REPO_ROOT, env=env, check=True,
    )


def run_sweep(terrain_path: Path, relief_ft: list[str]) -> str:
    result = subprocess.run(
        [sys.executable, "scripts/mtn_obsc_area_sweep.py",
         "--terrain", str(terrain_path),
         "--sweep", "relief", "--relief-ft", *relief_ft,
         "--probability", "uniform", "--tsv"],
        cwd=REPO_ROOT, check=True, capture_output=True, text=True,
    )
    print(result.stdout)
    if result.stderr:
        print(result.stderr, file=sys.stderr)
    return result.stdout


def parse_terrain_radius(sweep_output: str) -> float:
    match = TERRAIN_RADIUS_RE.search(sweep_output)
    if not match:
        raise SystemExit(
            "sweep output never printed terrain_radius_nm=... in its header -- "
            "can't confirm MTN_OBSC_TERRAIN_RADIUS_NM took effect"
        )
    return float(match.group(1))


def parse_tsv_rows(sweep_output: str) -> list[list[str]]:
    rows = [
        line.split("\t")[1:]
        for line in sweep_output.splitlines()
        if line.startswith("TSV\t")
    ]
    if not rows:
        raise SystemExit(
            "sweep output carried no TSV rows -- did mtn_obsc_area_sweep.py's "
            "--tsv output format change?"
        )
    header, data_rows = rows[0], rows[1:]
    if header != TSV_COLUMNS:
        raise SystemExit(
            f"mtn_obsc_area_sweep.py's --tsv header changed: got {header}, "
            f"expected {TSV_COLUMNS}. Update TSV_COLUMNS (and the parsing "
            f"below) to match."
        )
    return data_rows


def markdown_table(header: list[str], rows: list[list[str]]) -> str:
    lines = ["| " + " | ".join(header) + " |",
             "| " + " | ".join(["---"] * len(header)) + " |"]
    lines += ["| " + " | ".join(row) + " |" for row in rows]
    return "\n".join(lines)


def format_display_row(radius_nm: str, fields: list[str]) -> list[str]:
    (relief, mask_sq_mi, pct_conus, pct_legacy,
     u_n, u_median, u_largest, u_ratio,
     f_n, f_total, f_median, f_largest, f_ratio) = fields
    return [
        radius_nm, f"{float(relief):,.0f}", f"{float(mask_sq_mi):,.0f}",
        f"{float(pct_conus):.1f}%", f"{float(pct_legacy):.0f}%",
        u_n, f"{float(u_median):,.0f}", f"{float(u_largest):,.0f}", f"{float(u_ratio):.3f}",
        f_n, f"{float(f_total):,.0f}", f"{float(f_median):,.0f}", f"{float(f_largest):,.0f}",
        f"{float(f_ratio):.3f}",
    ]


def compare_to_sep_11(radius_nm: str, fields: list[str]) -> str | None:
    if radius_nm != COMPARISON_RADIUS_NM:
        return None
    relief = float(fields[0])
    reference = SEP_11_REFERENCE_12NM.get(relief)
    if reference is None:
        return None
    mask_sq_mi = float(fields[1])
    unfiltered_largest = float(fields[6])
    mask_ok = abs(mask_sq_mi - reference["mask_sq_mi"]) < 1.0
    largest_ok = abs(unfiltered_largest - reference["unfiltered_largest_sq_mi"]) < 1.0
    status = "MATCH" if (mask_ok and largest_ok) else "MISMATCH"
    return (
        f"- relief {relief:,.0f} ft: **{status}** -- this run: mountainous "
        f"{mask_sq_mi:,.0f} mi², unfiltered largest {unfiltered_largest:,.0f} mi² "
        f"vs. Sep 11: {reference['mask_sq_mi']:,} mi², largest "
        f"{reference['unfiltered_largest_sq_mi']:,} mi²"
    )


def main() -> int:
    radii = os.environ["RADII_NM"].split()
    relief_ft = os.environ["RELIEF_FT"].split()
    runner_temp = Path(os.environ["RUNNER_TEMP"])
    output_dir = runner_temp / "sweep_output"
    output_dir.mkdir(parents=True, exist_ok=True)

    display_rows = []
    comparison_lines = []

    for radius in radii:
        terrain_path = runner_temp / f"terrain_r{radius}.npz"
        print(f"=== radius {radius} nm: fetching grids ===", flush=True)
        run_fetch_grids(radius, terrain_path)

        print(f"=== radius {radius} nm: relief sweep ===", flush=True)
        sweep_output = run_sweep(terrain_path, relief_ft)
        (output_dir / f"sweep_r{radius}.txt").write_text(sweep_output)

        actual_radius = parse_terrain_radius(sweep_output)
        if abs(actual_radius - float(radius)) > 1e-6:
            raise SystemExit(
                f"MISMATCH at radius {radius}: requested "
                f"MTN_OBSC_TERRAIN_RADIUS_NM={radius} but the grid's own header "
                f"reports terrain_radius_nm={actual_radius} -- the env var did "
                f"not take effect for this run"
            )
        print(f"confirmed: grid built at terrain_radius_nm={actual_radius}")

        for fields in parse_tsv_rows(sweep_output):
            display_rows.append(format_display_row(radius, fields))
            comparison = compare_to_sep_11(radius, fields)
            if comparison:
                comparison_lines.append(comparison)

    summary = ["# Terrain Radius Sweep", "", markdown_table(DISPLAY_HEADER, display_rows), ""]
    if comparison_lines:
        summary += [
            f"## {COMPARISON_RADIUS_NM} nm vs. the Sep 11 sweep", "",
            "Mask area and the *unfiltered* largest component -- see this script's "
            "module docstring for why those are the columns that match, not the "
            "filtered ones.", "",
        ] + comparison_lines
    else:
        summary += [
            f"## {COMPARISON_RADIUS_NM} nm vs. the Sep 11 sweep", "",
            f"{COMPARISON_RADIUS_NM} nm was not in radii_nm this run -- nothing to compare.",
        ]

    summary_text = "\n".join(summary) + "\n"
    print(summary_text)
    with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
        f.write(summary_text)

    return 0


if __name__ == "__main__":
    sys.exit(main())
