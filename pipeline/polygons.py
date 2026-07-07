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
from shapely.geometry import shape as shapely_shape
from shapely.geometry import mapping as shapely_mapping


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
    simplify_tolerance_deg: float = 0.02,
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
        noise/speckle. This is a crude filter -- 1 degree of longitude
        is a different real distance depending on latitude -- but it's
        good enough to strip out single-pixel artifacts. We can swap in
        a proper equal-area projection for this filter later if we find
        we need more precision.
    simplify_tolerance_deg : float
        Shapely `simplify()` tolerance, in degrees. Keeps vertex counts
        (and therefore GeoJSON file size) sane for a 2.5km CONUS grid.

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
        if poly.area < min_area_deg2:
            continue
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
