"""
pipeline/generate_latest_ifr.py
---------------------------------
THE production driver. Run on a schedule (see
.github/workflows/generate_ifr.yml) to:

  1. Find the most recent NBM cycle that's actually been posted (NBM
     runs hourly, but there's always some posting lag -- we don't know
     in advance exactly how much, so we just try recent cycles until
     one works rather than guessing a fixed lag).
  2. Generate real IFR hazard polygons for a near-term forecast hour
     from that cycle.
  3. Overwrite output/ifr_latest.geojson with the result (deliberately
     NOT timestamped/accumulating -- the web app always serves
     whatever's currently there, and we don't want the repo to grow
     unboundedly with old forecasts).

Threshold is configurable via the IFR_THRESHOLD_PCT environment
variable (defaults to 50%), so a forecaster can re-run this manually
with a different cutoff without editing code -- see the workflow's
workflow_dispatch input.
"""

import json
import os
import sys
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.fetch_nbm import fetch_idx
from pipeline.hazards.ifr import generate_ifr_polygons

FORECAST_HOUR = 3  # near-term snapshot; see project notes on why not 0 (NBM doesn't provide fxx=0)
MAX_CYCLES_TO_TRY = 12  # NBM runs hourly; try up to this many hours back before giving up
THRESHOLD_PCT = float(os.environ.get("IFR_THRESHOLD_PCT", "50.0"))

OUTPUT_PATH = Path(__file__).resolve().parent.parent / "output" / "ifr_latest.geojson"


def find_latest_available_cycle(fxx: int) -> datetime:
    """
    Tries the most recent hourly NBM cycles, newest first, until one
    actually has data posted (checked via the cheap .idx fetch, not a
    full download). Raises if none of the recent MAX_CYCLES_TO_TRY
    hours are available at all.
    """
    now = datetime.now(timezone.utc).replace(tzinfo=None, minute=0, second=0, microsecond=0)
    for hours_back in range(MAX_CYCLES_TO_TRY):
        candidate = now - timedelta(hours=hours_back)
        try:
            fetch_idx(candidate, fxx)
            print(f"Found available cycle: {candidate:%Y-%m-%d %H}Z (F{fxx:03d})")
            return candidate
        except RuntimeError:
            print(f"  not yet available: {candidate:%Y-%m-%d %H}Z")
            continue
    raise RuntimeError(f"No NBM cycle in the last {MAX_CYCLES_TO_TRY} hours has F{fxx:03d} posted yet")


def main():
    print(f"Generating latest IFR polygons (F{FORECAST_HOUR:03d}, threshold={THRESHOLD_PCT}%)\n")

    try:
        cycle_date = find_latest_available_cycle(FORECAST_HOUR)
        fc = generate_ifr_polygons(cycle_date, FORECAST_HOUR, threshold_pct=THRESHOLD_PCT)
    except Exception:
        print("FAILED. Full traceback:\n")
        traceback.print_exc()
        sys.exit(1)

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, "w") as f:
        json.dump(fc, f, indent=2)

    print(f"\nSUCCESS: wrote {len(fc['features'])} polygon(s) to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
