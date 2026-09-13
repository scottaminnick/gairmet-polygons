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
  max filter over a **circular** footprint (§4.5), so it is already "the
  highest nearby peak", not this cell.
- `baseline_elevation_ft` — the ground here (§4.3).
- `critical_ceiling_agl = ridge − baseline + clearance_margin_ft`, then
  NBM's five published ceiling-probability thresholds are interpolated to
  that per-cell height. The interpolation refuses to extrapolate past
  6,600 ft rather than projecting a line with no data behind it.

### 4.2 The four gates

A cell participates only if all of these hold:

1. **Relief** ≥ `mountainous_relief_ft`, defaulting to
   `MOUNTAINOUS_RELIEF_THRESHOLD_FT` (500 ft, still a placeholder — see
   §4.7, where it is now a forecaster-settable input rather than a fixed
   rule).
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
slider does very little for MTN OBSC. That is the requested behaviour
(confirmed with the forecaster: areas should follow terrain rather than
span valleys), but it is a visible change: expect more, smaller polygons
than 09Z produced. §4.5 and §4.6 are the two follow-ons that fall out of
it.

**Small enclosed valleys are filled back in.** The mountainous re-mask is
right at the scale of the Central Valley and wrong at the scale of the
Shenandoah: inside a mountain mass it punches out every valley floor under
the relief threshold, and the Appalachian polygon came out as Swiss cheese
— on the 13/03Z cycle, 39 holes carrying 234 vertices against a
159-vertex outline. No hand-drawn G-AIRMET shows that, none of it is
legible on aviationweather.gov, and a downstream converter crashed on the
point count. `fill_enclosed_gaps()` (pipeline/polygons.py) now runs after
the re-mask: connected regions of excluded cells that are completely
surrounded by hazard and smaller than **`min_area_sq_mi`** are set back
to hazard. Same number as the polygon filter, on purpose — a gap too
small to be a polygon is too small to be a gap — and no new control.
Gaps that reach the outside (bays in the outline) are never filled, so the
Central Valley test above still holds; gaps at or over the limit survive.

Measured on the 13/03Z cycle, all five hours: 47–316 enclosed gaps per
hour, median 2 sq mi, 90th percentile under 25; the fill adds 0.4–1.6 %
to the hazard area. The only gap over 3,000 sq mi in the whole cycle is
the Columbia Basin at F09 (12,700 sq mi), which is kept. Small inland
lakes inside a mountain mass are filled too, which is the right call for
an obscuration area. The mountainous-area figure (§4.7) deliberately does
**not** count filled ground — a filled valley is inside the polygon and
still not mountainous. `tests/test_mtn_obsc_gap_fill.py` pins the fill,
its ordering after the re-mask, and that figure.

Left open: the *outline* is still notched wherever a valley reaches the
edge of the mass, since those are bays rather than holes. If the vertex
count is still a problem after this, the next lever is
`FINAL_SIMPLIFY_TOLERANCE_DEG` for MTN OBSC (0.05° now); its polygons do
not share edges, so simplifying them harder is safe in a way IFR's
label-grid output is not.

### 4.5 The ridge search footprint is a circle

`maximum_filter(size=...)` is a rectangle, so the search reached √2 ≈
1.41× the nominal radius into its corners and 1.0× along the axes — a
17 nm reach at a nominal 12 nm. That was a deliberate v1 simplification,
over-generous in the safe direction for hazard detection, and it was
invisible while the closing was bridging across valleys. With bridging
suppressed it prints grid-aligned boxy edges directly onto
terrain-following polygons: one peak just past the radius on the diagonal
drags a square of cells up to ridge height.

`_disc_footprint()` builds a boolean disc that is a circle **on the
ground**, which is an ellipse in index space — a lon/lat cell is not
square, and the aspect ratio follows cos(latitude), so the footprint is
rebuilt per latitude band alongside the existing `_radius_deg()` call.

A disc is not separable, and scipy's footprint path visits every one of
its ~2,550 True cells per pixel. `_footprint_maximum_filter()`
decomposes it into rows instead — one horizontal running max per distinct
half-width, then a vertical max over the shifted results. Measured over
the mosaic: **~9 s, against ~2 min through scipy's footprint path and
~1 s for the rectangle**, all noise against the ~90 minute tile fetch.
The decomposition is checked against `maximum_filter(footprint=...)` at
six aspect ratios in `tests/test_fetch_terrain.py`.

> **Baked in at fetch time.** Like `TERRAIN_RADIUS_NM`, the footprint
> shape is applied when the terrain grid is built, so this only takes
> effect on the next `Fetch Terrain Grid` run.

### 4.6 The minimum-area filter is MTN OBSC's own

`DEFAULT_MIN_AREA_SQ_MI` is now a separate constant from IFR's default,
settable end to end (`MTN_OBSC_MIN_AREA_SQ_MI`, or the workflow input,
both falling back to that one constant) without touching IFR.

Its value is **still 3,000 sq mi and still a placeholder**, in the same
sense as `MOUNTAINOUS_RELIEF_THRESHOLD_FT`. 3,000 is AIRMET/G-AIRMET's
historical "widespread" criterion, adopted for IFR, and it was in place
while the vector closing was inflating terrain into blobs. Terrain-
following areas are long and thin — a 100 × 10 nm ridge is about 1,300
sq mi, a 60 × 8 nm one about 640 — so at 3,000 the filter removes real
ridges rather than noise.

`scripts/mtn_obsc_area_sweep.py` reports the trade against the real
cached terrain grid so the number can be chosen from evidence. Run it
both ways: `uniform` probability isolates the terrain, `varying` adds a
smooth synthetic field and shows the small-polygon population the filter
actually meets.

### 4.7 The relief threshold is forecaster-settable, and reported

The relief gate decides how much ground the hazard may claim *before any
weather is consulted*, so it sits upstream of clearance, radius and
min-area — all three act only on terrain it has already admitted. At 500
ft it admits river bluffs and the Ozark and Appalachian foothills, not
just terrain a pilot would call mountainous, which is why the western US
comes out as one million-plus-square-mile area under uniform probability.

It is applied **at recompute time**, once, in `polygonize_mtn_obsc_grid`,
against `ridge − baseline` from the cached grid. `fetch_terrain.py` never
reads it. That separation is what makes it adjustable live at all: the
cached grid stores ridge and baseline *elevations*, not a pre-computed
mountainous mask, so moving the threshold does not require re-fetching
terrain. (Contrast `TERRAIN_RADIUS_NM`, which *is* baked in at fetch time
and therefore cannot be a slider.) `tests/test_mtn_obsc_relief.py` pins
that, because a later change moving the gate upstream would break the
slider with no visible symptom — the control would still move and the map
would simply stop responding.

The viewer carries it as a RELIEF stepper (500–5,000 ft, 250 ft steps)
directly above CLEARANCE, with the same per-hour state and reset
semantics as every other parameter. Beside it is the **mountainous area at
the current threshold**, in square miles, measured after all four gates
and returned as a `mountainous_area_sq_mi` foreign member on the
FeatureCollection. It is a collection member rather than a feature
property because it describes the mask the polygons were cut from: at a
threshold high enough to leave no mountains there are no features to hang
it on, and that is exactly the reading worth showing.

Two reference points make the figure legible: the legacy broad-brush
MTN OBSC areas total about **1.20M sq mi**, and CONUS land is about
**3.12M**. Both appear next to the number in the panel.

`scripts/mtn_obsc_area_sweep.py --sweep relief` reports mask area,
polygon count, total area, median and largest across a range of
thresholds. **Choosing the value is a meteorological judgment and is
deliberately left open.**

One consequence worth knowing: no snapshot written before this change
carries `mountainous_relief_ft`, and the viewer re-seeds an hour's
controls from its snapshot's properties. `makeHourStore.fromProps` now
falls back to the control's *default* rather than its current value for an
absent property, so RESET restores 500 ft instead of silently leaving the
control where the forecaster put it.

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

Both conversions use `@` rather than `*` for the affine multiplication.
Same transform — `affine` raises a `PendingDeprecationWarning` on `*`, and
these two run per contour vertex, so `*` was emitting ~83,000 warnings per
test run and burying everything else in the log.

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

### Tags

The `tag` attribute is the thread linking F00/F03/F06/F09/F12 into one
time-evolving hazard. NMAP2 draws polygons sharing a tag as one feature
through time, and the BUFR smear — the swept area between consecutive
snapshots — is built by walking a tag from hour to hour. A tag scheme
that cannot say "this is the same area, three hours later" cannot produce
a smear at all.

`pipeline/tag_tracking.py` implements the five rules from the AWC
G-AIRMET snapshot/tagging training document:

1. **Continuation** — a polygon at hour N that intersects a polygon at
   hour N−1 inherits that polygon's tag.
2. **Ending** — a hazard ends by *absence*: no polygon carries its tag at
   the next hour. There is nothing to write for this; it falls out of
   rule 1.
3. **Split** — one parent, several children: all children take the
   parent's tag. Several polygons sharing a tag within one hour is legal.
4. **Merge** — several parents, one child: the child takes the **lowest**
   parent tag; the others end by absence.
5. **New** — no intersection with any previous-hour polygon: the next
   unused integer. Numbers belonging to ended tags are never reused.

`min()` over the intersecting tags implements rules 3 and 4 at once,
without either case being detected — a split is several children each
finding one parent, a merge is one child finding several. That matters
more than it sounds: the messy real cases are simultaneous splits and
merges, and a classifier would have to pick one label for them.

Settled decisions, deliberately not configurable:

- **"Intersects" is any contact, boundary touching included** —
  shapely's `.intersects`, not an area threshold. Two areas meeting along
  a shared edge intersect in zero area but are plainly one hazard
  continuing, and the label-grid polygonizer produces that contact
  routinely.
- **Each hazard layer is independent and starts at 1.** The IFR file and
  the MT_OBSC file do not share a counter.
- **Only the previous hour is consulted.** A feature absent for one
  snapshot returns with a new tag — per the doc, a three-hour gap is a
  new hazard, not a continuation.
- **Standard hours only** (0/3/6/9/12). No specials.
- **`tag` stays numeric.** The desk letter is already a separate `Gfa`
  attribute.

This replaced unique-sequential numbering, which gave every polygon its
own tag and so asserted that nothing was ever the same hazard twice. That
scheme was adopted after an August run produced heavy overlapping smears,
on the reasoning that a shared tag makes NMAP2 draw the polygons as one
evolving feature — **which is the intended behaviour, not the defect**.
What actually broke was the assigner: centroid-proximity matching, which
paired polygons nowhere near each other, on top of within-hour overlaps
that were a polygonization bug. Both are fixed — overlaps are now
rejected at the export boundary by `assert_rings_disjoint()` — so the
reason for avoiding reuse is gone, and avoiding it costs the snapshot
relationship the format exists to carry.

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

### 7.1 Regenerating it, and the identifier

`scripts/build_legacy.py` rebuilds the file from the VOR strings it
carries. It writes to `data/boundaries/legacy_mtnobsc.json` by default
(repo-relative) and takes the navaid table with `--navaids`; the table is
OurAirports' open dump, not vendored, with the download URL in the script
header. `tests/fixtures/navaids_subset.csv` is a verbatim subset of the
rows these three strings resolve, and
`tests/test_legacy_mtnobsc_overlay.py` asserts that regenerating from it
reproduces the committed file **byte for byte** — the script and the data
are two artifacts of one source, and nothing else keeps them in step.

The feature identifier is the `name` property. It was `area` in the
script's output while the viewer and the tests read `name`, which is worth
recording because of *how* it failed: in Python it was a loud `KeyError`,
but in the viewer it was silent. All three areas still drew; every tooltip
fell back to a plausible "legacy area"; and `CentralValleyCutout` lost the
dotted styling and the label that mark it as a hole in the Rockies area,
so it read as a third hazard area. Nothing on screen said otherwise.

The viewer now checks the loaded features and, if any lack `name`, shows a
warning in the layers panel naming the key it found instead, and logs the
same to the console. It deliberately does **not** disable the layer: the
geometry is still worth looking at, and a forecaster mid-calibration
should not lose the overlay over a property name. A missing *file* still
disables the toggle; that is a different condition.


## 8. Viewer panel structure

The right rail is `webapp/static/index.html` plus the accordion and export
wiring in `map.js`. It is written for **seven hazards, not two** — icing,
turbulence, LLWS, surface winds and freezing levels are planned — so the
shape matters more than it would for a two-panel rail.

```
LAYERS
  [x] IFR CIG/VIS        ▸     hazard row: expandable
        ADJUST                 ← steppers, APPLY / REVERT / APPLY ALL HOURS, resets
  [x] MTN OBSC           ▸
  [x] STATE BOUNDARIES         non-hazard row: no adjustors, no disclosure
  [ ] ARTCC BOUNDARIES
  [ ] LEGACY MTN OBSC
EXPORT
  hazards: [x] IFR  [x] MTN OBSC
  fcst hrs: F00..F12           (disabled — per-hour split not built)
  GENERATE GEOJSON + XML (THIS HOUR)
  DOWNLOAD PGEN (ALL HOURS)
```

**Adding a hazard** is a row in `index.html` and an entry in
`HAZARD_PANELS` (accordion) and `EXPORT_HAZARDS` (export panel) in
`map.js`. Nothing else in the rail is per-hazard.

Four rules the structure depends on:

- **The checkbox and the expander are siblings**, never nested. The
  checkbox is visibility only and the expander is expansion only, so a
  hazard can be visible and collapsed or hidden and expanded. Nesting an
  `<input>` inside the expander button would make one click do both, and
  is invalid HTML besides.
- **One row open at a time.** With seven hazards, several open adjustors
  would put the rail back to occupying a third of the screen, which is
  what this replaced. All collapsed on load.
- **Only hazard rows get a disclosure triangle**, so "this row has
  adjustors" is visible without clicking.
- **Exports are panel scope, resets are adjustor scope.** RESET THIS HOUR
  and RESET ALL HOURS undo what the controls above them did, so they stay
  in the row; GENERATE and PGEN act on the cycle, so they live once in
  EXPORT rather than repeated per hazard.

### Steppers and APPLY (replacing live sliders)

Each parameter is a **stepper** — a bounded number field with a `−` and
`+` button either side — rather than a slider. The parameters are
discrete (5 % probability, 250 ft relief, 500 ft clearance, 5 nm radius,
250 mi² area) and the rail is about 230 px wide, so a slider thumb moved
several steps per pixel and could not be set reproducibly. The field can
also be typed into; a typed value is snapped onto the field's
min/max/step lattice when editing finishes, so "47" becomes 45 and the
panel never shows a value the buttons could not have reached.

**Nothing recomputes on input.** Each parameter has two values: the
*applied* value, held in the per-hour store and matching the polygons on
the map, and the *pending* value in the field. While they differ the row
is marked (amber outline, `was N` under the label) and APPLY / REVERT
light up. APPLY copies pending into the store for the hour on screen and
fires **one** recompute for the whole parameter set; Enter in any field
does the same; REVERT copies applied back into the fields with no network
call. APPLY ALL HOURS writes the pending set against every hour in the
cycle and recomputes the one on screen — the others pick it up when
shown. The mountainous-area figure under RELIEF comes back with the
recompute, so it too updates on APPLY.

Two consequences worth knowing. GENERATE exports the **applied** set, not
the fields — a pending edit is not on the map, so it is not in a file
that claims to be the map. And each hazard's recompute carries a sequence
number, so if an APPLY is still in flight when the forecaster switches
hours, the slow response is discarded rather than painted over the new
hour (`recomputeCurrentSnapshot` / `recomputeCurrentMtnSnapshot`).

Wiring is one `ADJUSTORS` entry per hazard in `map.js` (store, buttons,
recompute function); the `−`/`+` buttons, Enter, dirty marking and all
three action buttons are driven off that list, so a new hazard's
adjustor is its markup plus one entry.

The hour checkboxes are present but **disabled**: per-hour file splitting
isn't built, GENERATE exports the hour on screen and PGEN covers all five,
exactly as they did before. A control that silently ignored its own state
would be worse than one that says it isn't ready.

The whole rail collapses to an icon strip. That state is **session-only
and lives in the DOM** — this environment has no `localStorage`, so a
reload comes back expanded, deliberately.

`tests/test_viewer_layout.py` pins all of this without a browser: chiefly
that every id `map.js` reaches for exists in the markup, which is the
contract a restructure breaks quietest.

---

## 8.5 Workflow dependency sets

Each workflow installs a different set, and a module-level import added
anywhere in `pipeline/` can silently widen what all of them need. This has
bitten twice, both times only at runtime in the one environment missing
the package:

- `fetch_terrain.py` imported `requests` at module level while
  `webapp/main.py` imports `load_terrain_grid()` from it — that endpoint
  500'd in production.
- The land-only baseline made `compute_output_grids` import
  `pipeline.boundaries` → `pipeline.polygons`, which imported `geojson` at
  module scope. Fetch Terrain Grid installs neither `geojson` nor
  `pyproj`, and the run died **after downloading all 1,708 tiles**.

`pipeline/polygons.py` therefore imports `geojson` and `pyproj` lazily,
inside the functions that wrap or measure finished polygons. Neither is
used by the boundary-mask path, which is the only part terrain reaches, so
keeping them lazy keeps the terrain job to `requirements-terrain.txt` —
`requests numpy scipy affine shapely scikit-image`. `shapely` and
`scikit-image` genuinely are needed there (unioning the coastline and
rasterizing it), and were the actual missing dependency once `geojson`
stopped being the first name to fail.

`tests/test_workflow_dependencies.py` walks each entry point's first-party
import graph — following imports inside function bodies, since that is how
`fetch_terrain` reaches `boundaries` — and asserts everything reached is in
the requirements file that workflow installs. It is static, so it cannot
see a dynamically constructed import; nothing here does that today.

**The test suite is an entry point too.** `tests.yml` runs the whole suite
under the light set (`requirements.txt pytest pyyaml`), deliberately, so CI
fails if a module-level heavy import creeps into a path the webapp touches
— which makes the suite subject to the same failure. That was not covered
until a test importing `NBM_LEAD_TIME_OFFSET_HOURS` from
`pipeline.gairmet_cycle` (→ `fetch_nbm` → `requests`) passed locally and
failed only in CI. Entry points are the files pytest actually collects;
`demo_visualize.py` is excluded with a reason, and a test fails if anything
in `tests/` is neither checked nor recorded as a non-test.

**Deferred vs. required.** The walker tracks whether a module is reachable
by module-scope imports the whole way down, or only through a function-level
hop that may never run. Two rules make this accurate:

- An *entry point's own* function-level imports are **not** deferred — a
  script's `main()` runs its calls, and a test body executes. This is why
  `fetch_terrain` → `boundaries` → `shapely` is required outright, which is
  exactly the chain the terrain job died on.
- A function-level hop *deeper in* is deferred, and only those can be
  excused by `DEFERRED_AND_UNUSED` — with a reason, and only while the
  import still exists. An allowance never covers a module the entry point
  imports directly.

Without that distinction a blanket "tests may reach `requests`" allowance
would have excused the very CI-only break this section exists for; it was
verified by reintroducing that import and confirming the guard fails.

## 8.6 The terrain mosaic is checkpointed

Assembling the intermediate mosaic means downloading 1,708 Skadi tiles and
is nearly all of the job's wall time; everything after is a few minutes of
array work. The workflow now runs `--stage mosaic`, saves the checkpoint,
then runs `--stage grids`, so a failure downstream of assembly costs
minutes rather than the whole fetch.

The save uses `actions/cache/save` with `if: always()` rather than
`actions/cache`, which only saves at the end of a *successful* job —
exactly the case that does not apply. The cache key is deliberately coarse:
correctness rests on the checkpoint storing the bounds and downsample
factor it was built from, which `load_mosaic_cache` checks, because a key
can be forgotten to bump and that comparison cannot. A mismatched,
unreadable, or absent checkpoint is a refetch, never a wrong answer.

## 8.7 Caching, and saying when the data is stale

Nothing the app served carried `Cache-Control`, so browsers applied their
own heuristic freshness (RFC 9111 §5.2) and cached it. Observed:
`raw.githubusercontent` had `model_cycle` 09Z while a normal window showed
03Z and a private window showed 09Z. The server was correct throughout,
which is what made it hard to see — a six-hour-old polygon set is
pixel-for-pixel as plausible as a current one.

One middleware in `webapp/main.py`, not a per-route decorator: there are a
dozen data routes now and seven hazards' worth later, and a decorator that
must be remembered is one that will eventually be forgotten — invisibly.

- **`/api/*` → `no-store, no-cache, must-revalidate, max-age=0`** (plus
  `Pragma`/`Expires` for HTTP/1.0 intermediaries). The data changes every
  six hours and a recompute is keyed entirely on its query string.
- **The front end (`/`, `.html`, `.js`, `.css`) → `no-cache,
  must-revalidate, max-age=0`.** Not `no-store`: `StaticFiles` already
  sends an `ETag`, so revalidation is a 304 with no body — verified, 46 KB
  of `map.js` becomes an empty 304. What it prevents is worse than stale
  data: last week's `map.js` against this morning's data, silently
  disagreeing about what a property is called.

Error responses carry it too, so a cached 503 cannot outlive the outage.
Leaflet and the fonts come from versioned CDN URLs and are left cacheable.

### The staleness indicator

Headers stop the browser causing this; they cannot fix a publish that
failed. `artifacts.refresh_hazard()` deliberately keeps the last good
cycle serving rather than blanking the site
(`test_failed_refresh_keeps_the_last_good_cycle`) — right behaviour, but
silent. So the panel now computes the cycle that *should* be published
from the clock and warns when the loaded one is older.

Cycles are 03/09/15/21Z. The expectation is derived from the publish
window rather than from a fixed grace period — see §8.8 for how that
window is now defined. The warning triggers only
at a **full cycle** behind: the comparison uses the browser's clock, and a
modest clock error should not be able to raise a false alarm. It names the
loaded cycle, the expected one, how far behind, and both remedies — a hard
reload for a cache, `/api/data/status` for a failed publish. A timer
re-checks, so a page left open across a boundary notices.

## 8.8 Publish schedule, polling, and NBM arrival

The crons originally fired at **+0:20** past each synoptic hour. At that
point the matching NBM run is not posted, so `find_latest_gairmet_cycle()`
fell back to the previous NBM cycle and, with the +6h lead offset, rebuilt
the G-AIRMET cycle that was **already current**. The 15Z first guess did
not appear until 15:20Z — thirty-five minutes *after* the 1445Z issuance
it exists to seed.

The fix for that was a ladder of four retry crons per hazard, +1:15 to
+3:00. That is gone too, for reasons §8.9 covers: the attempts were not
arriving when they were scheduled, and each one did a full fetch and
generation before discovering it had nothing to publish.

### Check before doing the work

The single highest-value change, and what makes everything else
affordable. Each generate workflow now, **before `pip install`, before
waiting for NBM, and before fetching any data**:

1. resolves which NBM cycle it is for and which package that would
   produce — pure arithmetic, no network
   (`.github/scripts/resolve_target_cycle.py`);
2. reads the cycle currently on its data branch;
3. asks `should_publish_cycle.py` whether that package is newer.

If it would not publish, the job exits successfully in seconds. That
ordering — publish check *before* the poll — is deliberate: the +3:37
backstop's normal outcome is "already published", and it must not spend
an hour waiting for a cycle nobody is going to use.

Everything those steps import is **stdlib-only**, which is what lets them
run above the install, and `tests/test_workflow_dependencies.py` fails if
a third-party import ever creeps into their reach or the steps get
reordered.

### Polling for the target cycle

Once the run knows it *would* publish, it waits for its NBM cycle:

- probe whether the cycle is posted — an HTTP `HEAD` on the `.idx`, so
  nothing is downloaded, not even the index;
- if not, sleep `POLL_INTERVAL_MINUTES` (5) and re-check;
- continue until found, or until `POLL_WINDOW_MINUTES` (60) from **job
  start**;
- on finding it, run the pipeline once and publish;
- on running out of window, log the `nbm-not-yet` WARNING and exit
  successfully, having built nothing.

The window is measured from job start rather than from the synoptic hour
because job start *is* dispatch time now (§8.9) — it is a statement about
how long we are willing to wait after asking, and the backstop gets the
same 60 minutes from its own later start.

**It does not fall back to an older cycle.** The old behaviour, when the
target had not posted, was to rebuild the package from the previous NBM
run — which is the package already live, so the publish guard skipped it
after a full fetch and generation had been paid for. A run now builds the
cycle it came for or builds nothing.

`timeout-minutes` is derived from the schedule
(`job_timeout_minutes(hazard)` = poll window + work allowance = 90 for
IFR, 105 for MTN OBSC), so a wedged poll cannot sit on GitHub's
360-minute default and retuning the window moves the timeout with it.

**The two hazards are no longer staggered**, and that is a finding rather
than a simplification. The 15-minute gap existed to keep their pushes and
NBM fetches from overlapping. Checked directly: they force-push separate
orphan branches with disjoint file globs, their `concurrency:` groups are
separate, each job runs on its own runner with its own IP (so there is no
shared rate-limit bucket at NOAA), and only MTN OBSC reads the terrain
grid. The decisive point is that the stagger never prevented overlap
anyway — MTN OBSC runs 30+ minutes against IFR's 15–20, so under the old
+1:15/+1:30 crons the two were fetching from NOAA simultaneously on most
cycles. `scripts/dispatch_workflows.py` keeps a `--stagger-seconds` knob
at 0 in case that stops being true.

`NBM_LEAD_TIME_OFFSET_HOURS` stays at **6** — the forecaster has accepted
~4.5 hours of lead for fresher guidance.

The schedule lives in `pipeline/publish_schedule.py` (stdlib-only, so the
poller, the publish guard and the Railway dispatcher can all share it
without the fetch stack). The backstop cron, the Railway cron in
`railway.dispatch.json`, the job timeouts and the browser's staleness
constants are all generated or derived from it;
`tests/test_publish_schedule.py` fails if any of them drift.

The generate step is told which cycle to build via `NBM_SOURCE_CYCLE`
rather than re-deriving one: up to an hour passes between the pre-flight
and the fetch, and a newer cycle posting in between would produce a
package no guard had approved.

### Two skips that used to look the same

"Branch holds X; not publishing" distinguishes:

- **`already-published`** — the run's target cycle is one whose package is
  already on the branch. This is now the *routine* outcome for the
  backstop, and the message says so, because a log where the common case
  looks alarming is a log where the alarming case gets missed.
- **`nbm-not-yet`** — the run would build from an **older** NBM cycle than
  the one it is chasing. The workflows cannot produce this any more, so it
  means a hand-run pipeline fell back through
  `find_latest_gairmet_cycle()`. Warned about: nothing publishes either
  way, but a rebuild of the live package is never what anyone wanted.

The separate case of "the cycle never posted" is now the poller's, not the
guard's — it is the `nbm-not-yet` WARNING on stderr from
`await_nbm_cycle.py`, and it names the backstop that will try again.

### NBM arrival instrumentation

Every run emits one greppable line. The old `attempt=N/4` label described
a retry ladder that no longer exists; what replaced it answers the two
questions that actually matter, separately:

```
NBM-ARRIVAL hazard=ifr trigger=railway-cron target=...03:00:00Z \
            package=...09:00:00Z posted=yes job_start=...03:46:00Z \
            start_delay_min=1 detected=...04:18:00Z poll_wait_min=32 \
            arrival_min=78 arrival_measured=yes
```

- `trigger` — `railway-cron`, `schedule-backstop` or `manual`. If
  `schedule-backstop` lines start appearing regularly, the Railway service
  is broken and the pipeline is running on its safety net.
- `start_delay_min` — job start minus that trigger's nominal time. **The
  number that says whether §8.9 worked.** It was ~95 minutes on
  2026-09-07 and up to 4.5 hours by 2026-09-10; on a `workflow_dispatch`
  it should be ~0. `-` for a manual run, which has no schedule to be late
  against — a zero there would read as "started on time".
- `poll_wait_min` — how long we then waited for NBM.
- `arrival_min` — when the cycle appeared, measured from its own synoptic
  hour. NBM's latency and nothing else.
- `arrival_measured` — whether `arrival_min` is a real measurement or only
  an upper bound. It is a measurement when the poll actually *watched* the
  cycle appear. Found on the first probe, all we know is "at or before
  then" — and conflating those two is exactly how a platform delay came to
  look like an NBM delay.

### What this does to the staleness indicator

The app **normally holds a cycle ahead of the wall clock**: at 10:15Z it
serves the 15Z package, 4h45m before 15Z. Comparing the loaded cycle to
"now" would call every healthy state stale.

Stale means: the loaded cycle is older than the newest one that should
have *published*, following the normal path end to end —

| | |
|---|---|
| +0:45 | Railway POSTs `workflow_dispatch` to both hazards |
| +1:45 | the 60-minute poll for the NBM cycle closes |
| +2:30 | 45 more minutes to install, generate and push |

so `PUBLISH_WINDOW_CLOSE_MINUTES` is 45 + 60 + 45 = 150.

**Deliberately not the +3:37 backstop.** That backstop could still publish
the cycle at ~+4:22, and waiting for it before saying anything would mean
hiding a real failure for nearly two hours to avoid one honest warning.
Past +2:30 the package genuinely *is* late; the backstop is a recovery,
not extra runway.

`expected_gairmet_cycle()` is the Python mirror of the browser's
`expectedGairmetCycle()`, exercised against frozen clocks either side of
the boundary and across midnight. The threshold is still a full cycle
behind, so browser clock skew cannot raise a false alarm.

## 8.9 Why the trigger lives on Railway

### What was measured

GitHub delays `schedule:` runs, and on this repository the delay is
**growing**:

| Date | Observed |
|---|---|
| 2026-09-07 | the 03Z package's four IFR attempts were scheduled at 22:15 / 22:45 / 23:15 / 23:45Z and **started** at 23:54 / 00:22 / 01:01 / 01:18Z — ~95 minutes late, every one |
| 2026-09-10 | up to **4.5 hours** late |

The consistency is the tell. All four attempts were delayed by nearly the
same amount, which is not what a busy runner pool looks like — it is what
a queue with a fixed backlog looks like.

### Why scheduled events cannot be relied on at this granularity

GitHub documents `schedule:` as best-effort and says runs may be delayed
during periods of high load, with scheduled events queued at the lowest
priority. That is fine for a nightly cleanup and useless for a product
with a deadline: the 03Z package exists to seed the **0245Z** issuance,
and a package that was designed to land ~4 hours ahead of it instead
arrived 2h45m ahead (Sep 7) and, at 4.5 hours of delay, would arrive
*after* the issuance it exists to seed.

No cron expression fixes this, because the expression is not what is
late. Moving the time earlier just moves the queue entry earlier; the
backlog is still in front of it.

Worse, we were contributing to it. The four-attempt retry ladder
quadrupled the number of scheduled runs this repository was putting into
that queue — eight per cycle across both hazards, thirty-two a day —
which very plausibly made our own delays worse. The instinct to add
scheduled attempts when scheduled runs are late is exactly backwards.

`workflow_dispatch` is not queued that way and starts promptly. So the
trigger moved off the platform's scheduler entirely.

### What runs instead

A **Railway cron service** in the same project as the web app runs
`scripts/dispatch_workflows.py` at `45 3,9,15,21 * * *`. It POSTs to
`/repos/{owner}/{repo}/actions/workflows/{file}/dispatches` for both
hazards with `ref: main` and an input `trigger: railway-cron`, so the run
can identify itself in the logs. It exits non-zero on any non-204 so
Railway marks the job failed rather than the pipeline silently going
quiet.

Deliberately stdlib-only (`urllib`, not `requests`): it runs in a minimal
container whose whole job is two HTTP POSTs, and making the one thing
that must not fail install the web app's dependency stack would give it a
large surface to fail on.

Setup is four fields in the Railway UI — config file, `GITHUB_TOKEN`,
cron schedule, start command — documented in
[`RAILWAY_DISPATCH.md`](../RAILWAY_DISPATCH.md) and in the script's own
header, which is where somebody debugging it at 3am will actually look.
The token is a fine-grained PAT scoped to this repository with a single
permission, `Actions: Read and write`; nothing here needs contents write.

### What the backstop is for

Moving the trigger off GitHub means a new single point of failure: if
Railway is down, the token expires, or the dispatch is simply never sent,
nothing happens at all. So each workflow keeps **one** `schedule:` entry,
at +3:37, and that is its entire purpose.

It is cheap precisely because of check-before-work. On a normal day the
Railway dispatch published the package around +1:30, so the backstop
resolves its cycle, reads the branch, sees `[already-published]` and exits
in seconds having installed nothing. On a bad day it publishes the package
late, which beats not publishing it.

Three details matter:

- **One entry, not a ladder.** Adding scheduled runs to work around
  scheduled-run delay is what got us here.
- **:37, not :30.** `:00`, `:15`, `:30` and `:45` are the platform's most
  oversubscribed scheduled slots. This is the one trigger still exposed to
  that queue, so it is the one that can least afford to sit behind
  everybody else's cron. It does not fix the delay; it is free.
- **+3:37 after 21Z is 00:37 the next day.** The cron is generated by
  `cron_entries()` rather than hand-written, and the round-trip test fires
  each generated line at its own minute and asserts it reads back as the
  cycle it encodes — which is what catches that rollover.

### How we will know whether it worked

`start_delay_min` on the `NBM-ARRIVAL` lines (§8.8). A week of
`grep NBM-ARRIVAL` over the job logs should show it near zero for
`trigger=railway-cron`. If it does not, `workflow_dispatch` is being
queued too and this design needs revisiting rather than tuning.

The second thing to watch is how often `trigger=schedule-backstop`
appears at all. It should be rare; a run of them means the Railway
service needs attention, and the non-zero exit on a failed dispatch is
what should have said so first.

## 9. Known limits

- `MOUNTAINOUS_RELIEF_THRESHOLD_FT` and `TERRAIN_RADIUS_NM` are placeholders
  pending comparison against the legacy shapefile. Relief is now at least
  visible and adjustable (§4.7); `TERRAIN_RADIUS_NM` is not, being baked
  into the cached grid at fetch time.
- `DEFAULT_MIN_AREA_SQ_MI` is a placeholder awaiting a forecaster's
  choice — see §4.6 and `scripts/mtn_obsc_area_sweep.py`.
- The relief gate at 500 ft is permissive enough that, with uniform
  probability, the western US comes out as a single area of over a
  million square miles. That is `MOUNTAINOUS_RELIEF_THRESHOLD_FT`'s
  placeholder status showing, not the area filter's — see §4.7 for the
  slider and the numbers now available for choosing it.
- Flat valley floors whose terrain search radius reaches into surrounding
  mountains (California's Central Valley) are not addressed by any
  elevation cutoff; that is a `TERRAIN_RADIUS_NM` question.
- `cell_dimensions_km()` evaluates longitude spacing once at the domain's
  middle latitude, so the closing radius is 10–15% off at the north and
  south edges of a CONUS domain. Cell *areas* are computed per row, where
  the same error would accumulate.
- HZ, FU and BLSN are deliberately not automated for either hazard: no NBM
  field exists, and per AWC practice they are rare enough to add by hand.
