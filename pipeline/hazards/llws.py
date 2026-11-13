"""
pipeline/hazards/llws.py
--------------------------
Low-level wind shear ("LLWS POTENTIAL"). Config for
pipeline.hazards.tango_common, which holds the actual logic.

WHAT THE FIELD IS. NBM core `WIND` at "surface - 610 m above ground"
(GRIB2 0/2/1, wind speed, m/s, PDT 0; first fixed surface 1 = ground,
second 103 = 610 m above ground). It is the SPEED part of NBM's TAF
low-level wind shear guidance, not a wind at 610 m and not a shear:

  - zero means NBM diagnosed no LLWS in that cell;
  - nonzero is the wind speed at the TOP of the layer where NBM found a
    wind-speed difference of >= 30 kt within 2,000 ft AGL.

NBM's own description (VLab, NBM Weather Elements): "Intended to provide
Low Level Wind Shear Speed guidance (up to 2,000 feet AGL) for WFO TAFs".
The algorithm (NBM v3.2 briefing, slides 19-20) drops any layer whose
wind-speed difference is under 30 kt and outputs "the wind speed found at
the top of that layer as the LLWS Speed". Sources and what is known,
inferred and unknown: docs/METHODS.md 4A.2.

IT HAS SIBLINGS. `WDIR` (0/2/0, the direction) and `DIST` (0/3/6, the
height of the layer top, m) sit at the same "surface - 610 m above
ground" level, and are nonzero in exactly the same cells (104,494 of
3,744,965 on NBM 2026-11-13 03Z f006 -- one cycle). Together they are a
TAF `WShhh/dddffKT` group. The idx filter keeps `:WIND:` in it, so it
still matches this one line; tests/test_tango.py pins that.

WHY THE DEFAULT IS 30 KT. Because the value is a top-of-layer speed and
the shear behind it is >= 30 kt, no nonzero value is under 30 kt
(smallest seen: 30.3 kt). A >= 30 kt threshold therefore selects exactly
the cells NBM flags. Anything higher filters on top-of-layer WIND, not on
shear strength: the old 40 kt default discarded about half of NBM's
flagged cells (49% native, 39% in our CONUS cache, 2026-11-12/13).

THE LIMIT. G-AIRMET LLWS is "air speed loss or gain of 20 kts or more
below 2000 ft AGL". NBM never flags shear between 20 and 30 kt, so an
NBM-based first guess is a conservative SUBSET of that criterion; no
threshold here can recover the 20-30 kt cases. NBM's difference is also
a scalar speed difference, not the vector difference the TAF directive
asks for.

A SPARSE EVENT FIELD, hence "nearest" in Phase A (a 0 next to a 30 must
not become a 15) and smoothing of the mask rather than the values in
Phase B (see tango_common's docstring).

UNCALIBRATED. Sigma 1.0, the 25 nm radius and the 300 sq mi minimum area
are placeholders (docs/METHODS.md 4A.8-4A.9). Measured with the 30 kt
default on five cycles: the 25 nm closing joins scattered clusters into
hulls that are often mostly unflagged; 15 nm roughly restores the old fill.
"""

from pipeline.hazards.tango_common import TangoLayer

LLWS = TangoLayer(
    key="llws",
    hazard="LLWS",
    phenomenon="LLWS POTENTIAL",
    idx_filters={"variable": ":WIND:", "level": "surface - 610 m above ground"},
    idx_exclude=(),
    regrid_method="nearest",
    default_threshold_kt=30.0,  # the field's floor: selects exactly NBM-flagged cells
    threshold_range_kt=(30.0, 60.0),
    default_smooth_sigma_cells=1.0,
    default_neighborhood_radius_nm=25.0,
    default_min_area_sq_mi=300.0,
)
