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
  3. EXACTLY which ceiling probability thresholds does NBM publish?
     (Mountain Obscuration needs to interpolate a probability at an
     arbitrary terrain-driven height, e.g. "critical ceiling 2,750 ft
     AGL" -- so we need the real, complete set of published threshold
     heights, not an assumption that it's just the 500/1000/3000 ft
     flight-category boundaries.)
  4. Does NBM separately publish a "lowest cloud base, any coverage"
     field distinct from ceiling? (Ceiling by definition requires
     BKN/OVC coverage; a scattered layer sitting right at ridge height
     can still intermittently obscure a peak without ever qualifying
     as a "ceiling" at all -- this is the "OCNL OBSC" case in real
     AIRMET text, as opposed to continuous obscuration.)

This needs a real internet connection to NOAA's servers, so it can't be
run or verified inside a sandboxed dev environment -- run it via the
GitHub Actions workflow (or locally) and share the output back so the
real pipeline code (pipeline/hazards/ifr.py) can be written against
actual facts instead of guesses.

DESIGN NOTE: this deliberately does NOT use the `herbie-data` library.
All we need for this discovery step is the small, plain-text ".idx"
index file that sits alongside each NBM grib2 file (listing every
message's byte range and metadata) -- not the multi-GB grib2 itself, and
not grib2-parsing machinery (cfgrib/eccodes/xarray). A plain `requests`
GET is simpler, faster, has one dependency instead of five, and sidesteps
an internal timezone-comparison bug we hit in herbie-data's validation
step that we couldn't reproduce/isolate against several dependency
version combinations. herbie-data remains a good choice for the *real*
pipeline later, once we're actually parsing grib2 data rather than just
inspecting an index -- see pipeline/hazards/ifr.py (not yet written).

Usage:
    pip install requests
    python3 pipeline/inspect_nbm.py

If both URLs 404, the date below may be too recent (that cycle might
not be posted yet) or too old (rolled off the archive) -- try adjusting
RUN_DATE a few hours/days in either direction.
"""

import re
from datetime import datetime, timedelta, timezone

import requests

# --- Pick a cycle that should definitely be archived by now ---
# NBM CONUS cycles run every hour; we don't need the very latest one,
# just one guaranteed to exist. 2 days back, 12Z cycle, forecast hour 6
# roughly matches one of the real G-AIRMET valid times (0/3/6/9/12h).
RUN_DATE = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=2)).replace(
    hour=12, minute=0, second=0, microsecond=0
)
FORECAST_HOUR = 6

# Same two sources herbie-data tries, in the same priority order (nomads
# first -- it's the "official" real-time source; AWS as a fallback/
# archive mirror). URL pattern confirmed from herbie's own nbm.py model
# template source.
CANDIDATE_URLS = [
    (
        "nomads",
        f"https://nomads.ncep.noaa.gov/pub/data/nccf/com/blend/prod/"
        f"blend.{RUN_DATE:%Y%m%d}/{RUN_DATE:%H}/core/"
        f"blend.t{RUN_DATE:%H}z.core.f{FORECAST_HOUR:03d}.co.grib2.idx",
    ),
    (
        "aws",
        f"https://noaa-nbm-grib2-pds.s3.amazonaws.com/"
        f"blend.{RUN_DATE:%Y%m%d}/{RUN_DATE:%H}/core/"
        f"blend.t{RUN_DATE:%H}z.core.f{FORECAST_HOUR:03d}.co.grib2.idx",
    ),
]


def fetch_idx_text():
    """Try each candidate source in turn, return (source_name, raw_text) for the first that works."""
    for source_name, url in CANDIDATE_URLS:
        print(f"Trying {source_name}: {url}")
        try:
            resp = requests.get(url, timeout=30)
        except requests.RequestException as e:
            print(f"  request failed: {e}")
            continue
        if resp.status_code == 200:
            print(f"  success ({len(resp.text)} bytes)")
            return source_name, resp.text
        else:
            print(f"  HTTP {resp.status_code}")
    return None, None


def parse_idx(raw_text):
    """
    Parses wgrib2-style .idx lines into a list of dicts.

    Standard format (colon-separated):
        <message_num>:<start_byte>:d=<reference_time>:<variable>:<level>:<forecast_time>[:<extra>...]

    We keep whatever extra colon-separated fields exist beyond the
    standard six, since that's exactly where a probability field's
    threshold (e.g. "prob <1000 ft") is likely to show up as text --
    and we don't want to guess that away by assuming a fixed number of
    fields.
    """
    rows = []
    for line in raw_text.strip().splitlines():
        if not line.strip():
            continue
        parts = line.split(":")
        row = {
            "message_num": parts[0] if len(parts) > 0 else "",
            "start_byte": parts[1] if len(parts) > 1 else "",
            "reference_time": parts[2] if len(parts) > 2 else "",
            "variable": parts[3] if len(parts) > 3 else "",
            "level": parts[4] if len(parts) > 4 else "",
            "forecast_time": parts[5] if len(parts) > 5 else "",
            "extra": ":".join(parts[6:]) if len(parts) > 6 else "",
        }
        row["_raw_line"] = line
        rows.append(row)
    return rows


def show(label, rows):
    print("\n" + "=" * 70)
    print(f"{label}  ({len(rows)} matches)")
    print("=" * 70)
    if not rows:
        print("  (nothing matched)")
        return
    for r in rows:
        print(f"  [{r['message_num']}] {r['variable']:<10} {r['level']:<20} {r['forecast_time']:<20} {r['extra']}")


PROB_THRESHOLD_RE = re.compile(r"prob\s*([<>])\s*([\d.]+)", re.IGNORECASE)


def show_thresholds(label, rows):
    """
    Pulls the exact probability threshold values out of a set of rows
    (e.g. every CEILING row) and prints the sorted, de-duplicated set,
    converted from meters to feet -- this is the direct answer to
    "does NBM publish enough thresholds to interpolate a Mountain
    Obscuration probability at an arbitrary terrain-driven height, or
    just the three flight-category boundaries (500/1000/3000 ft)?"
    Meters-to-feet conversion matches the one already confirmed for
    IFR's real fields (e.g. "prob <304.8" = 1000 ft, see
    pipeline/hazards/ifr.py's docstring).
    """
    print("\n" + "=" * 70)
    print(label)
    print("=" * 70)
    found = set()
    for r in rows:
        m = PROB_THRESHOLD_RE.search(r["_raw_line"])
        if m:
            op, meters_str = m.groups()
            meters = float(meters_str)
            found.add((op, meters))
    if not found:
        print("  (no 'prob <NUM' / 'prob >NUM' pattern found in these rows -- check")
        print("  nbm_idx_raw.txt by hand, the encoding may not match IFR's fields)")
        return
    for op, meters in sorted(found, key=lambda t: t[1]):
        feet = meters / 0.3048
        print(f"  prob {op} {meters:>8.2f} m   (~{round(feet):>5} ft)")


def main():
    print(f"Looking for NBM CONUS 'core' .idx: {RUN_DATE:%Y-%m-%d %H}Z, F{FORECAST_HOUR:03d}\n")

    source_name, raw_text = fetch_idx_text()
    if raw_text is None:
        print("\nCould not fetch the .idx file from either source. This usually means the")
        print("RUN_DATE/FORECAST_HOUR combination isn't available (too recent or rolled off")
        print("the archive) -- try adjusting RUN_DATE.")
        return

    with open("nbm_idx_raw.txt", "w") as f:
        f.write(raw_text)
    print(f"Saved raw .idx text ({source_name}) to nbm_idx_raw.txt")

    rows = parse_idx(raw_text)
    print(f"Parsed {len(rows)} messages total.\n")

    with open("nbm_full_inventory.csv", "w") as f:
        f.write("message_num,start_byte,reference_time,variable,level,forecast_time,extra\n")
        for r in rows:
            f.write(
                f"{r['message_num']},{r['start_byte']},{r['reference_time']},"
                f"{r['variable']},{r['level']},{r['forecast_time']},\"{r['extra']}\"\n"
            )
    print("Saved parsed inventory to nbm_full_inventory.csv\n")

    def matches(keywords):
        return [r for r in rows if any(k.lower() in r["_raw_line"].lower() for k in keywords)]

    ceiling_rows = matches(["ceil"])
    show("Anything related to CEILING:", ceiling_rows)
    show_thresholds(
        "Distinct CEILING probability thresholds published "
        "(need this to know how finely we can interpolate a terrain-driven "
        "critical-ceiling probability for Mountain Obscuration):",
        ceiling_rows,
    )

    show("Anything related to VISIBILITY:", matches(["vis"]))
    show("Anything related to PROBABILITY:", matches(["prob", "%", "ppi"]))

    # Still the biggest open unknown for Mountain Obscuration: does NBM's
    # own file include a usable terrain/orography field, so we can skip
    # sourcing + reprojecting a separate external DEM entirely?
    show(
        "Anything related to TERRAIN/HEIGHT/LAND/OROGRAPHY (Mtn Obsc DEM question):",
        matches(["hgt", "land", "orog", "elev"]),
    )

    # Fractional sky cover (e.g. TCDC = total cloud cover, a 0-100%
    # coverage amount) is a DIFFERENT question from cloud BASE HEIGHT
    # (how high the lowest layer sits) -- kept as separate categories
    # below rather than one lumped "cloud" bucket, since Mountain
    # Obscuration cares about height, not coverage fraction.
    show(
        "Anything related to fractional SKY COVER (coverage %, not height):",
        matches(["sky", "tcdc"]),
    )
    # NOTE: previously searched "cld" only, which does NOT match the literal
    # word "cloud" as a substring (c-l-o-u-d has no contiguous "c-l-d") --
    # so a level field spelled out as "cloud base" rather than abbreviated
    # would have been silently missed. "cloud" added explicitly below.
    show(
        "Anything related to CLOUD BASE HEIGHT (lowest layer, any coverage -- "
        "distinct from CEILING, which by definition requires BKN/OVC; a "
        "scattered layer right at ridge height is the 'OCNL OBSC' case):",
        matches(["cld", "cloud", "cldbas"]),
    )

    print("\nDone. Please share back everything printed above, plus nbm_full_inventory.csv")
    print("and/or nbm_idx_raw.txt if possible (both are small plain text).")


if __name__ == "__main__":
    main()
