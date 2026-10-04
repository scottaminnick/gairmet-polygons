"""
pipeline/hazards/sfc_wind.py
------------------------------
Strong surface winds ("STG SFC WND", NWSI 10-811 section 7.1): sustained
surface wind of 30 knots or greater. Config for pipeline.hazards.tango_common,
which holds the actual logic.

FIELD. NBM core deterministic WIND at 10 m above ground. The idx has TWO
traps and the filter handles both:
  * ":10 m above ground:" -- WITH the colons. The bare substring
    "10 m above ground" also matches "surface - 610 m above ground", which
    is the LLWS field.
  * "ens std dev" -- WIND at 10 m has a spread sibling on the very next
    line; it is excluded.
tests/test_tango.py pins this to exactly one line of a saved real idx.

REGRID "linear". A 10 m wind field is smooth, and interpolating between
native cells is what a smooth field wants. (Slow: see
tango_common.prepare_tango_grid.)

ALL DEFAULTS ARE UNCALIBRATED PLACEHOLDERS. 30 kt is the directive's
number; the range, the smoothing sigma, the 50 nm radius and the
1,000 sq mi minimum area have not been checked with a forecaster.
"""

from pipeline.hazards.tango_common import TangoLayer

SFC_WIND = TangoLayer(
    key="sfc_wind",
    hazard="SFC_WIND",
    phenomenon="STG SFC WND",
    idx_filters={"variable": ":WIND:", "level": ":10 m above ground:"},
    idx_exclude=("std dev",),
    regrid_method="linear",
    default_threshold_kt=30.0,
    threshold_range_kt=(20.0, 50.0),
)
