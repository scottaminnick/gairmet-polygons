"""
pipeline/generate_latest_mtn_obsc.py
--------------------------------------
THE production driver for Mountain Obscuration. Run on a schedule (see
.github/workflows/generate_mtn_obsc.yml) -- mirrors
pipeline/generate_latest_ifr.py's overall shape closely, including the
exact same cycle-finding/scheduling logic (now shared via
pipeline/gairmet_cycle.py, since none of that was ever IFR-specific).
Differs from IFR in three real ways, not just cosmetic ones:

  1. NINE real NBM fields fetched per snapshot instead of four (five
     ceiling-probability thresholds, deterministic ceiling, cloud base,
     precip, two visibility thresholds) -- see
     pipeline/hazards/mtn_obsc.py's docstring. A real, expected cost
     difference, not an inefficiency to fix.

  2. FOUR forecaster-adjustable parameters instead of three:
     threshold_pct, clearance_margin_ft (NEW -- one of
     pipeline.hazards.mtn_obsc.CLEARANCE_MARGIN_OPTIONS_FT),
     neighborhood_radius_nm, min_area_sq_mi. terrain_radius_nm is
     deliberately NOT a parameter here at all -- it's baked into the
     cached terrain grid at FETCH time (see
     pipeline/fetch_terrain.py's module docstring for why that one is
     fundamentally different in kind from the other four); this driver
     only reads whichever value is already baked into whatever terrain
     grid is on disk, purely for inclusion in the output manifest.

  3. The per-snapshot grid cache does NOT include baseline_elevation_ft
     / ridge_elevation_ft, unlike IFR caching all of its input grids.
     Those two are STATIC (identical every NBM cycle, every forecast
     hour -- terrain doesn't change), and already available via a
     separate, much cheaper pipeline.fetch_terrain.load_terrain_grid()
     call that needs no NBM access at all. Duplicating them into every
     6-hourly snapshot's cache would be pure waste. Just as important:
     pipeline.polygons.save_grid_cache() quantizes to uint8 assuming
     0-100 percentage values -- appropriate for the eight probability-
     range grids cached here, but would silently corrupt elevation data
     in feet (clipping everything above 255 ft) -- the exact same
     reasoning that led pipeline/fetch_terrain.py to need its own
     dedicated load_terrain_grid() rather than reusing load_grid_cache().
     A future web app live-recompute endpoint for this hazard would
     load the eight cached probability grids here PLUS a separate,
     already-cheap load_terrain_grid() call, then call
     polygonize_mtn_obsc_grid() fresh -- not built in this driver, but
     the cache format here is shaped to support it cleanly later.
"""

import json
import os
import sys
import traceback
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.gairmet_cycle import FORECAST_HOURS, NBM_LEAD_TIME_OFFSET_HOURS, find_latest_gairmet_cycle
from pipeline.hazards.mtn_obsc import (
    CEILING_PROB_THRESHOLDS_FT,
    DEFAULT_CLEARANCE_MARGIN_FT,
    polygonize_mtn_obsc_grid,
    prepare_mtn_obsc_grid,
)
from pipeline.polygons import save_grid_cache

THRESHOLD_PCT = float(os.environ.get("MTN_OBSC_THRESHOLD_PCT", "50.0"))
CLEARANCE_MARGIN_FT = float(os.environ.get("MTN_OBSC_CLEARANCE_MARGIN_FT", str(DEFAULT_CLEARANCE_MARGIN_FT)))
NEIGHBORHOOD_RADIUS_NM = float(os.environ.get("MTN_OBSC_NEIGHBORHOOD_RADIUS_NM", "50.0"))
MIN_AREA_SQ_MI = float(os.environ.get("MTN_OBSC_MIN_AREA_SQ_MI", "3000.0"))
TERRAIN_GRID_PATH = os.environ.get("MTN_OBSC_TERRAIN_GRID_PATH", "data/terrain/terrain_grid.npz")

OUTPUT_DIR = Path(__file__).resolve().parent.parent / "output"


def generate_one_snapshot(nbm_cycle_date, gairmet_cycle_date, requested_fxx: int):
    """
    Generates one forecast-hour snapshot. Same requested_fxx /
    actual_nbm_fxx relationship as IFR's version -- see
    pipeline/gairmet_cycle.py's docstring.

    Returns (feature_collection, prepared) -- prepared is
    prepare_mtn_obsc_grid()'s full return dict, kept around so main()
    can cache the probability grids (not the terrain grids -- see this
    module's docstring) and report terrain_radius_nm in the manifest.
    """
    actual_nbm_fxx = requested_fxx + NBM_LEAD_TIME_OFFSET_HOURS
    prepared = prepare_mtn_obsc_grid(nbm_cycle_date, actual_nbm_fxx, terrain_grid_path=TERRAIN_GRID_PATH)

    fc = polygonize_mtn_obsc_grid(
        prepared["ceiling_prob"],
        prepared["precip"],
        prepared["vis3"],
        prepared["vis1"],
        prepared["baseline_elevation_ft"],
        prepared["ridge_elevation_ft"],
        prepared["grid_spec"],
        gairmet_cycle_date,
        requested_fxx,
        threshold_pct=THRESHOLD_PCT,
        clearance_margin_ft=CLEARANCE_MARGIN_FT,
        neighborhood_radius_nm=NEIGHBORHOOD_RADIUS_NM,
        min_area_sq_mi=MIN_AREA_SQ_MI,
        terrain_radius_nm=prepared["terrain_radius_nm"],
    )
    return fc, prepared


def main():
    print(
        f"Generating Mountain Obscuration snapshot set: F{FORECAST_HOURS}, threshold={THRESHOLD_PCT}%, "
        f"clearance_margin={CLEARANCE_MARGIN_FT}ft, neighborhood_radius={NEIGHBORHOOD_RADIUS_NM}nm, "
        f"min_area={MIN_AREA_SQ_MI}sq mi\n"
    )

    try:
        nbm_cycle_date = find_latest_gairmet_cycle()
    except Exception:
        print("FAILED to find any available cycle. Full traceback:\n")
        traceback.print_exc()
        sys.exit(1)

    gairmet_cycle_date = nbm_cycle_date + timedelta(hours=NBM_LEAD_TIME_OFFSET_HOURS)
    print(
        f"Producing G-AIRMET cycle: {gairmet_cycle_date:%Y-%m-%d %H}Z "
        f"(from NBM's {nbm_cycle_date:%H}Z run)\n"
    )

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    manifest = {
        "model_cycle": gairmet_cycle_date.isoformat() + "Z",
        "nbm_source_cycle": nbm_cycle_date.isoformat() + "Z",
        "threshold_pct": THRESHOLD_PCT,
        "clearance_margin_ft": CLEARANCE_MARGIN_FT,
        "neighborhood_radius_nm": NEIGHBORHOOD_RADIUS_NM,
        "min_area_sq_mi": MIN_AREA_SQ_MI,
        "snapshots": [],
    }

    any_succeeded = False
    for requested_fxx in FORECAST_HOURS:
        actual_nbm_fxx = requested_fxx + NBM_LEAD_TIME_OFFSET_HOURS
        print(f"\n--- F{requested_fxx:02d} (NBM {nbm_cycle_date:%H}Z F{actual_nbm_fxx:03d}) ---")
        try:
            fc, prepared = generate_one_snapshot(nbm_cycle_date, gairmet_cycle_date, requested_fxx)
        except Exception:
            print(f"  FAILED for F{requested_fxx:02d}, skipping this snapshot. Traceback:")
            traceback.print_exc()
            continue

        filename = f"mtn_obsc_f{requested_fxx:02d}.geojson"
        with open(OUTPUT_DIR / filename, "w") as f:
            json.dump(fc, f, indent=2)

        # Only the eight probability-range (0-100) grids -- see module
        # docstring for why baseline/ridge elevation are deliberately
        # excluded here.
        cache_grids = {f"ceiling_prob_{t}": prepared["ceiling_prob"][t] for t in CEILING_PROB_THRESHOLDS_FT}
        cache_grids.update({"precip": prepared["precip"], "vis3": prepared["vis3"], "vis1": prepared["vis1"]})
        cache_filename = f"mtn_obsc_f{requested_fxx:02d}_grid.npz"
        save_grid_cache(OUTPUT_DIR / cache_filename, cache_grids, prepared["grid_spec"])

        valid_time = gairmet_cycle_date + timedelta(hours=requested_fxx)
        manifest["snapshots"].append(
            {
                "requested_forecast_hour": requested_fxx,
                "valid_time": valid_time.isoformat() + "Z",
                "filename": filename,
                "cache_filename": cache_filename,
                "feature_count": len(fc["features"]),
                "terrain_radius_nm": prepared["terrain_radius_nm"],
            }
        )
        print(f"  wrote {len(fc['features'])} polygon(s) to {filename} (valid {valid_time:%Y-%m-%d %HZ})")
        print(f"  cached prepared probability grids to {cache_filename}")
        any_succeeded = True

    if not any_succeeded:
        print("\nFAILED: every forecast hour failed for this cycle.")
        sys.exit(1)

    with open(OUTPUT_DIR / "mtn_obsc_manifest.json", "w") as f:
        json.dump(manifest, f, indent=2)
    print(f"\nSUCCESS: wrote manifest with {len(manifest['snapshots'])} snapshot(s) to mtn_obsc_manifest.json")


if __name__ == "__main__":
    main()
