"""
pipeline/fetch_terrain.py
--------------------------
One-time (or rarely-rerun) fetch + processing of a terrain elevation
grid for Mountain Obscuration, sourced from the AWS "Terrain Tiles"
Open Data bucket (elevation-tiles-prod), Skadi format.

WHY THIS IS DIFFERENT FROM fetch_nbm.py
----------------------------------------
NBM's ceiling/visibility fields change every forecast cycle -- that's
the whole point of pipeline/fetch_nbm.py, run on a schedule. Terrain
does not change. This script is meant to be run manually and rarely
(realistically once, unless we widen coverage or bump resolution later)
-- it is NOT wired into the recurring 6-hourly Action. Its output is a
small, committed, cached grid; this script itself sits outside the
operational hazard-generation path.

DATA SOURCE
-----------
AWS Open Data "Terrain Tiles" (originally assembled by Mapzen, now
hosted by AWS): https://registry.opendata.aws/terrain-tiles/
  - Anonymous HTTPS reads, no AWS account/credentials needed.
  - Uses the "Skadi" encoding specifically: plain SRTM-style .hgt files,
    one per 1x1 degree tile, native (unprojected) lat/lon, 16-bit signed
    integer meters, big-endian. Chosen over the Terrarium/GeoTIFF
    encodings (tiled in Web Mercator) because it drops straight into
    this project's existing lon/lat grid convention (see
    pipeline/regrid.py) with no extra reprojection step.
  - Attribution is required by the data source's terms -- see
    https://github.com/tilezen/joerd/blob/master/docs/attribution.md
    (needs a line in the project README once this lands).

THE MEMORY PROBLEM, AND WHY THIS IS A STAGED PIPELINE
-------------------------------------------------------
A full CONUS mosaic at Skadi's native 1-arcsecond resolution would be
roughly 1,400 tiles x 3601x3601 pixels -- tens of GB, nowhere near
something we can hold in memory (or want to hold, since we only ever
need "highest nearby peak," not meter-level precision everywhere).

So each tile is: fetched -> immediately MAX-pooled down to a much
coarser INTERMEDIATE resolution (preserving peak height, not averaging
it away) -> placed into a full-CONUS intermediate mosaic -> the raw
tile is discarded. That intermediate mosaic (tens of MB) is small
enough to hold entirely in memory, which means the real radius-based
"highest nearby ridge" search can run as one clean operation over a
contiguous array, with no tile-boundary-halo bookkeeping needed.

WHAT THIS PRODUCES
-------------------
Two grids, both on the SAME regular lon/lat grid convention as
pipeline.regrid's NBM output (pipeline.grid_spec.GridSpec), cached
together in one compressed .npz:

  baseline_elevation_ft : "local" terrain elevation, lightly smoothed
      to roughly NBM's own working resolution. Stands in for the
      surface elevation NBM's ceiling-AGL forecast is implicitly
      relative to (NBM does not publish this field itself -- confirmed
      empty via pipeline/inspect_nbm.py's terrain search against a real
      NBM core-file inventory).

  ridge_elevation_ft : MAX elevation within TERRAIN_RADIUS_NM of each
      grid cell -- "the highest thing nearby a pilot would need to
      clear," not the grid cell's own elevation. This is the number
      Mountain Obscuration's critical_ceiling_agl formula actually
      needs; a valley-floor grid cell sitting next to a 9,000 ft ridge
      must see that ridge, not its own low elevation.

IMPORTANT: mtn_obsc.py must import CONUS_BOUNDS / OUTPUT_RESOLUTION_DEG
from THIS module (not redefine its own copies) when it regrids NBM's
ceiling fields for Mountain Obscuration, passing them as
regrid_to_regular_latlon(..., target_bounds=CONUS_BOUNDS,
target_resolution_deg=OUTPUT_RESOLUTION_DEG). Otherwise the terrain
grid and the NBM grid could end up on subtly different origins/extents
and silently misalign cell-for-cell.

TERRAIN_RADIUS_NM is a placeholder default -- not yet confirmed against
real output the way neighborhood_radius_nm and min_area_sq_mi were for
IFR. Deliberately isolated in one constant so it's a one-line change
(and one re-run) once a better value is chosen.

NOTE ON TESTING: the actual S3 fetch can't be exercised from a sandbox
without egress to amazonaws.com (same limitation as NBM fetch from
NOAA). Every piece of logic that doesn't require the network -- tile
naming, byte parsing, the radius math, the pooling/filtering pipeline
-- is covered by tests/test_fetch_terrain.py using synthetic data. The
network call itself needs a real run (e.g. via GitHub Actions, which
has full internet egress) to confirm end-to-end.
"""

from __future__ import annotations

import gzip
import math
import os
import sys
from pathlib import Path

import numpy as np
import requests
from scipy.ndimage import maximum_filter, uniform_filter

# Needed because this script is meant to be run directly
# (`python3 pipeline/fetch_terrain.py`), which only puts this file's own
# folder on Python's import path, not the repo root -- so `pipeline` the
# package is otherwise invisible to itself. Same fix
# pipeline/generate_latest_ifr.py already uses, for the same reason.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.grid_spec import GridSpec

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Deliberately generous CONUS box -- wider than NBM's actual grid extent on
# every side. Unlike pipeline/regrid.py (which reads NBM's own embedded grid
# definition rather than hardcoding projection parameters, precisely to
# avoid silently mislocating things if a hardcoded number were ever wrong),
# there's no equivalent "read it from the data" option here -- terrain
# tiles don't carry NBM's grid definition with them. So instead we pick a
# box guaranteed to fully cover NBM's real domain with margin to spare,
# rather than trying to match it exactly.
CONUS_BOUNDS = (-126.0, 22.0, -65.0, 50.0)  # (west, south, east, north)

# Matches pipeline/regrid.py's default NBM output resolution -- see the
# module docstring note above on why mtn_obsc.py must reuse this constant
# rather than picking its own.
OUTPUT_RESOLUTION_DEG = 0.025

# Intermediate mosaic resolution, in arcseconds. 30 arcsec (~900m) is
# still far finer than OUTPUT_RESOLUTION_DEG (90 arcsec / ~2.3km at mid
# latitude), so no meaningful peak information is lost at this stage --
# but it's ~9x fewer pixels than the raw 1-arcsec tiles, which is what
# makes holding a full-CONUS mosaic in memory practical (see module
# docstring). Deliberately chosen as a clean divisor of the raw 1-arcsec
# tiles AND a clean divisor of OUTPUT_RESOLUTION_DEG (90 / 30 = 3) so
# every downsampling step in this file is an exact integer block-reduce,
# never an approximate resample.
INTERMEDIATE_ARCSEC = 30
RAW_TILE_SIZE = 3601  # Skadi/SRTM1: 3601x3601, 1 arcsecond, tiles overlap
                       # their neighbor's edge pixel by design.
TILE_DOWNSAMPLE_FACTOR = INTERMEDIATE_ARCSEC  # 1 arcsec -> 30 arcsec

TERRAIN_RADIUS_NM = float(os.environ.get("MTN_OBSC_TERRAIN_RADIUS_NM", "12.0"))
# PLACEHOLDER default -- see module docstring. Overridable via the
# MTN_OBSC_TERRAIN_RADIUS_NM env var (wired to a workflow_dispatch input
# in .github/workflows/fetch_terrain.yml) so changing it doesn't require
# editing code -- but note this is fundamentally different from IFR's
# threshold_pct/neighborhood_radius_nm sliders: those are applied at
# HAZARD-GENERATION time, on the fly, against an already-fixed input
# grid. This radius is baked into ridge_elevation_ft at FETCH time --
# changing it means re-running this whole script, not adjusting a
# runtime slider in the eventual mtn_obsc.py.

SRTM_VOID = -32768  # SRTM/Skadi sentinel for "no data" -- treated as sea
                     # level (0) rather than a real elevation.

METERS_TO_FEET = 1.0 / 0.3048

# Generous real-world bounds, in FEET -- Death Valley (-282 ft, lowest
# point in North America) and Denali (20,310 ft, highest), both with
# wide margin. Anything outside this after unit conversion is treated
# as bad data (a source artifact, not a real elevation) and zeroed out
# at the earliest possible point, rather than being allowed to
# propagate into later aggregation steps and potentially silently wrap
# around when finally cast to int16. This check exists because of a
# real corrupted run: coastal tiles (where this dataset blends SRTM
# land data with ETOPO1 bathymetry to fill in oceans -- see
# https://github.com/tilezen/joerd/blob/master/docs/formats.md)
# produced values that survived the entire pipeline and wrapped around
# into int16 garbage (exactly 32767, and implausible large-magnitude
# negatives) completely silently -- no exception, no warning, just a
# corrupted .npz. This turns that into a loud, visible, safe clamp.
PLAUSIBLE_ELEVATION_FT_MIN = -1500.0
PLAUSIBLE_ELEVATION_FT_MAX = 20500.0

SKADI_BASE_URL = "https://s3.amazonaws.com/elevation-tiles-prod/skadi"


# ---------------------------------------------------------------------------
# Tile naming / URLs
# ---------------------------------------------------------------------------

def skadi_tile_name(sw_lat: int, sw_lon: int) -> str:
    """
    Standard SRTM/Skadi tile naming: a tile is named for its SOUTHWEST
    corner. e.g. sw_lat=39, sw_lon=-105 -> "N39W105" (covers 39-40N,
    105-104W -- the Colorado Front Range).
    """
    lat_hem = "N" if sw_lat >= 0 else "S"
    lon_hem = "E" if sw_lon >= 0 else "W"
    return f"{lat_hem}{abs(sw_lat):02d}{lon_hem}{abs(sw_lon):03d}"


def skadi_url(sw_lat: int, sw_lon: int) -> str:
    """Full S3 URL for one tile, e.g. .../skadi/N39/N39W105.hgt.gz"""
    name = skadi_tile_name(sw_lat, sw_lon)
    lat_dir = name[:3]  # e.g. "N39" -- Skadi's own directory convention
    return f"{SKADI_BASE_URL}/{lat_dir}/{name}.hgt.gz"


def list_conus_tiles(
    bounds: tuple[float, float, float, float] = CONUS_BOUNDS,
) -> list[tuple[int, int]]:
    """Every 1x1 degree (sw_lat, sw_lon) tile needed to cover `bounds`."""
    west, south, east, north = bounds
    lats = range(math.floor(south), math.ceil(north))
    lons = range(math.floor(west), math.ceil(east))
    return [(lat, lon) for lat in lats for lon in lons]


# ---------------------------------------------------------------------------
# Fetch + parse (network call and pure parsing kept separate so the parsing
# logic is testable without hitting the network -- see
# tests/test_fetch_terrain.py)
# ---------------------------------------------------------------------------

def _parse_hgt_bytes(raw_bytes: bytes, tile_size: int = RAW_TILE_SIZE) -> np.ndarray:
    """
    Decode raw (already gunzipped) .hgt bytes into a (tile_size, tile_size)
    float32 array of elevation in FEET.

    .hgt files store 16-bit signed big-endian integers, row-major,
    starting from the NORTHWEST corner (row 0 = northernmost, matching
    this project's GridSpec convention elsewhere -- no flip needed).
    Worth double-checking against one real downloaded tile once we have
    network access to confirm; this is standard SRTM convention but
    hasn't been confirmed against Skadi's actual bytes yet.

    Values are stored in METERS in the source file -- converted to feet
    here so every function downstream of this one can assume feet
    without needing to know or care about the source encoding.
    """
    arr = np.frombuffer(raw_bytes, dtype=">i2").reshape(tile_size, tile_size)
    arr = arr.astype(np.float32)
    arr[arr == SRTM_VOID] = 0.0
    arr *= METERS_TO_FEET

    implausible = (arr < PLAUSIBLE_ELEVATION_FT_MIN) | (arr > PLAUSIBLE_ELEVATION_FT_MAX)
    n_bad = int(implausible.sum())
    if n_bad:
        print(
            f"  WARNING: {n_bad} pixel(s) outside plausible elevation range "
            f"({PLAUSIBLE_ELEVATION_FT_MIN}, {PLAUSIBLE_ELEVATION_FT_MAX}) ft "
            f"in this tile -- zeroing them out. Sample bad value: {arr[implausible].flat[0]:.1f} ft."
        )
        arr[implausible] = 0.0

    return arr


def fetch_tile_raw(
    sw_lat: int, sw_lon: int, session: requests.Session
) -> np.ndarray | None:
    """
    Downloads and decodes one Skadi tile. Returns a (RAW_TILE_SIZE,
    RAW_TILE_SIZE) float32 elevation array, or None if the tile doesn't
    exist -- Skadi simply has no file for tiles that are 100% open ocean,
    which is normal and expected, not an error condition.
    """
    url = skadi_url(sw_lat, sw_lon)
    resp = session.get(url, timeout=30)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    raw_bytes = gzip.decompress(resp.content)
    return _parse_hgt_bytes(raw_bytes)


# ---------------------------------------------------------------------------
# Per-tile downsample + mosaic assembly
# ---------------------------------------------------------------------------

def _block_max_pool(arr: np.ndarray, factor: int) -> np.ndarray:
    """
    Downsample a 2D array by taking the MAX (not mean) over each
    factor x factor block -- deliberately preserves peak height rather
    than smoothing it away, since a real ridge narrower than one output
    pixel still needs to register as tall, not get averaged down with
    the valley next to it.

    Trims at most (factor - 1) rows/cols off the far edge if the array
    doesn't divide evenly. For the raw-tile downsample this trims a
    sliver of the tile's shared edge pixel, which is redundant with the
    neighboring tile's own copy of that same edge -- not lost data.
    """
    rows, cols = arr.shape
    tr, tc = (rows // factor) * factor, (cols // factor) * factor
    trimmed = arr[:tr, :tc]
    reshaped = trimmed.reshape(tr // factor, factor, tc // factor, factor)
    return reshaped.max(axis=(1, 3))


def _block_mean_pool(arr: np.ndarray, factor: int) -> np.ndarray:
    """Same as _block_max_pool but averaging -- used for the baseline
    (local elevation) grid, which should represent "the ground here,"
    not "the highest nearby peak.\""""
    rows, cols = arr.shape
    tr, tc = (rows // factor) * factor, (cols // factor) * factor
    trimmed = arr[:tr, :tc]
    reshaped = trimmed.reshape(tr // factor, factor, tc // factor, factor)
    return reshaped.mean(axis=(1, 3))


def assemble_intermediate_mosaic(
    bounds: tuple[float, float, float, float] = CONUS_BOUNDS,
    downsample_factor: int = TILE_DOWNSAMPLE_FACTOR,
    session: requests.Session | None = None,
) -> tuple[np.ndarray, GridSpec]:
    """
    Fetches every tile covering `bounds`, max-pools each one down to
    INTERMEDIATE_ARCSEC resolution immediately (discarding the raw
    tile), and places it into one full-region mosaic array. Missing
    (ocean) tiles are filled with 0.0 -- sea level is the correct value,
    and 0 can never wrongly dominate a later MAX filter the way a
    fabricated high value would.
    """
    session = session or requests.Session()
    west, south, east, north = bounds
    intermediate_deg = downsample_factor / 3600.0  # arcsec -> degrees

    n_rows = round((north - south) / intermediate_deg)
    n_cols = round((east - west) / intermediate_deg)
    mosaic = np.zeros((n_rows, n_cols), dtype=np.float32)

    px_per_tile = RAW_TILE_SIZE // downsample_factor  # after per-tile pooling

    for sw_lat, sw_lon in list_conus_tiles(bounds):
        raw = fetch_tile_raw(sw_lat, sw_lon, session)
        pooled = (
            _block_max_pool(raw, downsample_factor)
            if raw is not None
            else np.zeros((px_per_tile, px_per_tile), dtype=np.float32)
        )
        # Row 0 of `mosaic` is the NORTH edge (north - ...), matching
        # GridSpec convention; row 0 of `pooled` is also north (see
        # _parse_hgt_bytes). This tile's north edge sits (sw_lat+1)
        # degrees north of `south`.
        row_start = round((north - (sw_lat + 1)) / intermediate_deg)
        col_start = round((sw_lon - west) / intermediate_deg)
        mosaic[row_start: row_start + px_per_tile, col_start: col_start + px_per_tile] = pooled

    grid_spec = GridSpec(west=west, north=north, dx=intermediate_deg, dy=-intermediate_deg)
    return mosaic, grid_spec


# ---------------------------------------------------------------------------
# Radius math (nm -> degrees, latitude-dependent)
# ---------------------------------------------------------------------------

def _radius_deg(radius_nm: float, center_lat_deg: float) -> tuple[float, float]:
    """
    Convert a real-world radius in nautical miles into (lat_deg, lon_deg)
    half-widths, valid near center_lat_deg.

    Latitude: exact everywhere -- 1 nautical mile has been defined as 1
    arcminute of latitude since the term was coined, so this never needs
    a "near this latitude" caveat.

    Longitude: degrees-per-nm grows by 1/cos(latitude) as you move away
    from the equator (meridians converge toward the poles), so the same
    real-world radius spans MORE degrees of longitude at higher latitude.
    This is applied per latitude BAND in compute_output_grids rather than
    once for all of CONUS: CONUS spans ~22N-50N, and cos(22 deg)=0.93 vs
    cos(50 deg)=0.64 -- a single global factor would make the search
    radius wrong by roughly 45% at one end of the domain or the other.
    """
    lat_deg = radius_nm / 60.0
    lon_deg = radius_nm / (60.0 * math.cos(math.radians(center_lat_deg)))
    return lat_deg, lon_deg


# ---------------------------------------------------------------------------
# Final output grids
# ---------------------------------------------------------------------------

def compute_output_grids(
    mosaic: np.ndarray,
    mosaic_grid_spec: GridSpec,
    terrain_radius_nm: float = TERRAIN_RADIUS_NM,
    output_resolution_deg: float = OUTPUT_RESOLUTION_DEG,
    output_bounds: tuple[float, float, float, float] = CONUS_BOUNDS,
) -> tuple[np.ndarray, np.ndarray, GridSpec]:
    """
    From the full-region intermediate mosaic, computes the two grids
    Mountain Obscuration needs, both resampled onto a regular grid
    matching pipeline.regrid's convention (same GridSpec shape as NBM's
    regridded output -- see module docstring for why mtn_obsc.py must
    reuse CONUS_BOUNDS / OUTPUT_RESOLUTION_DEG rather than redefining
    them).

    Returns (baseline_elevation_ft, ridge_elevation_ft, output_grid_spec),
    both grids rounded to whole feet as int16 (elevation fits comfortably
    in int16 without needing IFR's lossy uint8 percentage-quantization
    trick).
    """
    n_rows, n_cols = mosaic.shape
    intermediate_deg = mosaic_grid_spec.dx
    row_lats = mosaic_grid_spec.north - np.arange(n_rows) * intermediate_deg

    ridge_full = np.empty_like(mosaic)
    band_size = max(1, round(1.0 / intermediate_deg))  # ~1 degree of rows/band
    for band_start in range(0, n_rows, band_size):
        band_end = min(band_start + band_size, n_rows)
        center_lat = row_lats[(band_start + band_end) // 2]
        lat_radius_deg, lon_radius_deg = _radius_deg(terrain_radius_nm, center_lat)
        row_px = max(1, round(lat_radius_deg / intermediate_deg))
        col_px = max(1, round(lon_radius_deg / intermediate_deg))

        # Pad with neighboring-band context so the filter has full
        # coverage right at this band's boundary, not just within it.
        pad_top = min(band_start, row_px)
        pad_bottom = min(n_rows - band_end, row_px)
        window = mosaic[band_start - pad_top: band_end + pad_bottom, :]
        # NOTE: rectangular footprint (size=...), not a true circle --
        # a deliberate v1 simplification. Slightly over-generous at the
        # window's corners, which is the safe direction to be wrong in
        # for a hazard-detection search (better to over-include a
        # borderline ridge than miss one).
        filtered = maximum_filter(window, size=(2 * row_px + 1, 2 * col_px + 1), mode="nearest")
        ridge_full[band_start:band_end, :] = filtered[pad_top: pad_top + (band_end - band_start), :]

    # Baseline: gentle smoothing roughly matching NBM's own working
    # resolution -- standing in for the surface elevation NBM's
    # ceiling-AGL forecast is implicitly relative to (see module
    # docstring; NBM does not publish this field itself).
    baseline_full = uniform_filter(mosaic, size=3, mode="nearest")

    ratio = output_resolution_deg / intermediate_deg
    if abs(ratio - round(ratio)) > 1e-6:
        raise ValueError(
            f"output_resolution_deg ({output_resolution_deg}) must be a "
            f"whole-number multiple of the intermediate mosaic resolution "
            f"({intermediate_deg}) -- got a ratio of {ratio}."
        )
    ratio = round(ratio)

    ridge_out = _block_max_pool(ridge_full, ratio)
    # Mean, not max, for baseline's final reduce -- baseline should stay
    # "the ground here," not get dragged back toward ridge height by a
    # second max operation.
    baseline_out = _block_mean_pool(baseline_full, ratio)

    # Defensive final check, in addition to the per-tile validation in
    # _parse_hgt_bytes -- belt-and-suspenders. If ANY float32 value here
    # is still outside int16's representable range (or NaN/Inf) when we
    # cast below, numpy does NOT raise an error by default -- it silently
    # wraps/corrupts (exactly what happened on a real run: coastal tiles
    # produced values that reached this point uncaught and wrapped into
    # exactly 32767 and implausible negatives, with no exception at all).
    # Clamping explicitly here, with a loud print if it ever actually
    # triggers, converts "silent data corruption" into "impossible."
    for name, grid in (("baseline", baseline_out), ("ridge", ridge_out)):
        bad = ~np.isfinite(grid) | (grid < -32768) | (grid > 32767)
        n_bad = int(np.sum(bad))
        if n_bad:
            print(
                f"  WARNING: {n_bad} pixel(s) in the FINAL {name} grid were still "
                f"outside int16 range (or NaN/Inf) after per-tile validation -- "
                f"clamping to 0 rather than letting numpy silently wrap them. "
                f"This means a bad value slipped past _parse_hgt_bytes' check; "
                f"worth investigating which tile it came from."
            )
            grid[bad] = 0.0

    west, south, east, north = output_bounds
    out_grid_spec = GridSpec(west=west, north=north, dx=output_resolution_deg, dy=-output_resolution_deg)
    return (
        np.round(baseline_out).astype(np.int16),
        np.round(ridge_out).astype(np.int16),
        out_grid_spec,
    )


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main(output_path: str = "data/terrain/terrain_grid.npz") -> None:
    print(f"Fetching {len(list_conus_tiles())} Skadi tiles covering {CONUS_BOUNDS}...")
    mosaic, mosaic_grid_spec = assemble_intermediate_mosaic()
    print(f"Intermediate mosaic assembled: {mosaic.shape}")

    baseline_ft, ridge_ft, out_grid_spec = compute_output_grids(mosaic, mosaic_grid_spec)
    print(f"Output grids computed: {baseline_ft.shape}, terrain_radius_nm={TERRAIN_RADIUS_NM}")

    np.savez_compressed(
        output_path,
        baseline_elevation_ft=baseline_ft,
        ridge_elevation_ft=ridge_ft,
        west=out_grid_spec.west,
        north=out_grid_spec.north,
        dx=out_grid_spec.dx,
        dy=out_grid_spec.dy,
        terrain_radius_nm=TERRAIN_RADIUS_NM,
    )
    print(f"Saved to {output_path}")


if __name__ == "__main__":
    main()
