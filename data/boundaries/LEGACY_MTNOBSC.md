# Legacy Mountain Obscuration boundaries — `legacy_mtnobsc.json`

The pre-automation MTN OBSC areas, built from VOR strings supplied by the
forecaster (see `scripts/build_legacy.py`, which generated the GeoJSON).

Three features: `Appalachians`, `Rockies`, and `CentralValleyCutout`.

## What this is for, and what it is not for

**Display only.** It is served at `/api/boundaries/legacy_mtnobsc`, drawn as
the `LEGACY MTN OBSC` layer in the viewer (off by default), and read by
nothing else. It is deliberately **not** wired into any gate, mask, or
filter, and it should stay that way.

It exists so a forecaster can put the derived areas and the legacy areas
side by side during calibration.

**Terrain-derived areas outside these boundaries are not assumed wrong.**
The legacy areas are broad-brush because the resolution to do better never
existed. Surfacing genuine relief that the legacy areas miss is a goal of
this tool, not a defect to be tuned away. If the overlay is ever used to
*restrict* output, that goal is lost — which is the reason for the previous
paragraph.

`CentralValleyCutout` is a **cutout of the Rockies area**, not a separate
hazard area. The viewer draws it dotted and dimmer than the other two and
labels it as a cutout; anything else consuming this file has to handle that
distinction itself.

## How much to trust the geometry

Three caveats, all of them from how the VOR strings were resolved. They
affect specific vertices, not the overall shape:

- **Offset bearings (`70NW_PQI` and the like) are computed as TRUE.** If
  NMAP treats them as magnetic, the fifteen offset vertices are displaced —
  roughly 20 nm at `70NW_PQI`, less where the offset is shorter. Vertices
  given as a plain identifier are unaffected either way, so a disagreement
  with NMAP that shows up only at offset vertices is this, and is fixable by
  re-running the build with magnetic variation applied.
- **`YSC` uses Sherbrooke airport coordinates.** It is absent from the open
  navaid table the build script reads, so the airport was substituted.
- **Eight identifiers collide with foreign navaids** — `CON`, `MLT`, `MSS`,
  `HAR`, `SHR`, `TBE`, `TOU`, `CME` — and were resolved **US-first**. That
  is right for a CONUS product, but it is an assumption rather than
  something the source string states.

## Regenerating

`scripts/build_legacy.py` writes this file from the VOR strings. Re-run it
if the strings change, if the offset bearings turn out to be magnetic, or
if a better navaid table becomes available for `YSC` and the eight
collisions above.
