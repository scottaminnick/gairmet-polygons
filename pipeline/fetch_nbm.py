"""
pipeline/fetch_nbm.py
-----------------------
Reusable utilities for fetching ONE specific NBM GRIB2 message (not the
whole multi-GB file) via an HTTP byte-range request, using its known
start byte from the .idx index.

Deliberately does not use herbie-data for the actual fetch (see
pipeline/inspect_nbm.py's design note for the internal timezone-
comparison bug we hit and couldn't resolve) -- this uses the same
nomads/AWS URLs and .idx format herbie-data itself relies on internally,
just via plain `requests` calls we have full visibility into.
"""

from __future__ import annotations

import tempfile
from datetime import datetime
from pathlib import Path

import requests

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


def fetch_idx(date: datetime, fxx: int) -> tuple[str, str]:
    """
    Fetches the plain-text .idx index for a given NBM cycle/forecast hour.

    Returns (raw_idx_text, grib2_url) -- grib2_url is whichever source
    (nomads or AWS) actually responded, so later byte-range requests hit
    the same source the index came from.
    """
    for grib_url in candidate_grib_urls(date, fxx):
        idx_url = grib_url + ".idx"
        try:
            resp = requests.get(idx_url, timeout=30)
        except requests.RequestException:
            continue
        if resp.status_code == 200:
            return resp.text, grib_url
    raise RuntimeError(f"Could not fetch .idx for date={date}, fxx={fxx} from nomads or AWS")


def parse_idx(raw_text: str) -> list[dict]:
    """
    Parses wgrib2-style .idx lines into a list of dicts.

    Standard format (colon-separated):
        <message_num>:<start_byte>:d=<reference_time>:<variable>:<level>:<forecast_time>[:<extra>...]

    Real NBM probability messages have extra fields beyond the standard
    six (e.g. "prob <304.8:prob fcst 3/7:probability forecast") -- we
    keep all of them joined in "extra" rather than assuming a fixed
    field count, confirmed necessary from real NBM data during
    development (see pipeline/inspect_nbm.py).
    """
    rows = []
    for line in raw_text.strip().splitlines():
        if not line.strip():
            continue
        parts = line.split(":")
        start_byte_str = parts[1] if len(parts) > 1 else ""
        row = {
            "message_num": parts[0] if len(parts) > 0 else "",
            "start_byte": int(start_byte_str) if start_byte_str.isdigit() else None,
            "reference_time": parts[2] if len(parts) > 2 else "",
            "variable": parts[3] if len(parts) > 3 else "",
            "level": parts[4] if len(parts) > 4 else "",
            "forecast_time": parts[5] if len(parts) > 5 else "",
            "extra": ":".join(parts[6:]) if len(parts) > 6 else "",
        }
        row["_raw_line"] = line
        rows.append(row)
    return rows


def find_message(rows: list[dict], **substring_filters: str) -> dict:
    """
    Finds exactly one row whose raw .idx line contains ALL given
    substrings (case-insensitive). Raises if zero or more than one
    match -- we never want to silently grab the wrong field because a
    filter was too loose or too strict.
    """
    matches = [
        r for r in rows
        if all(v.lower() in r["_raw_line"].lower() for v in substring_filters.values())
    ]
    if len(matches) == 0:
        raise ValueError(f"No message matched filters: {substring_filters}")
    if len(matches) > 1:
        lines = "; ".join(m["_raw_line"] for m in matches)
        raise ValueError(f"Ambiguous: {len(matches)} messages matched filters {substring_filters}: {lines}")
    return matches[0]


def fetch_message_bytes(grib_url: str, rows: list[dict], message: dict) -> bytes:
    """
    Fetches one message's raw bytes via an HTTP Range request. The end
    of the range is the NEXT message's start byte minus one (or end of
    file, for the last message in the index) -- GRIB2 messages are just
    concatenated back-to-back, so this always gets exactly one complete,
    self-contained message.
    """
    idx = rows.index(message)
    start = message["start_byte"]
    end = rows[idx + 1]["start_byte"] - 1 if idx + 1 < len(rows) else None
    range_header = f"bytes={start}-{end}" if end is not None else f"bytes={start}-"
    resp = requests.get(grib_url, headers={"Range": range_header}, timeout=120)
    resp.raise_for_status()
    return resp.content


def save_message_to_tempfile(data: bytes, suffix: str = ".grib2") -> Path:
    """Writes raw message bytes to a temp file, since cfgrib/eccodes need a file path, not raw bytes."""
    f = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    f.write(data)
    f.close()
    return Path(f.name)
