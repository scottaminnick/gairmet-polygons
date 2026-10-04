"""
pipeline/generate_latest_tango.py
-----------------------------------
Production driver for the TANGO-category layers: surface wind
(pipeline.hazards.sfc_wind) and LLWS (pipeline.hazards.llws). Shaped like
pipeline/generate_latest_mtn_obsc.py, with these differences:

  1. TWO layers, one run, TWO manifests (sfc_wind_manifest.json and
     llws_manifest.json), published together to one data-tango branch.
     Each layer runs in its own try/except, so one failing never blocks
     the other. The run fails only if NEITHER layer produced anything.

  2. ONE NBM field per layer per hour (deterministic wind, core file) --
     no probabilities, no terrain.

  3. The per-hour cache holds ONE grid, the raw speed in knots, stored
     as knots x 2 in uint8 (save_grid_cache(scale=2)). Nothing is
     smoothed before it is cached, so every Phase B parameter can be
     re-run from it.

  4. Same source cycle as IFR / MTN OBSC (resolve_nbm_cycle), F00..F12
     mapping to NBM f006..f018 through NBM_LEAD_TIME_OFFSET_HOURS.

All forecaster parameters are env vars (TANGO_<LAYER>_*), empty or unset
meaning the layer's default. All defaults are uncalibrated placeholders.
"""

import json
import os
import sys
import time
import traceback
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.gairmet_cycle import FORECAST_HOURS, NBM_LEAD_TIME_OFFSET_HOURS, resolve_nbm_cycle
from pipeline.hazards.llws import LLWS
from pipeline.hazards.sfc_wind import SFC_WIND
from pipeline.hazards.tango_common import (
    CACHE_GRID_KEY,
    CACHE_SCALE,
    polygonize_tango_grid,
    prepare_tango_grid,
)
from pipeline.polygons import save_grid_cache

LAYERS = [SFC_WIND, LLWS]

OUTPUT_DIR = Path(__file__).resolve().parent.parent / "output"


def _env_float(layer, name, default):
    """TANGO_<LAYER>_<NAME>; empty as well as unset means the default."""
    raw = os.environ.get(f"TANGO_{layer.key.upper()}_{name}", "").strip()
    return float(raw) if raw else default


def layer_parameters(layer) -> dict:
    return {
        "speed_threshold_kt": _env_float(layer, "THRESHOLD_KT", layer.default_threshold_kt),
        "smooth_sigma_cells": _env_float(layer, "SMOOTH_SIGMA_CELLS", layer.default_smooth_sigma_cells),
        "neighborhood_radius_nm": _env_float(layer, "NEIGHBORHOOD_RADIUS_NM", layer.default_neighborhood_radius_nm),
        "min_area_sq_mi": _env_float(layer, "MIN_AREA_SQ_MI", layer.default_min_area_sq_mi),
    }


def generate_layer(layer, nbm_cycle_date, gairmet_cycle_date) -> int:
    """
    Runs every forecast hour for one layer and writes its manifest.
    Returns the number of snapshots that succeeded.
    """
    params = layer_parameters(layer)
    print(f"\n===== {layer.phenomenon} ({layer.key}) =====\nparameters: {params}", flush=True)

    manifest = {
        "category": layer.category,
        "phenomenon": layer.phenomenon,
        "model_cycle": gairmet_cycle_date.isoformat() + "Z",
        "nbm_source_cycle": nbm_cycle_date.isoformat() + "Z",
        **params,
        "snapshots": [],
    }

    for requested_fxx in FORECAST_HOURS:
        actual_nbm_fxx = requested_fxx + NBM_LEAD_TIME_OFFSET_HOURS
        print(f"\n--- {layer.key} F{requested_fxx:02d} (NBM {nbm_cycle_date:%H}Z F{actual_nbm_fxx:03d}) ---", flush=True)
        try:
            t0 = time.monotonic()
            speed_kt, grid_spec, idx_line = prepare_tango_grid(nbm_cycle_date, actual_nbm_fxx, layer)
            t1 = time.monotonic()
            print(f"  idx line: {idx_line}")
            print(f"  fetch+regrid ({layer.regrid_method}): {t1 - t0:.1f}s", flush=True)

            fc = polygonize_tango_grid(
                speed_kt, grid_spec, layer, gairmet_cycle_date, requested_fxx, **params
            )
            print(f"  polygonize: {time.monotonic() - t1:.1f}s", flush=True)
        except Exception:
            print(f"  FAILED for {layer.key} F{requested_fxx:02d}, skipping this snapshot. Traceback:")
            traceback.print_exc()
            continue

        filename = f"{layer.key}_f{requested_fxx:02d}.geojson"
        with open(OUTPUT_DIR / filename, "w") as f:
            json.dump(fc, f, indent=2)

        cache_filename = f"{layer.key}_f{requested_fxx:02d}_grid.npz"
        save_grid_cache(OUTPUT_DIR / cache_filename, {CACHE_GRID_KEY: speed_kt}, grid_spec, scale=CACHE_SCALE)

        valid_time = gairmet_cycle_date + timedelta(hours=requested_fxx)
        manifest["snapshots"].append(
            {
                "requested_forecast_hour": requested_fxx,
                "valid_time": valid_time.isoformat() + "Z",
                "filename": filename,
                "cache_filename": cache_filename,
                "feature_count": len(fc["features"]),
                "nbm_idx_line": idx_line,
            }
        )
        print(f"  wrote {len(fc['features'])} polygon(s) to {filename} (valid {valid_time:%Y-%m-%d %HZ})")
        print(f"  cached raw speed grid to {cache_filename}", flush=True)

    if not manifest["snapshots"]:
        print(f"\n{layer.key}: every forecast hour failed; no manifest written.")
        return 0

    with open(OUTPUT_DIR / f"{layer.key}_manifest.json", "w") as f:
        json.dump(manifest, f, indent=2)
    print(f"\n{layer.key}: wrote manifest with {len(manifest['snapshots'])} snapshot(s)")
    return len(manifest["snapshots"])


def main():
    try:
        # Tango rides MTN OBSC's cycle; the hazard name is only a log label.
        nbm_cycle_date = resolve_nbm_cycle(hazard="mtn_obsc")
    except Exception:
        print("FAILED to find any available cycle. Full traceback:\n")
        traceback.print_exc()
        sys.exit(1)

    gairmet_cycle_date = nbm_cycle_date + timedelta(hours=NBM_LEAD_TIME_OFFSET_HOURS)
    print(f"Producing G-AIRMET cycle: {gairmet_cycle_date:%Y-%m-%d %H}Z (from NBM's {nbm_cycle_date:%H}Z run)")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    succeeded = {}
    for layer in LAYERS:
        try:
            succeeded[layer.key] = generate_layer(layer, nbm_cycle_date, gairmet_cycle_date)
        except Exception:
            print(f"\n{layer.key}: FAILED outside the per-hour handler. Traceback:")
            traceback.print_exc()
            succeeded[layer.key] = 0

    print(f"\nsnapshots written per layer: {succeeded}")
    if not any(succeeded.values()):
        print("FAILED: neither layer produced anything.")
        sys.exit(1)
    for key, n in succeeded.items():
        if n < len(FORECAST_HOURS):
            print(f"WARNING: {key} wrote {n}/{len(FORECAST_HOURS)} snapshots")


if __name__ == "__main__":
    main()
