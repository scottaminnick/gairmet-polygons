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

from pipeline.hazards.ifr import generate_ifr_polygons

# Same naive-UTC-datetime requirement discussed in inspect_nbm.py applies
# here too, since fetch_nbm.py's URL formatting doesn't care either way,
# but keeping this consistent avoids re-introducing that whole class of
# bug if this code ever gets reused near Herbie again.
RUN_DATE = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=2)).replace(
    hour=12, minute=0, second=0, microsecond=0
)
FORECAST_HOUR = 6
THRESHOLD_PCT = 50.0


def main():
    print(f"Testing real IFR pipeline: {RUN_DATE:%Y-%m-%d %H}Z, F{FORECAST_HOUR:03d}, threshold={THRESHOLD_PCT}%\n")

    try:
        fc = generate_ifr_polygons(RUN_DATE, FORECAST_HOUR, threshold_pct=THRESHOLD_PCT)
    except Exception:
        print("FAILED. Full traceback:\n")
        traceback.print_exc()
        print("\nIf this is a fetch error, try adjusting RUN_DATE in this script (a few hours/days")
        print("earlier or later) -- the specific cycle/forecast-hour combo may not be available.")
        sys.exit(1)

    n_features = len(fc["features"])
    print(f"SUCCESS: got {n_features} IFR hazard polygon(s)\n")

    for i, feature in enumerate(fc["features"]):
        geom = feature["geometry"]
        props = feature["properties"]
        # Compute a rough bounding box just for a human-readable summary
        if geom["type"] == "Polygon":
            coords = geom["coordinates"][0]
        else:
            coords = [pt for ring in geom["coordinates"] for pt in ring[0]]
        lons = [c[0] for c in coords]
        lats = [c[1] for c in coords]
        print(f"  [{i}] {props['hazard']} >= {props['threshold_pct']}% "
              f"valid {props['valid_time']} "
              f"bounds=({min(lons):.2f},{min(lats):.2f})-({max(lons):.2f},{max(lats):.2f}) "
              f"vertices={len(coords)}")

    with open("test_ifr_live_output.geojson", "w") as f:
        json.dump(fc, f, indent=2)
    print("\nSaved full output to test_ifr_live_output.geojson")


if __name__ == "__main__":
    main()
