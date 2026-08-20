"""
pipeline/boundaries.py
------------------------
Shared geographic boundary masks -- the static "where is this product
actually responsible for" files, plus the memoized rasterizer every
hazard module uses to turn one of them into a grid-shaped boolean mask.

Lives here rather than inside a hazard module because it is genuinely
hazard-agnostic: MTN OBSC needs both files (a real coastline for water
exclusion, and AWC's ARTCC area of responsibility), IFR needs the ARTCC
one, and any future SIERRA-category hazard will need the same. Keeping
one module-level cache here also means the two hazards SHARE cache
entries when they run against the same grid in one process, rather than
each paying the several-second union+rasterize cost separately.

The masks themselves come from pipeline.polygons.load_boundary_mask(),
which is the generic (path, grid_spec, shape) -> boolean array
primitive; this module adds only the memoization and the two paths.
"""

from __future__ import annotations

import numpy as np

from pipeline.polygons import GridSpec, load_boundary_mask

# The PRIMARY water exclusion -- a real land polygon following the actual
# coastline, unlike CONUS_BOUNDARY_PATH below (which deliberately extends
# over "adjacent coastal waters" per NWSI 10-811, and so cannot exclude
# ocean on its own).
#
# This exists because the current operational drawing tooling can't
# follow a real coastline -- legacy FAA constraints force a fictitious
# straight-ish line offshore -- so rather than reproduce that limitation,
# this cuts polygons off at the real coast, which the higher-resolution
# terrain data here makes possible.
#
# Measured against a real run's F00 output: adds a ~12% total-area
# reduction over the elevation check alone (which managed only ~0.7%,
# because smoothing bleeds real coastal peak elevations into adjacent
# ocean cells, giving them POSITIVE baseline that no sea-level cutoff can
# catch). Verified to flip exactly at the real coastline (44.0N: -124.1
# is land at 169 ft, -124.2 is water at -131 ft) and to correctly exclude
# the Great Lakes.
LAND_BOUNDARY_PATH = "data/boundaries/us_states.json"

# AWC's own defined area of responsibility per NWSI 10-811 section 3 --
# "Twenty (20) domestic Air Route Traffic Control Center (ARTCC) Flight
# Information Regions (FIRs) covering the conterminous U.S. and adjacent
# coastal waters." Used to restrict a hazard to real CONUS, rather than
# an arbitrary bounding box -- confirmed directly (see a real run's
# output) that the terrain grid's generously-sized bounding box (see
# pipeline.fetch_terrain.CONUS_BOUNDS -- deliberately wide margin, by
# design) was producing real MTN OBSC polygons over Quebec, Mexico, and
# open Pacific water, since a rectangle can't cleanly exclude a
# neighboring country that happens to share similar latitudes. The real
# ARTCC boundary follows the actual US border, which a bounding box
# cannot. The same is true of IFR against NBM's CONUS grid, whose
# coverage runs well out over the Pacific, Canada, and the Atlantic.
#
# Still needed ALONGSIDE LAND_BOUNDARY_PATH above, not replaced by it:
# us_states.json is US land only, so it happens to exclude Mexico and
# Canada too -- but the ARTCC boundary is the authoritative statement of
# what this product is actually responsible for, and keeping it explicit
# means the intent survives if the land file is ever swapped for one with
# different coverage.
CONUS_BOUNDARY_PATH = "data/boundaries/artcc.json"

_boundary_mask_cache: dict = {}


def get_boundary_mask(grid_spec: GridSpec, shape: tuple, boundaries_path: str) -> np.ndarray:
    """
    Memoized wrapper around pipeline.polygons.load_boundary_mask().
    Both boundary files here (LAND_BOUNDARY_PATH and
    CONUS_BOUNDARY_PATH) are static -- they don't depend on NBM data, the
    forecast cycle, or the forecast hour -- but unioning + rasterizing
    either one takes several real seconds (see load_boundary_mask's
    docstring), so computing them fresh for each of a run's 5 snapshots
    would add ~5x that cost for zero benefit. Cached here by (grid_spec
    fields, shape, path) rather than via functools.lru_cache directly on
    GridSpec, since GridSpec is a plain (non-frozen) dataclass and so
    isn't hashable by default. Keying on path means the two different
    boundary files each get their own cache entry rather than colliding.
    """
    cache_key = (grid_spec.west, grid_spec.north, grid_spec.dx, grid_spec.dy, shape, boundaries_path)
    if cache_key not in _boundary_mask_cache:
        _boundary_mask_cache[cache_key] = load_boundary_mask(boundaries_path, grid_spec, shape)
    return _boundary_mask_cache[cache_key]
