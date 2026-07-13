"""
polygons.py
-----------
The core, hazard-agnostic engine of this pipeline.

Everything else in this project (IFR ceiling/visibility, mountain
obscuration, whatever we add later) boils down to the same shape of
problem:

    "I have a 2D grid of numbers (a probability, a height, whatever)
     laid out over lon/lat points. Give me the polygon(s) where that
     grid crosses some threshold."

That's it. This module does ONLY that, and nothing NBM-specific, on
purpose. Reasons this separation matters:

1. We can unit-test this with made-up numpy arrays (no internet, no
   grib2, no NOAA servers needed) -- which is exactly what we're about
   to do in test_polygons.py.
2. If we ever want a different hazard (say, icing, or turbulence) we
   just feed it a different grid -- none of this code changes.
3. It keeps the "hard geospatial math" in one well-tested place instead
   of copy-pasted into every hazard file.

How it works, in plain English:
1. We treat `values >= threshold` as a black/white mask ("in the hazard
   area" vs "not").
2. `rasterio.features.shapes()` walks that mask and traces the boundary
   of every contiguous blob of "in" pixels, automatically handling
   holes (e.g. a clear pocket surrounded by IFR conditions) for us.
   This is the standard, robust way to do raster-to-vector conversion --
   much less fiddly than hand-rolling a marching-squares contour tracer.
3. We convert the resulting pixel-space polygons into real lon/lat
   polygons using the grid's affine transform.
4. We drop tiny speckle polygons (small_area filter) and simplify the
   remaining ones (fewer vertices = smaller GeoJSON = faster web map),
   because a 2.5km-resolution CONUS grid can produce polygons with
   thousands of vertices if left untouched.
"""

from __future__ import annotations

from dataclasses import dataclass

import geojson
import numpy as np
import rasterio.features
from affine import Affine
from pyproj import CRS, Geod, Transformer
from shapely.geometry import shape as shapely_shape
from shapely.geometry import mapping as shapely_mapping
from shapely.ops import transform as shapely_transform
from shapely.ops import unary_union

# WGS84 geodesic calculator -- gives TRUE area on the Earth's surface,
# correctly accounting for the fact that a degree of longitude covers
# less real distance at higher latitudes. Used for accurate "square
# miles" filtering (e.g. matching AIRMET/G-AIRMET's real 3,000 sq mi
# "widespread" criterion), instead of the older, cruder degrees-squared
# proxy (which is still available for callers that don't need real-world
# precision, e.g. small synthetic test grids).
_GEOD = Geod(ellps="WGS84")
SQ_METERS_PER_SQ_MILE = 2_589_988.11
NM_TO_METERS = 1852.0


def geodesic_area_sq_mi(polygon) -> float:
    """
    True area of a shapely polygon on the Earth's surface, in square
    miles, using proper geodesic calculation (not a flat-projection
    approximation). Works directly on lon/lat coordinates.
    """
    area_sq_m, _perimeter = _GEOD.geometry_area_perimeter(polygon)
    return abs(area_sq_m) / SQ_METERS_PER_SQ_MILE


def filter_polygons_by_area(polygons: list, min_area_sq_mi: float) -> list:
    """Drops polygons smaller than min_area_sq_mi, using true geodesic area."""
    return [p for p in polygons if geodesic_area_sq_mi(p) >= min_area_sq_mi]


def merge_nearby_polygons(polygons: list, radius_nm: float) -> list:
    """
    Merges polygons that are within radius_nm of each other into single
    combined shapes -- this is how "pull nearby smaller areas into
    larger ones" should actually work, replacing an earlier approach
    that blurred the GRID with a circular filter before contouring.

    Why this is different (and avoids turning isolated areas into
    circles): this uses morphological CLOSING (buffer every polygon out
    by radius_nm, union any that now overlap, then buffer back in by
    the same amount) on the POLYGONS themselves, not the underlying
    grid. An isolated polygon with nothing else nearby buffers out and
    then immediately back in to very close to its ORIGINAL shape --
    closing is a no-op-ish operation for isolated shapes. Two polygons
    within 2x radius_nm of each other, though, have their buffered
    versions overlap, so they union into one connected shape with a
    smoothed "bridge" between them. Grid-level blurring can't do this:
    it grows EVERY point by the same disk regardless of whether
    there's anything nearby to justify it, which is exactly what
    produced literal circles around isolated hazard areas.

    Uses a locally-accurate azimuthal equidistant projection (centered
    on the combined bounding box of all input polygons) so the
    real-world buffer distance is correct regardless of latitude --
    unlike buffering directly in lon/lat degrees, which would distort
    unevenly.

    Parameters
    ----------
    polygons : list of shapely geometries (lon/lat)
    radius_nm : float
        Real-world radius, nautical miles. 0 or negative = no-op.

    Returns
    -------
    list of shapely geometries (lon/lat), possibly fewer than the input
    if some were merged together.
    """
    if not polygons or radius_nm <= 0:
        return polygons

    bounds = [p.bounds for p in polygons]
    minx = min(b[0] for b in bounds)
    miny = min(b[1] for b in bounds)
    maxx = max(b[2] for b in bounds)
    maxy = max(b[3] for b in bounds)
    center_lon = (minx + maxx) / 2
    center_lat = (miny + maxy) / 2

    aeqd = CRS.from_proj4(f"+proj=aeqd +lat_0={center_lat} +lon_0={center_lon} +units=m")
    to_aeqd = Transformer.from_crs("EPSG:4326", aeqd, always_xy=True).transform
    to_lonlat = Transformer.from_crs(aeqd, "EPSG:4326", always_xy=True).transform

    radius_m = radius_nm * NM_TO_METERS

    projected = [shapely_transform(to_aeqd, p) for p in polygons]
    grown = [p.buffer(radius_m) for p in projected]
    closed = unary_union(grown).buffer(-radius_m)

    if closed.is_empty:
        return polygons  # shouldn't happen, but don't silently lose everything

    result_geoms = list(closed.geoms) if hasattr(closed, "geoms") else [closed]
    return [shapely_transform(to_lonlat, g) for g in result_geoms if not g.is_empty]


def smooth_polygon_boundary(polygon, smoothing_deg: float, join_style: int = 2):
    """
    Rounds off small-scale jaggedness in a polygon's boundary using the
    "buffer out, buffer in, buffer in, buffer out" morphological
    closing+opening trick:

      - Closing (buffer out then in) fills small concave notches.
      - Opening (buffer in then out) trims small convex spikes.

    join_style controls the CHARACTER of the result: 1=round (smooth
    curves -- can look "blobby" if overused), 2=mitre (sharp corners,
    default here -- closer to a hand-drawn look), 3=bevel. Real
    forecaster-drawn G-AIRMET polygons have straight segments and sharp
    vertices, not smooth curves, so mitre is the better default despite
    "smooth_polygon_boundary" sounding like it should mean rounded --
    the goal is removing small-scale jaggedness, not adding roundness.

    smoothing_deg is in degrees -- 0 or negative disables this and
    returns the polygon unchanged.
    """
    if smoothing_deg <= 0 or polygon.is_empty:
        return polygon

    closed = polygon.buffer(smoothing_deg, join_style=join_style).buffer(-smoothing_deg, join_style=join_style)
    opened = closed.buffer(-smoothing_deg, join_style=join_style).buffer(smoothing_deg, join_style=join_style)

    if opened.is_empty:
        return polygon
    return opened


@dataclass
class GridSpec:
    """
    Describes how a 2D numpy array maps onto real-world lon/lat.

    We assume a REGULAR grid for now (constant spacing in each
    direction) -- true for a simple lat/lon grid, and also true for
    NBM's native Lambert Conformal Conic grid *in its own x/y space*
    (we'd reproject to lon/lat as a separate step -- see the note at
    the bottom of this file).

    Attributes
    ----------
    west, north : float
        Lon/lat of the CENTER of the top-left pixel, i.e. values[0, 0].
    dx, dy : float
        Pixel size in the x (longitude) and y (latitude) directions.
        dx should be positive (grid runs west->east).
        dy should be NEGATIVE if row 0 is the northernmost row (the
        conventional "image" orientation, and what rasterio expects).
    """

    west: float
    north: float
    dx: float
    dy: float  # typically negative

    def to_affine(self) -> Affine:
        """Build the affine transform rasterio needs: pixel (col,row) -> (lon,lat)."""
        return Affine(self.dx, 0.0, self.west - self.dx / 2, 0.0, self.dy, self.north - self.dy / 2)


def save_grid_cache(path, values: np.ndarray, grid_spec: GridSpec) -> None:
    """
    Saves a prepared grid + its GridSpec to a compressed .npz file, so
    it can be re-processed later (e.g. with different forecaster-
    adjustable parameters) without re-fetching or re-preparing from
    source data.

    Quantizes values to uint8 (rounded to the nearest integer
    percentage point, 0-100) rather than storing float32. This isn't
    just smaller -- it's DRAMATICALLY smaller in practice (~70x in
    testing, 14.7MB -> 0.2MB for a realistic CONUS-sized grid), because
    repeated byte patterns in low-entropy integer data compress far
    better than float32's effectively-random-looking mantissa bits.
    Max error from this rounding is 0.5 percentage points -- negligible
    for a threshold decision, and irrelevant compared to NBM's own
    forecast uncertainty.
    """
    np.savez_compressed(
        path,
        values=np.round(values).astype(np.uint8),
        west=grid_spec.west,
        north=grid_spec.north,
        dx=grid_spec.dx,
        dy=grid_spec.dy,
    )


def load_grid_cache(path) -> tuple[np.ndarray, GridSpec]:
    """
    Loads a grid + GridSpec previously saved with save_grid_cache().
    Returns values as float32 (upcast from the stored uint8) so
    downstream code (thresholding, smoothing) works exactly as it does
    with a freshly-prepared grid, without needing to know about the
    on-disk quantization.
    """
    data = np.load(path)
    grid_spec = GridSpec(
        west=float(data["west"]), north=float(data["north"]), dx=float(data["dx"]), dy=float(data["dy"])
    )
    return data["values"].astype(np.float32), grid_spec


def grid_to_polygons(
    values: np.ndarray,
    grid: GridSpec,
    threshold: float,
    min_area_deg2: float = 0.01,
    min_area_sq_mi: float | None = None,
    simplify_tolerance_deg: float = 0.02,
    boundary_smoothing_deg: float = 0.0,
) -> list:
    """
    Convert a 2D grid into a list of shapely polygons/multipolygons
    wherever `values >= threshold`.

    Parameters
    ----------
    values : np.ndarray, shape (rows, cols)
        e.g. probability (0-100) that ceiling < 1000 ft at each grid cell.
    grid : GridSpec
        Describes the lon/lat location of every cell in `values`.
    threshold : float
        Cells with value >= threshold are considered "inside" the hazard.
    min_area_deg2 : float
        Polygons smaller than this (in square degrees) are dropped as
        noise/speckle. Crude -- a degree of longitude is a different
        real distance depending on latitude. Used only when
        min_area_sq_mi is NOT provided (kept around for small synthetic
        test grids where real-world precision doesn't matter).
    min_area_sq_mi : float, optional
        If provided, filters using TRUE geodesic area in square miles
        instead of the crude degrees^2 proxy above -- use this for real
        data. E.g. AIRMET/G-AIRMET's historical "widespread" criterion
        is 3,000 sq mi.
    simplify_tolerance_deg : float
        Shapely `simplify()` tolerance, in degrees. Keeps vertex counts
        (and therefore GeoJSON file size) sane for a 2.5km CONUS grid.
    boundary_smoothing_deg : float
        If > 0, rounds off jagged raster-derived edges into a more
        hand-drawn-looking shape (see smooth_polygon_boundary()). 0
        (default) disables this.

    Returns
    -------
    list[shapely.geometry.Polygon | MultiPolygon]
    """
    if values.ndim != 2:
        raise ValueError(f"Expected a 2D grid, got shape {values.shape}")

    mask = (values >= threshold).astype(np.uint8)

    if not mask.any():
        return []

    transform = grid.to_affine()

    polygons = []
    # rasterio.features.shapes yields (geojson-like geometry dict, value)
    # pairs for every contiguous region of equal value in the mask.
    for geom, value in rasterio.features.shapes(mask, mask=mask.astype(bool), transform=transform):
        if value != 1:
            continue
        poly = shapely_shape(geom)

        if min_area_sq_mi is not None:
            if geodesic_area_sq_mi(poly) < min_area_sq_mi:
                continue
        elif poly.area < min_area_deg2:
            continue

        if boundary_smoothing_deg > 0:
            poly = smooth_polygon_boundary(poly, boundary_smoothing_deg)
        if simplify_tolerance_deg > 0:
            poly = poly.simplify(simplify_tolerance_deg, preserve_topology=True)
        if not poly.is_empty:
            polygons.append(poly)

    return polygons


def polygons_to_feature_collection(
    polygons: list,
    properties: dict | None = None,
) -> geojson.FeatureCollection:
    """
    Wrap a list of shapely polygons into a GeoJSON FeatureCollection,
    attaching the same `properties` dict to every feature (e.g.
    hazard type, threshold used, valid time).
    """
    properties = properties or {}
    features = [
        geojson.Feature(geometry=shapely_mapping(poly), properties=dict(properties))
        for poly in polygons
    ]
    return geojson.FeatureCollection(features)


# ---------------------------------------------------------------------------
# NOTE for later (Track B): NBM's native grid is NOT plain lat/lon.
#
# NBM's CONUS domain is a ~2.5km Lambert Conformal Conic grid. Before we
# can use grid_to_polygons() on real NBM data, we'll need to reproject
# either:
#   (a) the NBM grid's x/y coordinates into lon/lat (then build a
#       GridSpec directly from that -- possible, but the grid is only
#       "regular" in its NATIVE projection, not in lon/lat, so the
#       GridSpec approach above technically breaks down), or
#   (b) resample the NBM data onto a regular lon/lat grid first (e.g.
#       with pyproj + scipy/xarray interpolation), THEN run it through
#       this module.
#
# Option (b) is simpler and is what we'll do -- it costs a small amount
# of resampling accuracy but keeps this module simple and reusable. This
# will live in pipeline/regrid.py once we get to Track B.
# ---------------------------------------------------------------------------
