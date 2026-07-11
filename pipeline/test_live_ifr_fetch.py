"""
pipeline/test_live_ifr_fetch.py
--------------------------------
NOT part of the production pipeline -- like inspect_nbm.py, this is a
one-off smoke test. Its job is simply to call generate_ifr_polygons()
against a REAL NBM cycle and report what happens, since none of the
actual network fetch / cfgrib parsing could be tested in the sandboxed
dev environment this was built in.

Usage:
    pip install -r requirements.txt
    python3 pipeline/test_live_ifr_fetch.py

If it fails with a fetch/"Did not find" style error, RUN_DATE below may
need adjusting (too recent = not posted yet, too old = rolled off the
archive) -- same as inspect_nbm.py.
"""

import json
import sys
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from pipeline.hazards.ifr import CEILING_PROB_FILTER, VISIBILITY_PROB_FILTER, fetch_probability_grid
from pipeline.regrid import regrid_to_regular_latlon

RUN_DATE = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=2)).replace(
    hour=12, minute=0, second=0, microsecond=0
)
FORECAST_HOUR = 6
THRESHOLD_PCT = 50.0


def _fetch_and_regrid(filters):
    """Calls the exact same fetch_probability_grid() that production
    code (pipeline.hazards.ifr.generate_ifr_polygons) uses, so this
    diagnostic is guaranteed to exercise the real code path rather than
    a copy that could silently drift out of sync with it."""
    values, lats, lons = fetch_probability_grid(RUN_DATE, FORECAST_HOUR, filters)
    return regrid_to_regular_latlon(values, lats, lons)


def main():
    print(f"Testing real IFR pipeline: {RUN_DATE:%Y-%m-%d %H}Z, F{FORECAST_HOUR:03d}, threshold={THRESHOLD_PCT}%\n")

    try:
        print("Fetching + regridding ceiling probability...")
        ceil_regridded, grid_spec = _fetch_and_regrid(CEILING_PROB_FILTER)
        print("Fetching + regridding visibility probability...")
        vis_regridded, _ = _fetch_and_regrid(VISIBILITY_PROB_FILTER)
    except Exception:
        print("FAILED during fetch/regrid. Full traceback:\n")
        traceback.print_exc()
        print("\nIf this is a fetch error, try adjusting RUN_DATE in this script.")
        sys.exit(1)

    combined = np.maximum(np.nan_to_num(ceil_regridded), np.nan_to_num(vis_regridded))

    from pipeline.polygons import grid_to_polygons, polygons_to_feature_collection

    polygons = grid_to_polygons(combined, grid_spec, threshold=THRESHOLD_PCT)
    valid_time = RUN_DATE + timedelta(hours=FORECAST_HOUR)
    fc = polygons_to_feature_collection(
        polygons,
        properties={
            "hazard": "IFR",
            "threshold_pct": THRESHOLD_PCT,
            "valid_time": valid_time.isoformat() + "Z",
        },
    )

    n_features = len(fc["features"])
    print(f"\nSUCCESS: got {n_features} IFR hazard polygon(s)\n")

    for i, feature in enumerate(fc["features"]):
        geom = feature["geometry"]
        coords = geom["coordinates"][0] if geom["type"] == "Polygon" else [pt for ring in geom["coordinates"] for pt in ring[0]]
        lons = [c[0] for c in coords]
        lats = [c[1] for c in coords]
        print(f"  [{i}] bounds=({min(lons):.2f},{min(lats):.2f})-({max(lons):.2f},{max(lats):.2f}) "
              f"vertices={len(coords)}")

    with open("test_ifr_live_output.geojson", "w") as f:
        json.dump(fc, f, indent=2)
    print("\nSaved full output to test_ifr_live_output.geojson")

    # --- Diagnostic plot: the actual combined probability field + polygon
    # outlines, so a human can visually judge whether large regions are
    # real widespread conditions or an interpolation artifact. ---
    lons_axis = grid_spec.west + np.arange(combined.shape[1]) * grid_spec.dx
    lats_axis = grid_spec.north + np.arange(combined.shape[0]) * grid_spec.dy
    extent = [lons_axis[0], lons_axis[-1], lats_axis[-1], lats_axis[0]]

    fig, ax = plt.subplots(figsize=(14, 9))
    im = ax.imshow(combined, extent=extent, origin="upper", cmap="Blues", vmin=0, vmax=100)
    plt.colorbar(im, ax=ax, label="max(P(ceiling<1000ft), P(vis<3SM)) %", shrink=0.7)

    for feature in fc["features"]:
        geom = feature["geometry"]
        rings = [geom["coordinates"]] if geom["type"] == "Polygon" else geom["coordinates"]
        for poly_coords in rings:
            exterior = poly_coords[0]
            xs = [c[0] for c in exterior]
            ys = [c[1] for c in exterior]
            ax.plot(xs, ys, color="darkred", linewidth=1)

    ax.set_title(f"Real NBM IFR probability -- {RUN_DATE:%Y-%m-%d %HZ} F{FORECAST_HOUR:03d} "
                 f"(valid {valid_time:%Y-%m-%d %HZ}) -- {THRESHOLD_PCT}% threshold outlined")
    ax.set_xlabel("longitude")
    ax.set_ylabel("latitude")
    plt.tight_layout()
    plt.savefig("test_ifr_live_diagnostic.png", dpi=130)
    print("Saved diagnostic plot to test_ifr_live_diagnostic.png")


if __name__ == "__main__":
    main()
