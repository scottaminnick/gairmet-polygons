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
from pyproj import Geod
from shapely.geometry import shape as shapely_shape
from shapely.geometry import mapping as shapely_mapping

# WGS84 geodesic calculator -- gives TRUE area on the Earth's surface,
# correctly accounting for the fact that a degree of longitude covers
# less real distance at higher latitudes. Used for accurate "square
# miles" filtering (e.g. matching AIRMET/G-AIRMET's real 3,000 sq mi
# "widespread" criterion), instead of the older, cruder degrees-squared
# proxy (which is still available for callers that don't need real-world
# precision, e.g. small synthetic test grids).
_GEOD = Geod(ellps="WGS84")
SQ_METERS_PER_SQ_MILE = 2_589_988.11


def geodesic_area_sq_mi(polygon) -> float:
    """
    True area of a shapely polygon on the Earth's surface, in square
    miles, using proper geodesic calculation (not a flat-projection
    approximation). Works directly on lon/lat coordinates.
    """
    area_sq_m, _perimeter = _GEOD.geometry_area_perimeter(polygon)
    return abs(area_sq_m) / SQ_METERS_PER_SQ_MILE


def smooth_polygon_boundary(polygon, smoothing_deg: float):
    """
    Rounds off jagged, raster-derived polygon edges into something
    closer to a hand-drawn shape, using the standard "buffer out, buffer
    in, buffer in, buffer out" morphological closing+opening trick with
    round joins:

      - Closing (buffer out then in) fills small concave notches.
      - Opening (buffer in then out) trims small convex spikes.

    Together they knock off small-scale jaggedness while preserving the
    polygon's overall shape and size reasonably well. smoothing_deg is
    in degrees (matching simplify_tolerance_deg's units elsewhere in
    this module) -- 0 or negative disables this and returns the polygon
    unchanged.
    """
    if smoothing_deg <= 0 or polygon.is_empty:
        return polygon

    closed = polygon.buffer(smoothing_deg, join_style=1).buffer(-smoothing_deg, join_style=1)
    opened = closed.buffer(-smoothing_deg, join_style=1).buffer(smoothing_deg, join_style=1)

    if opened.is_empty:
        # Over-aggressive smoothing erased a small polygon entirely --
        # better to keep the original than to silently drop it.
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
