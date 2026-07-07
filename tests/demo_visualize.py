"""
demo_visualize.py
------------------
Not a real unit test -- just a quick visual sanity check so a human can
SEE that grid_to_polygons() is doing something sensible, rather than
just trusting assert statements. Produces:
  - demo_output.png   (side-by-side: raw grid vs. extracted polygons)
  - demo_output.geojson (the actual GeoJSON we'd hand to a web map)
"""

import json
import sys
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
from matplotlib.patches import Polygon as MplPolygon

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.polygons import GridSpec, grid_to_polygons, polygons_to_feature_collection
from test_polygons import make_fake_ifr_probability_grid

values = make_fake_ifr_probability_grid()
grid = GridSpec(west=-110.0, north=45.0, dx=0.02, dy=-0.02)
threshold = 70.0

polygons = grid_to_polygons(
    values, grid, threshold=threshold, min_area_deg2=0.02, simplify_tolerance_deg=0.02
)

lons = grid.west + np.arange(values.shape[1]) * grid.dx
lats = grid.north + np.arange(values.shape[0]) * grid.dy
extent = [lons[0], lons[-1], lats[-1], lats[0]]

fig, axes = plt.subplots(1, 2, figsize=(13, 5.5))

im = axes[0].imshow(values, extent=extent, origin="upper", cmap="Blues", vmin=0, vmax=100)
axes[0].set_title(f"Input: synthetic 'probability of IFR ceiling' grid\n(2 real blobs + 40 single-pixel noise spikes)")
axes[0].set_xlabel("longitude")
axes[0].set_ylabel("latitude")
plt.colorbar(im, ax=axes[0], label="probability (%)", shrink=0.8)

axes[1].imshow(values, extent=extent, origin="upper", cmap="Greys", vmin=0, vmax=100, alpha=0.35)
for poly in polygons:
    x, y = poly.exterior.xy
    axes[1].add_patch(MplPolygon(list(zip(x, y)), closed=True, facecolor="orangered", edgecolor="darkred", alpha=0.6, linewidth=1.5))
axes[1].set_xlim(extent[0], extent[1])
axes[1].set_ylim(extent[2], extent[3])
axes[1].set_title(f"Output: {len(polygons)} polygon(s) where probability >= {threshold:.0f}%\n(noise filtered out, vertices simplified)")
axes[1].set_xlabel("longitude")
axes[1].set_ylabel("latitude")

plt.tight_layout()
plt.savefig("tests/demo_output.png", dpi=130)
print("Saved tests/demo_output.png")

fc = polygons_to_feature_collection(
    polygons,
    properties={"hazard": "IFR", "threshold_pct": threshold, "valid_time": "2026-07-07T18:00:00Z"},
)
with open("tests/demo_output.geojson", "w") as f:
    json.dump(fc, f, indent=2)
print("Saved tests/demo_output.geojson")
print(f"\n{len(polygons)} polygon(s), total vertices: {sum(len(p.exterior.coords) for p in polygons)}")
