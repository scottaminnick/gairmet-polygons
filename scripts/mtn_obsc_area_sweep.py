#!/usr/bin/env python3
"""
mtn_obsc_area_sweep.py -- what MTN OBSC output looks like at several
min_area_sq_mi values, so the number can be chosen from evidence rather
than inherited from IFR.

WHY THIS EXISTS: DEFAULT_MIN_AREA_SQ_MI is still 3,000 sq mi, which is
AIRMET/G-AIRMET's historical "widespread" criterion and IFR's default. It
was never chosen for this hazard, and it was set while the vector closing
was inflating terrain into blobs. Terrain-following areas are long and
thin -- a 100 x 10 nm ridge is about 1,300 sq mi -- so the filter now
removes real ridges. This script reports the trade so a forecaster can
pick; it deliberately does not recommend one.

WHAT IS REAL HERE AND WHAT IS NOT:
  - Terrain (ridge and baseline) is the REAL cached grid,
    data/terrain/terrain_grid.npz. Polygon shapes are therefore real
    terrain shapes, which is the thing the area filter acts on.
  - Ceiling probability is SYNTHETIC, in one of two modes. `uniform`
    (default) puts every published threshold at one value, so the
    polygons are exactly the mountainous mask and the sweep measures the
    terrain rather than one morning's weather. `varying` adds a smooth
    seeded field, which breaks the areas up the way real guidance does --
    that is where the small-polygon population the filter acts on
    actually appears. Run both; they answer different questions.
  - The cached grid predates the circular ridge footprint and the
    land-only baseline. Both shrink area slightly (the footprint stops
    reaching 1.41x diagonally, the baseline stops inflating coastal
    relief), so real post-regeneration counts will run a little lower
    than these.

Example:
    python scripts/mtn_obsc_area_sweep.py
    python scripts/mtn_obsc_area_sweep.py --areas 250 500 1000 --radius-nm 0
"""
import argparse
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
from shapely.geometry import shape as shapely_shape

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from pipeline.fetch_terrain import load_terrain_grid  # noqa: E402
from pipeline.hazards.mtn_obsc import (  # noqa: E402
    CEILING_PROB_THRESHOLDS_FT,
    DEFAULT_MIN_AREA_SQ_MI,
    polygonize_mtn_obsc_grid,
)
from pipeline.polygons import geodesic_area_sq_mi  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--terrain", default=str(REPO_ROOT / "data" / "terrain" / "terrain_grid.npz"))
    parser.add_argument("--areas", type=float, nargs="+", default=[500, 1000, 2000, 3000])
    parser.add_argument("--threshold-pct", type=float, default=50.0)
    parser.add_argument("--radius-nm", type=float, default=50.0)
    parser.add_argument("--clearance-ft", type=float, default=500.0)
    parser.add_argument("--probability-pct", type=float, default=90.0,
                        help="uniform mode: the synthetic ceiling probability at every threshold")
    parser.add_argument("--probability", choices=["uniform", "varying"], default="uniform",
                        help="uniform isolates the terrain; varying adds a smooth synthetic "
                             "probability field, which fragments the areas the way a real "
                             "morning does and is where the small-polygon population shows up")
    parser.add_argument("--seed", type=int, default=20260814)
    args = parser.parse_args()

    if not Path(args.terrain).exists():
        raise SystemExit(f"terrain grid not found: {args.terrain}")

    grids, grid_spec, terrain_radius_nm = load_terrain_grid(args.terrain)
    baseline = grids["baseline_elevation_ft"]
    ridge = grids["ridge_elevation_ft"]
    shape = baseline.shape

    print(f"terrain    : {args.terrain}")
    print(f"grid       : {shape} at {grid_spec.dx} deg, terrain_radius_nm={terrain_radius_nm}")
    if args.probability == "uniform":
        print(f"probability: synthetic uniform, {args.probability_pct:.0f}% at every published threshold")
    else:
        print(f"probability: synthetic varying field (seed {args.seed}) -- realistic structure, not a forecast")
    print(f"settings   : threshold {args.threshold_pct:.0f}%, radius {args.radius_nm:.0f} nm, "
          f"clearance {args.clearance_ft:.0f} ft")
    print()

    if args.probability == "uniform":
        ceiling_probs = {t: np.full(shape, args.probability_pct) for t in CEILING_PROB_THRESHOLDS_FT}
    else:
        # A smooth, seeded field standing in for one morning's guidance:
        # not a forecast, just realistic spatial structure, so the areas
        # break up into the size distribution the filter actually meets.
        from pipeline.smoothing import gaussian_smooth

        rng = np.random.default_rng(args.seed)
        field = gaussian_smooth(rng.random(shape), sigma_cells=40.0)
        z = (field - field.mean()) / field.std()
        base = np.clip(50.0 + 30.0 * z, 0, 100)
        # Higher thresholds are more likely by construction, as in real
        # NBM output (P(ceiling < 3000) >= P(ceiling < 500)).
        ceiling_probs = {
            t: np.clip(base * (0.55 + 0.45 * i / (len(CEILING_PROB_THRESHOLDS_FT) - 1)), 0, 100)
            for i, t in enumerate(CEILING_PROB_THRESHOLDS_FT)
        }
    zeros = np.zeros(shape, dtype=np.float32)

    # Polygonized once at the smallest area, then re-filtered: the filter
    # is the last step and applies per polygon, so running the expensive
    # part once and filtering the result gives identical answers to
    # running the whole thing per threshold.
    smallest = min(args.areas)
    fc = polygonize_mtn_obsc_grid(
        ceiling_probs, zeros, zeros, zeros, baseline, ridge, grid_spec,
        datetime(2026, 7, 16, 12), 0,
        threshold_pct=args.threshold_pct,
        clearance_margin_ft=args.clearance_ft,
        neighborhood_radius_nm=args.radius_nm,
        min_area_sq_mi=smallest,
    )
    areas = sorted(
        (geodesic_area_sq_mi(shapely_shape(f["geometry"])) for f in fc["features"]), reverse=True
    )

    header = f"{'min area':>10} {'polygons':>9} {'total sq mi':>13} {'median':>9} {'largest':>10} {'smallest':>10}"
    print(header)
    print("-" * len(header))
    for min_area in sorted(args.areas):
        kept = [a for a in areas if a >= min_area]
        marker = "  <- current default" if min_area == DEFAULT_MIN_AREA_SQ_MI else ""
        if not kept:
            print(f"{min_area:>10,.0f} {0:>9} {0:>13} {'-':>9} {'-':>10} {'-':>10}{marker}")
            continue
        print(
            f"{min_area:>10,.0f} {len(kept):>9} {sum(kept):>13,.0f} "
            f"{np.median(kept):>9,.0f} {max(kept):>10,.0f} {min(kept):>10,.0f}{marker}"
        )

    print()
    print(f"dropped between {min(args.areas):,.0f} and {max(args.areas):,.0f} sq mi: "
          f"{len([a for a in areas if min(args.areas) <= a < max(args.areas)])} polygons, "
          f"{sum(a for a in areas if min(args.areas) <= a < max(args.areas)):,.0f} sq mi")
    print("a 100 x 10 nm ridge is ~1,300 sq mi; a 60 x 8 nm one ~640 sq mi")


if __name__ == "__main__":
    main()
