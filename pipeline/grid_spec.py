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
        """Build the affine transform mapping pixel (col,row) -> (lon,lat)."""
        return Affine(self.dx, 0.0, self.west - self.dx / 2, 0.0, self.dy, self.north - self.dy / 2)
