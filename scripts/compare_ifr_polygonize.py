#!/usr/bin/env python3
"""
compare_ifr_polygonize.py -- run BOTH IFR polygonize implementations over
the same cached probability grid and print the delta.

v1 is pipeline.hazards.ifr.polygonize_ifr_grid (three vector layers, each
closed by merge_nearby_polygons and contoured separately). v2 is
polygonize_ifr_grid_v2 (one raster partition, contoured once). See the
LABEL-GRID POLYGONIZATION comment block in pipeline/hazards/ifr.py for why
the rewrite exists; this script is how you see what it changed on real data
before flipping USE_LABEL_GRID_POLYGONIZE for forecasters.

Reports per radius, per implementation:
    polys       how many areas a forecaster would be handed
    area        total geodesic area (sq mi), holes and overlaps included once
    overlaps    pairs of polygons whose interiors intersect  <- must be 0
    nested      of those, pairs where one fully contains the other
    verts       largest ring's vertex count (NMAP editability)
    seconds     wall clock, excluding the one-off ARTCC rasterization

Example:
    python scripts/compare_ifr_polygonize.py output/ifr_f06_grid.npz
    python scripts/compare_ifr_polygonize.py            # synthetic fallback

With no path it builds a deterministic synthetic CONUS field instead, which
is fine for the structural counts (overlaps/nesting) but says nothing about
how a real forecast morning looks -- point it at a real
output/ifr_f{00,03,06,09,12}_grid.npz from a pipeline run for that.
"""
import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np
from shapely.geometry import shape as shapely_shape

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from pipeline.hazards.ifr import polygonize_ifr_grid, polygonize_ifr_grid_v2  # noqa: E402
from pipeline.polygons import GridSpec, geodesic_area_sq_mi, load_grid_cache  # noqa: E402
from pipeline.smoothing import gaussian_smooth  # noqa: E402

REQUIRED_KEYS = ("ceiling", "visibility_3sm", "visibility_1sm", "precipitation")


def synthetic_grids():
    """
    The same deterministic field tests/test_ifr_label_grid.py uses:
    smoothed noise scaled so ~5-10% of cells cross threshold, with
    visibility correlated with ceiling and vis<1SM a subset of vis<3SM.
    """
    spec = GridSpec(west=-125.0, north=50.0, dx=0.05, dy=-0.05)
    shape = (520, 1180)
    rng = np.random.default_rng(20260814)

    def field(sigma=18.0, offset=1.0):
        smoothed = gaussian_smooth(rng.random(shape), sigma_cells=sigma)
        z = (smoothed - smoothed.mean()) / smoothed.std()
        return np.clip(50.0 + 25.0 * (z - offset), 0, 100)

    ceiling, other, precip = field(), field(), field(offset=1.4)
    vis3 = np.clip(0.6 * ceiling + 0.4 * other, 0, 100)
    vis1 = np.clip(vis3 - 12.0, 0, 100)
    return (ceiling, vis3, vis1, precip), spec


def cached_grids(path):
    grids, spec = load_grid_cache(path)
    missing = [key for key in REQUIRED_KEYS if key not in grids]
    if missing:
        raise SystemExit(f"{path} is missing {missing} -- not an IFR grid cache?")
    return tuple(grids[key] for key in REQUIRED_KEYS), spec


def measure(feature_collection, seconds):
    geometries = [shapely_shape(f["geometry"]) for f in feature_collection["features"]]
    overlaps = nested = 0
    for i, a in enumerate(geometries):
        for b in geometries[i + 1:]:
            if a.intersection(b).area > 1e-9:
                overlaps += 1
                if a.contains(b) or b.contains(a):
                    nested += 1
    vertices = [
        len(g.exterior.coords) if g.geom_type == "Polygon"
        else max(len(p.exterior.coords) for p in g.geoms)
        for g in geometries
    ]
    return {
        "polys": len(geometries),
        "area": sum(geodesic_area_sq_mi(g) for g in geometries),
        "overlaps": overlaps,
        "nested": nested,
        "verts": max(vertices, default=0),
        "seconds": seconds,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("cache", nargs="?", help="path to an ifr_f??_grid.npz written by the pipeline")
    parser.add_argument("--threshold-pct", type=float, default=50.0)
    parser.add_argument("--min-area-sq-mi", type=float, default=3000.0)
    parser.add_argument(
        "--radius-nm", type=float, nargs="+", default=[0.0, 50.0, 100.0],
        help="neighborhood radii to compare (the overlap count scales with this on v1)",
    )
    args = parser.parse_args()

    if args.cache:
        grids, spec = cached_grids(args.cache)
        source = args.cache
    else:
        grids, spec = synthetic_grids()
        source = "synthetic field (no cache path given)"

    print(f"grid   : {source}")
    print(f"shape  : {grids[0].shape} at {spec.dx} deg, threshold {args.threshold_pct:.0f}%, "
          f"min area {args.min_area_sq_mi:.0f} sq mi")
    print()
    header = f"{'radius':>7} {'impl':>5} {'polys':>6} {'area sq mi':>12} {'overlaps':>9} {'nested':>7} {'verts':>6} {'seconds':>8}"
    print(header)
    print("-" * len(header))

    for radius_nm in args.radius_nm:
        for name, implementation in (("v1", polygonize_ifr_grid), ("v2", polygonize_ifr_grid_v2)):
            started = time.time()
            fc = implementation(
                *grids, spec, datetime(2026, 7, 14, 15), 0,
                threshold_pct=args.threshold_pct,
                neighborhood_radius_nm=radius_nm,
                min_area_sq_mi=args.min_area_sq_mi,
            )
            stats = measure(fc, time.time() - started)
            print(
                f"{radius_nm:>7.0f} {name:>5} {stats['polys']:>6} {stats['area']:>12,.0f} "
                f"{stats['overlaps']:>9} {stats['nested']:>7} {stats['verts']:>6} {stats['seconds']:>8.1f}"
            )
    print()
    print("overlaps must be 0 for downstream FAA vendors -- that is the whole point of v2.")


if __name__ == "__main__":
    main()
