"""
pipeline/inspect_nbm.py
------------------------
NOT part of the production pipeline. This is a one-time (or occasional)
DISCOVERY script -- its whole job is to answer questions we can't answer
from documentation alone:

  1. What do the ceiling/visibility probability fields actually look
     like in a real NBM file? (Probability fields in GRIB2 encode their
     threshold as metadata, not as part of a friendly field name, so we
     need to see real data to know what to filter for.)
  2. Does NBM's own file happen to include a terrain/land-mask field we
     could reuse for Mountain Obscuration (avoiding a separate DEM
     entirely)?

This needs a real internet connection to NOAA's servers, so it can't be
run or verified inside a sandboxed dev environment -- run it locally and
share the output back so the real pipeline code (pipeline/hazards/ifr.py)
can be written against actual facts instead of guesses.

Usage:
    pip install herbie-data
    python3 pipeline/inspect_nbm.py

If you get a "Did not find" error, the date below may be too recent
(that cycle might not be archived yet) or too old (rolled off the
archive) -- try adjusting RUN_DATE a few hours/days in either direction.
"""

from datetime import datetime, timedelta, timezone

import pandas as pd
from herbie import Herbie

# --- Pick a cycle that should definitely be archived by now ---
# NBM CONUS cycles run every hour; we don't need the very latest one,
# just one guaranteed to exist. 2 days back, 12Z cycle, forecast hour 6
# roughly matches one of the real G-AIRMET valid times (0/3/6/9/12h).
RUN_DATE = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=2)).replace(
    hour=12, minute=0, second=0, microsecond=0
)
FORECAST_HOUR = 6

pd.set_option("display.max_rows", 500)
pd.set_option("display.max_colwidth", 80)
pd.set_option("display.width", 200)


def show(label, subset):
    print("\n" + "=" * 70)
    print(label)
    print("=" * 70)
    print(subset if len(subset) else "  (nothing matched)")


def main():
    print(f"Looking for NBM CONUS 'core' file: {RUN_DATE:%Y-%m-%d %H}Z, F{FORECAST_HOUR:03d}")
    H = Herbie(RUN_DATE, model="nbm", product="co", fxx=FORECAST_HOUR)

    # .inventory() reads just the tiny .idx index file (not the full
    # multi-GB grib2), and returns every message's metadata as a
    # DataFrame -- this is the fast, cheap way to see what's inside.
    full_inventory = H.inventory()

    full_inventory.to_csv("nbm_full_inventory.csv", index=False)
    print(f"\nSaved full inventory ({len(full_inventory)} messages) to nbm_full_inventory.csv")
    print("(Share this file back if possible -- it's small text, and having the FULL list")
    print(" means we're not relying on the filters below happening to guess the right names.)")

    s = full_inventory["search_this"]

    show("Anything related to CEILING:", full_inventory[s.str.contains("CEIL", case=False, na=False)])
    show("Anything related to VISIBILITY:", full_inventory[s.str.contains("VIS", case=False, na=False)])
    show(
        "Anything related to PROBABILITY (prob, %, or similar):",
        full_inventory[s.str.contains("prob|%|PPI", case=False, na=False, regex=True)],
    )
    show(
        "Anything related to TERRAIN/HEIGHT/LAND (checking for a reusable terrain field):",
        full_inventory[s.str.contains("HGT|LAND|orog", case=False, na=False, regex=True)],
    )
    show(
        "Anything related to SKY COVER / CLOUD (for Mountain Obscuration later):",
        full_inventory[s.str.contains("CLD|SKY|TCDC", case=False, na=False, regex=True)],
    )

    print("\nDone. Please share back:")
    print("  1. Everything printed above")
    print("  2. Ideally, the nbm_full_inventory.csv file itself (it's plain text, a few hundred rows)")


if __name__ == "__main__":
    main()
