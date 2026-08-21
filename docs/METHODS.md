# Methods

How the polygons are actually produced, and why each step is the way it is.
The README covers running and deploying this; everything here is about the
algorithms and the decisions behind them.

Each section names the code that implements it, so this stays checkable
rather than becoming a parallel account that drifts.

---

## 1. Shape of the pipeline

Both hazards split into an **expensive, data-dependent phase** and a
**cheap, data-independent phase**:

| Phase | IFR | MTN OBSC | Needs |
|---|---|---|---|
| Prepare | `prepare_ifr_grid()` | `prepare_mtn_obsc_grid()` | NBM fetch, cfgrib/xarray, network |
| Polygonize | `polygonize_ifr_grid_active()` | `polygonize_mtn_obsc_grid()` | numpy/shapely/scipy only |

The split exists so the web app can re-run the second phase live against a
cached grid while a forecaster drags a slider, without re-fetching from NBM.
It is also why `pipeline/hazards/ifr.py` defers its `xarray` import into the
function that needs it: the deployed app imports the module for the cheap
phase and does not have the GRIB2 stack installed.

The three forecaster-adjustable parameters — `threshold_pct`,
`neighborhood_radius_nm`, `min_area_sq_mi` — are all applied in the cheap
phase, which is what makes them adjustable at all.

---

## 2. IFR

### 2.1 Fields and criteria

NWSI 10-811 defines IFR as *ceiling below 1,000 ft and/or visibility below
3 SM*. Four real NBM probability fields carry that:

| Meaning | NBM field |
|---|---|
| ceiling < 1,000 ft | `CEIL:cloud ceiling:…:prob <304.8` |
| visibility < 3 SM | `VIS:surface:…:prob <4828.03` |
| visibility < 1 SM | `VIS:surface:…:prob <1609.34` |
| measurable precip | `APCP:surface:…:prob >0.254`, recent 1-hour window |

The 1 SM field exists only to separate FG from BR; the precip field only to
separate PCPN from both. Neither is a hazard criterion on its own.

### 2.2 Two polygonizers

`USE_LABEL_GRID_POLYGONIZE` selects between them, and
`polygonize_ifr_grid_active()` is what production calls, so reverting is a
one-line change.

**v1, `polygonize_ifr_grid()`** — three cell-disjoint layers (CIG, PCPN,
visibility-non-precip), each closed by `merge_nearby_polygons()` and
contoured separately. It can emit overlapping and nested polygons, and the
cause is structural: the closing runs in vector space, per layer, so it
re-fills the holes the visibility mask punched in the ceiling layer. Both
layers then cover the same ground and are attributed identically, because
attribution runs on the final hole-filled shapes. Overlap count scales with
the radius slider.

**v2, `polygonize_ifr_grid_v2()`** — the current default. Every topology
decision moves into raster space and the output is contoured once:

1. **One class per cell.** Cause (`CIG`, `VIS`, `CIG/VIS`) crossed with
   primary weather (`PCPN`, `FG`, `BR`) gives seven classes plus 0 — see
   `IFR_CLASS_LABELS`. A cell has exactly one value, so two polygons can
   never claim the same ground.
2. **Close the envelope once.** The neighborhood radius is applied to the
   union of all hazard cells, not to each layer, so a closing cannot
   re-fill another layer's holes. Cells the closing adds inherit the class
   of the nearest original hazard cell.
3. **Connected components, then the area filter applied to whole
   components.** A component below `min_area_sq_mi` goes entirely. Nothing
   is dropped out of the middle of an area — v1's other failure mode.
4. **Absorption within each component.** Either one class covers at least
   `DOMINANT_FRACTION` (0.50) and the component is drawn whole, or it
   splits into class sub-regions which then absorb anything below
   `INCLUDE_FRACTION` (0.25) into its largest-perimeter-sharing neighbour,
   and anything enclosed by another region regardless of size.
5. **One contour per region**, traced from a copy of the region grid
   coarsened to `CONTOUR_RESOLUTION_DEG` (0.1°, ≈6 nm).

**Why enclosure absorption is mandatory, not cosmetic:** a G-AIRMET GFA
element is a simple ring and cannot carry a hole. A region wrapping another
would need an interior ring, which either fails to export or silently loses
the hole. After absorption no region encloses another, so `find_contours`
returns exactly one ring per region — and the code raises if it ever
returns more, rather than dropping the extra ring quietly.

The rule is implemented as "does this region have holes of its own"
(`binary_fill_holes` minus itself), which also catches a region wrapping
*several* others. A pocket enclosed **jointly** by two regions is not
absorbed and does not need to be: neither neighbour ends up with a hole, so
every region still contours to a single ring. Both cases are pinned in
`tests/test_ifr_label_grid.py`.

**Why nothing is smoothed or simplified afterwards:** adjacent regions
share their boundary exactly, because the 0.5 isoline between an A cell and
a B cell is the same geometric line from either side. Per-polygon smoothing
moves each polygon's copy of that shared edge independently, which is
precisely what would break it. `BOUNDARY_SMOOTHING_DEG` and
`FINAL_SIMPLIFY_TOLERANCE_DEG` are v1-only for this reason.
`ADJACENT_REGION_EROSION_DEG` exists as an escape hatch (shrink each region
by a hairline if some downstream tool ever rejects coincident vertices) and
is deliberately 0.

### 2.3 Labels

Cause: include `CIG` if cells whose class includes CIG cover at least
`INCLUDE_FRACTION` of the region, `VIS` likewise, both → `CIG/VIS`. If
neither clears the floor, whichever is larger wins, so every region gets a
cause.

Weather (only when `VIS` is in the cause): `PCPN` and `FG` each need
`INCLUDE_FRACTION`; **`BR` is included whenever visibility crosses anywhere
in the region**, with no fraction test. That last one is an interpretation
rather than a rule handed down — it follows AWC's convention that BR is a
catch-all appearing alongside more specific descriptors rather than being
replaced by them. It is one line in `_region_cause_and_weather()` if that
turns out to be wrong.

Fractions are measured against the region's **classes**, not the raw
probability grids: cells the closing added inherit a class but were never
above threshold, so a heavily-closed region measured against the raw grids
could report 0% of everything.

---

## 3. Area of responsibility (both hazards)

`data/boundaries/artcc.json` — AWC's 20 domestic ARTCC FIRs per NWSI 10-811
§3, "covering the conterminous U.S. and adjacent coastal waters."

NBM's CONUS grid has real coverage well past that (the Pacific, Canada, the
Atlantic), and the terrain grid's bounding box is deliberately generous.
IFR conditions and real relief out there are real data this product simply
isn't responsible for, so both hazards gate on this boundary. The mask is
memoized in `pipeline/boundaries.py`; the first request after a process
restart pays a few seconds to rasterize it, every later one is free.

**Applied twice in v2.** The fine-grid application guarantees no hazard
*cell* is outside the boundary, but a contour runs along coarse cell
*edges*, so a coarse cell straddling the line drew up to half a coarse cell
(~5 km) outside it. `_fully_inside_downsample()` keeps only coarse cells
whose every fine cell is inside, which pushes the quantization inward:
under-covering our own AOR is a forecaster edit, drawing outside it is a
product error.

**The residue, stated honestly.** Marching squares draws its isoline
halfway between cell *centres*, so at a staircase corner it cuts the corner
diagonally and a triangular sliver of the first excluded block can still
fall inside. Measured over the Great Lakes at 100 nm — the worst concavity
in CONUS — that is 0 outside cells covered, against 129 with the majority
rule alone. It was 1 before the coordinate-convention fix (§5). The residue
is a property of contouring a cell-centred raster at all, not of a closing
jumping the line, and it cannot grow with the radius.

---

## 4. Mountain Obscuration

### 4.1 The relational problem

IFR is a flat AGL threshold at each cell. Mountain obscuration is
relational: a 6,000 ft MSL cloud base is unremarkable over a 2,000 ft
valley and completely obscures a 9,000 ft ridge a few miles away. NBM's
ceiling fields are AGL relative to whatever it considers the surface at
that cell and cannot see a nearby ridge, so this needs terrain:

- `ridge_elevation_ft` — highest point within `TERRAIN_RADIUS_NM`, from a
  max filter, so it is already "the highest nearby peak", not this cell.
- `baseline_elevation_ft` — the ground here (§4.3).
- `critical_ceiling_agl = ridge − baseline + clearance_margin_ft`, then
  NBM's five published ceiling-probability thresholds are interpolated to
  that per-cell height. The interpolation refuses to extrapolate past
  6,600 ft rather than projecting a line with no data behind it.

### 4.2 The four gates

A cell participates only if all of these hold:

1. **Relief** ≥ `MOUNTAINOUS_RELIEF_THRESHOLD_FT` (500 ft, a placeholder
   pending comparison against the legacy shapefile).
2. **Land** — `data/boundaries/us_states.json`, a real coastline. This is
   the primary water exclusion and has to be polygon-based: smoothing
   bleeds coastal peak elevations into adjacent ocean cells, so those cells
   show high relief *and* positive baseline, defeating any elevation test.
3. **Deep-water backstop** — `baseline ≥ MIN_BASELINE_ELEVATION_FT`
   (−500 ft). Deliberately *not* sea level: that seems like the natural
   cutoff and silently dropped Death Valley (−255 ft floor, 10,065 ft of
   relief) and the Salton Sea shore.
4. **ARTCC** — §3.

### 4.3 Baseline is a land-only mean

`baseline_elevation_ft` is a block mean, and it used to include water
surface. Ocean and lake cells sit at or near 0 ft, so the mean dragged
every coastal and lakeside block toward the water surface — and since
relief is ridge minus baseline, that **inflates relief** in a band along
every coast and every large lake. For a block with land fraction *f* and
land at elevation *E*, the false relief is about (1 − *f*)·*E*: a
half-water block over 1,000 ft land invents ~500 ft, which is the entire
relief threshold.

Both averaging steps in `compute_output_grids()` now average over land
cells only. The land mask is evaluated on the **30 arcsec mosaic grid**,
which is the grid the averaging actually runs on — not the 0.025° output
grid, which is 3× coarser and could not tell which cells inside a block are
land.

A block with **no** land cells gets **NaN**, not a substituted value. Water
cannot be mountainous, and NaN fails the relief gate without special-casing
(`NaN >= threshold` is False). Substituting 0 ft would instead hand the gate
a plausible sea-level baseline under whatever ridge height the max filter
reached from the nearest shore — exactly the false relief this removes. The
stored format is int16, which has no NaN, so it is carried on disk as
`NO_LAND_BASELINE_SENTINEL` and converted back in `load_terrain_grid()`,
which is the only reader.

> **This takes effect only when the terrain grid is regenerated.** The
> committed `data/terrain/terrain_grid.npz` was produced by the old code.
> Re-run the `Fetch Terrain Grid` workflow to pick it up.

Measured on a synthetic North Shore profile with the real coastline (land
rising 600 → 1,150 ft over 20 km): coastal-band baseline 684 → 729 ft,
relief 433 → 389 ft, mountainous cells 796 → 378 on land and 13,502 → 497
on water. Reduced, not eliminated — which is the intent, since relief there
should be measured against local land rather than against the lake surface.

### 4.4 The neighborhood radius is applied to the raster

`merge_nearby_polygons()` closes in vector space, *after* every gate, so it
can push a polygon straight back over ground a gate removed. That is what
put MTN OBSC areas on Lake Superior in the 09Z run: the land mask excludes
the lake correctly, and the closing bridged across it anyway. Same failure
mode IFR v2 was rewritten to remove.

`USE_RASTER_CLOSING_MTNOBSC` (default on; flip for a one-line revert)
closes the raster instead and then re-applies every gate:

```python
closed &= on_land_mask
closed &= within_conus_mask
closed &= mountainous_mask
```

The mountainous re-mask matters as much as the water one: a closing that
reaches across a valley must not invent mountainous terrain where the
relief gate said there is none.

Measured over Lake Superior at 50 nm with both shores mountainous: open
water covered drops from **8,391 cells to 150**. Two ranges either side of
a ~69 nm flat valley stay two polygons instead of merging into one that
covers the valley.

**Consequence worth knowing:** the radius now smooths over *probability*
gaps within mountainous terrain only — it can no longer merge two ranges
across non-mountainous ground. Where probability is fairly uniform, the
slider does very little for MTN OBSC. That is the requested behaviour, but
it is a visible change: expect more, smaller polygons than 09Z produced.

---

## 5. The pixel/lon-lat convention

Two conventions differ by exactly half a cell:

- **corner-based** — `GridSpec.to_affine()`, GDAL/rasterio: integer = cell
  edge, cell *i* spans [*i*, *i*+1], centre at *i*+0.5.
- **centre-based** — numpy, `skimage.measure.find_contours`,
  `skimage.draw.polygon`: integer *i* **is** cell *i*.

`pipeline/polygons.py` mixed them in both directions: contours came out
half a cell northwest, rasterized masks sat half a cell southeast. They
cancelled wherever one fed the other, which is how it survived — ~1.8 km at
0.025° / 38.5N.

`to_affine()` keeps the corner convention, because a geotransform handed to
any standard tool has to mean what that tool thinks it means. The half-cell
now lives in `GridSpec.pixel_to_lonlat()` / `lonlat_to_pixel()`, which are
exact inverses by construction because both derive from the same affine.

**Verify against an external anchor, not an internal round trip.** This is
the part worth carrying forward. A round trip proves the two directions are
inverses; it cannot see a uniform offset, and the broken code round-tripped
perfectly while sitting half a cell off the ground. So did a
contour-then-rasterize check — the two errors cancelled exactly. What has
teeth is an anchor (index (0,0) is the centre of the top-left cell) plus
ground-truth checks (a contour landing on real cell edges, a lon/lat
rectangle rasterizing to exactly its cells). All three fail on the old
behaviour; the round trips do not. See `tests/test_polygons.py`.

---

## 6. Export

`pipeline/pgen_xml.py` writes NMAP2 PGEN product XML, byte-matched against
real NMAP2 exports.

**No vertex budget on the label-grid path.** There is no PGEN or NMAP2
vertex limit — the old 25-point budget was an invented constraint — and
Douglas-Peucker applied per ring is the mechanism by which two areas that
traced the same boundary can come apart into a gap or an overlap in the XML
a vendor receives. Vertex count on that path is governed upstream by
`CONTOUR_RESOLUTION_DEG` instead. v1 and MT_OBSC keep the budget: their
rings are already simplified and have no shared-boundary guarantee left to
protect.

**Disjointness is checked at the boundary.** `assert_rings_disjoint()` runs
over the rings about to be serialized, per hazard and forecast hour, and
raises. Rings sharing an *edge* pass — that is the normal adjacent case,
and two polygons meeting along a line intersect in zero area — while shared
interior area or containment fails. It is not enforced on v1 or MT_OBSC,
which overlap by construction; enforcing there would turn the revert into
an export that always raises.

The check exists because three separate times a vector operation
downstream of a sound polygonization has quietly undone it. An upstream
invariant that is not re-checked at the boundary is folklore.

---

## 7. Legacy MTN OBSC overlay

`data/boundaries/legacy_mtnobsc.json`, drawn as the `LEGACY MTN OBSC` layer
(off by default). **Display only** — it feeds no gate, mask, or filter, and
a test asserts nothing under `pipeline/` references it.

Terrain-derived areas outside the legacy boundaries are **not** assumed
wrong. The legacy areas are broad-brush because the resolution to do better
never existed; surfacing genuine relief they miss is a goal of this tool.

`data/boundaries/LEGACY_MTNOBSC.md` carries the caveats that decide how far
to trust the geometry: offset bearings computed as TRUE (≈20 nm of
displacement at `70NW_PQI` if NMAP treats them as magnetic), `YSC`
substituted with Sherbrooke airport, and eight identifiers that collide
with foreign navaids resolved US-first.

---

## 8. Known limits

- `MOUNTAINOUS_RELIEF_THRESHOLD_FT` and `TERRAIN_RADIUS_NM` are placeholders
  pending comparison against the legacy shapefile.
- The terrain search footprint is rectangular, not a true circle —
  over-generous at the corners, which is the safe direction for hazard
  detection.
- Flat valley floors whose terrain search radius reaches into surrounding
  mountains (California's Central Valley) are not addressed by any
  elevation cutoff; that is a `TERRAIN_RADIUS_NM` question.
- `cell_dimensions_km()` evaluates longitude spacing once at the domain's
  middle latitude, so the closing radius is 10–15% off at the north and
  south edges of a CONUS domain. Cell *areas* are computed per row, where
  the same error would accumulate.
- HZ, FU and BLSN are deliberately not automated for either hazard: no NBM
  field exists, and per AWC practice they are rare enough to add by hand.
