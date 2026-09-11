"""
pipeline/nbm_urls.py
-----------------------
Where a given NBM cycle/forecast hour lives, and nothing else.

STDLIB-ONLY, and that is the entire reason it exists as its own module.
These templates used to sit in pipeline/fetch_nbm.py, which imports
`requests` at module scope. The poller (.github/scripts/await_nbm_cycle.py)
runs BEFORE the generate workflows' `pip install` step -- that ordering
is what lets a run which has nothing to publish exit in seconds instead
of paying for a dependency install first -- so it cannot import
fetch_nbm, and copying two URL templates into it would have meant two
places to update when NOAA moves a path.

fetch_nbm re-exports everything here under its original names, so every
existing caller is unaffected.
"""

from __future__ import annotations

from datetime import datetime

NOMADS_URL_TMPL = (
    "https://nomads.ncep.noaa.gov/pub/data/nccf/com/blend/prod/"
    "blend.{date:%Y%m%d}/{date:%H}/core/blend.t{date:%H}z.core.f{fxx:03d}.co.grib2"
)
AWS_URL_TMPL = (
    "https://noaa-nbm-grib2-pds.s3.amazonaws.com/"
    "blend.{date:%Y%m%d}/{date:%H}/core/blend.t{date:%H}z.core.f{fxx:03d}.co.grib2"
)


def candidate_grib_urls(date: datetime, fxx: int) -> list[str]:
    return [NOMADS_URL_TMPL.format(date=date, fxx=fxx), AWS_URL_TMPL.format(date=date, fxx=fxx)]


def candidate_idx_urls(date: datetime, fxx: int) -> list[str]:
    """
    The .idx index URLs for the same message set.

    The index is what both the fetch path and the poller actually ask for
    first: it is a few KB of plain text, and its mere EXISTENCE is proof
    the cycle has posted, which is all the poller needs (it never
    downloads it -- see await_nbm_cycle.py).
    """
    return [url + ".idx" for url in candidate_grib_urls(date, fxx)]
