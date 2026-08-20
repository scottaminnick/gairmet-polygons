"""
pipeline/grid_spec.py
-----------------------
GridSpec lives in its own module, separate from pipeline/polygons.py,
specifically so lightweight scripts that only need the grid coordinate
convention (e.g. pipeline/fetch_terrain.py, which talks to a plain S3
bucket and has nothing to do with polygon generation) aren't forced to
install polygons.py's much heavier dependencies -- geojson, pyproj,
shapely, skimage -- just to get a 4-field dataclass.

pipeline/polygons.py re-exports GridSpec from here, so every existing
`from pipeline.polygons import GridSpec` elsewhere in the codebase
continues to work completely unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass

from affine import Affine


@dataclass
class GridSpec:
    """
    Describes how a 2D numpy array maps onto real-world lon/lat.

    We assume a REGULAR grid for now (constant spacing in each
    direction) -- true for a simple lat/lon grid, and also true for
    NBM's native Lambert Conformal Conic grid *in its own x/y space*
    (we'd reproject to lon/lat as a separate step -- see the note at
    the bottom of pipeline/polygons.py).

    Attributes
    ----------
    west, north : float
        Lon/lat of the CENTER of the top-left pixel, i.e. values[0, 0].
    dx, dy : float
        Pixel size in the x (longitude) and y (latitude) directions.
        dx should be positive (grid runs west->east).
        dy should be NEGATIVE if row 0 is the northernmost row (the
        conventional "image" orientation).
    """

    west: float
    north: float
    dx: float
    dy: float  # typically negative

    def to_affine(self) -> Affine:
        """
        Build the affine transform mapping pixel (col,row) -> (lon,lat),
        in CORNER-based pixel coordinates: col=0 is the left edge of
        column 0, and its centre is at col=0.5. That is the standard GIS
        geotransform convention (GDAL, rasterio, the affine package's own
        documentation), which is why it stays that way -- a transform
        handed to any of those tools has to mean what they think it means.

        It is NOT the convention numpy/skimage use, and that mismatch has
        bitten this codebase twice; use pixel_to_lonlat() /
        lonlat_to_pixel() below for anything living in array index space.
        """
        return Affine(self.dx, 0.0, self.west - self.dx / 2, 0.0, self.dy, self.north - self.dy / 2)

    # -----------------------------------------------------------------
    # ARRAY INDEX SPACE <-> LON/LAT
    #
    # THE ONE PLACE THE HALF-CELL LIVES. There are two pixel coordinate
    # conventions in play and they differ by exactly half a cell:
    #
    #   corner-based (to_affine, GDAL/rasterio): integer = cell EDGE,
    #       so cell i spans [i, i+1] and its centre is at i+0.5.
    #   centre-based (numpy, skimage.measure.find_contours,
    #       skimage.draw.polygon): integer i IS cell i, i.e. its centre.
    #
    # Everything in this project that indexes an array -- contour output,
    # rasterization, every rr/cc pair -- is centre-based, so these two
    # methods are what array-space code should use. Feeding array indices
    # straight into to_affine() (or its inverse) silently displaces the
    # result by half a cell, which is what pipeline/polygons.py did in
    # BOTH directions, in opposite directions: contours came out half a
    # cell northwest and rasterized masks sat half a cell southeast. They
    # partially cancelled wherever one fed the other, which is why it went
    # unnoticed -- ~1.8 km at 0.025 deg / 38.5N.
    # -----------------------------------------------------------------

    def pixel_to_lonlat(self, row: float, col: float) -> tuple[float, float]:
        """
        Array index (row, col) -> (lon, lat). Integer indices are cell
        CENTRES, and fractional ones interpolate between them -- which is
        exactly what find_contours returns, e.g. row=0.5 for an isoline
        running halfway between the centres of rows 0 and 1.
        """
        return self.to_affine() * (col + 0.5, row + 0.5)

    def lonlat_to_pixel(self, lon: float, lat: float) -> tuple[float, float]:
        """
        (lon, lat) -> fractional array index (row, col), the exact inverse
        of pixel_to_lonlat(). Derived from the same affine rather than
        rewritten as arithmetic, so the two cannot drift apart.
        """
        col, row = ~self.to_affine() * (lon, lat)
        return row - 0.5, col - 0.5
