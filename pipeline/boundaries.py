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

import threading

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
#
# The file is the U.S. domestic FIR, built by scripts/build_aor_boundary.py
# (rule and numbers in docs/METHODS.md section 3). It replaced the raw
# 20-ARTCC union (data/boundaries/artcc.json), which spilled ~17,900 sq mi
# over Canada through the FAA's delegated airspace (Rainy River, the
# Canadian side of Lake Erie, New Brunswick) and missed the U.S. half of
# Lake Superior. artcc.json stays in the repo: the viewer's ARTCC reference
# layer serves it, and pointing this path back at it is the rollback.
CONUS_BOUNDARY_PATH = "data/boundaries/conus_aor.json"

_boundary_mask_cache: dict = {}

# One lock per cache key, so concurrent first callers of the SAME mask wait
# for one computation instead of each repeating it, while different masks
# can still be computed side by side. Without it, two overlapping first
# requests each rasterized the full mask (measured: two concurrent 44 s
# requests). _locks_guard only protects the dict of locks.
_boundary_mask_locks: dict = {}
_locks_guard = threading.Lock()


def _cache_key(grid_spec: GridSpec, shape: tuple, boundaries_path: str) -> tuple:
    return (grid_spec.west, grid_spec.north, grid_spec.dx, grid_spec.dy, tuple(shape), boundaries_path)


def _lock_for(cache_key: tuple) -> threading.Lock:
    with _locks_guard:
        return _boundary_mask_locks.setdefault(cache_key, threading.Lock())


def get_boundary_mask(grid_spec: GridSpec, shape: tuple, boundaries_path: str) -> np.ndarray:
    """
    Memoized wrapper around pipeline.polygons.load_boundary_mask().
    Both boundary files here (LAND_BOUNDARY_PATH and
    CONUS_BOUNDARY_PATH) are static -- they don't depend on NBM data, the
    forecast cycle, or the forecast hour -- but unioning + rasterizing
    either one is slow: ~38 s for a CONUS-sized grid, nearly all of it in
    skimage.draw.polygon, which tests every cell in the polygon's bounding
    box against every edge and holds the GIL throughout (so a first call in
    the web app stalls every other request too; see warm_boundary_masks).
    Computing them fresh for each of a run's 5 snapshots would add ~5x that
    cost for zero benefit. Cached here by (grid_spec fields, shape, path)
    rather than via functools.lru_cache directly on GridSpec, since GridSpec
    is a plain (non-frozen) dataclass and so isn't hashable by default.
    Keying on path means the two different boundary files each get their
    own cache entry rather than colliding.

    Thread-safe: concurrent first callers for one key compute it once.
    """
    cache_key = _cache_key(grid_spec, shape, boundaries_path)
    mask = _boundary_mask_cache.get(cache_key)
    if mask is not None:
        return mask
    with _lock_for(cache_key):
        if cache_key not in _boundary_mask_cache:
            _boundary_mask_cache[cache_key] = load_boundary_mask(boundaries_path, grid_spec, shape)
        return _boundary_mask_cache[cache_key]


def warm_boundary_masks(requests, executor) -> int:
    """
    Compute the masks for (grid_spec, shape, boundaries_path) requests that
    are not cached yet, on `executor`, and store them. Returns how many
    were computed.

    Meant for a PROCESS pool: the rasterization holds the GIL for its whole
    ~38 s, so warming in a thread would freeze a web server exactly as the
    cold first request does. In a child process it costs the server
    nothing but CPU. Each key's lock is held while its mask is computed, so
    a request that needs it meanwhile waits for this result -- blocked on
    a lock, which releases the GIL -- rather than starting its own.
    """
    computed = 0
    for grid_spec, shape, boundaries_path in requests:
        cache_key = _cache_key(grid_spec, shape, boundaries_path)
        if cache_key in _boundary_mask_cache:
            continue
        with _lock_for(cache_key):
            if cache_key in _boundary_mask_cache:
                continue
            future = executor.submit(load_boundary_mask, boundaries_path, grid_spec, tuple(shape))
            _boundary_mask_cache[cache_key] = future.result()
            computed += 1
    return computed
