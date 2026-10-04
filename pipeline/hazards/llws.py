"""
pipeline/hazards/llws.py
--------------------------
Low-level wind shear ("LLWS POTENTIAL"). Config for
pipeline.hazards.tango_common, which holds the actual logic.

FIELD. NBM core deterministic WIND at "surface - 610 m above ground"
(the 0-2,000 ft layer's top wind). It has no siblings in core, so the
level string alone isolates it. Do not confuse it with the 10 m wind:
see pipeline/hazards/sfc_wind.py.

A SPARSE EVENT FIELD. Measured on 2026-10-04 06Z f006: ~99.7% of cells are
zero and the nonzero floor is ~30 kt -- the field behaves like a flag
that carries a magnitude, not like a wind speed with shoulders. That is
why Phase A regrids "nearest" (a thin feature must not be blurred into
its zero neighbours, and "linear" would invent 15 kt values between a 0
and a 30), and why Phase B smooths the mask rather than the values (see
tango_common's docstring).

ALL DEFAULTS ARE UNCALIBRATED PLACEHOLDERS. 40 kt is a first guess above
the 30 kt floor; the range, sigma and the 1,000 sq mi minimum area have
not been checked with a forecaster.

The 25 nm radius (surface wind keeps 50) was chosen by eye from two
frames in one regime -- SW Arizona / New Mexico, 2026-10-04 -- as a
placeholder. At 50 nm the closing joined separate clusters into hulls
mostly empty of flagged cells (docs/METHODS.md 4A.9).
"""

from pipeline.hazards.tango_common import TangoLayer

LLWS = TangoLayer(
    key="llws",
    hazard="LLWS",
    phenomenon="LLWS POTENTIAL",
    idx_filters={"variable": ":WIND:", "level": "surface - 610 m above ground"},
    idx_exclude=(),
    regrid_method="nearest",
    default_threshold_kt=40.0,
    threshold_range_kt=(30.0, 60.0),
    default_neighborhood_radius_nm=25.0,
)
