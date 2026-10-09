#!/usr/bin/env python3
"""
Build the CONUS area-of-responsibility (AOR) polygon used to gate G-AIRMET first-guess output.

WHY THIS EXISTS
  The forecast area is the U.S. domestic FIR: the 20 CONUS ARTCC FIRs. The FAA ARTCC polygons
  include delegated airspace over Canada (Rainy River MN, western Lake Erie, northern Maine),
  where the Canadian FIR polygons are the correct edge. Naively subtracting the Canadian FIRs
  would also delete U.S. land/water because the FAA FIR polygons are coarse (they swallow
  Caribou ME, Detroit, etc.). So the rule is:

      AOR = ( ARTCC_L union  -  ( Canadian FIRs  -  U.S. territory ) )
            U ( U.S. territory  ∩  buffer(ARTCC_L union, 2 deg) )

  i.e. subtract Canadian FIR airspace everywhere EXCEPT over U.S. territory, and make sure
  U.S. territory (including the U.S. half of the Great Lakes) is never lost.
  Mexico is intentionally NOT subtracted (the FAA ARTCC edge and the Mexican FIR polygon
  disagree along the Rio Grande at the resolution of the available U.S. outline; net effect
  was spill, not improvement).  The seaward (ocean) edge is the FAA ARTCC_L edge, unchanged.

INPUTS (not committed to the repo; both public domain)
  --faa   FAA ADDS "Airspace Boundary" GeoJSON (Airspace_Boundary.geojson)
  --ne    Natural Earth admin-0 countries shapefile (.shp), e.g. ne_110m_admin_0_countries.shp
          (U.S. polygon there includes the U.S. half of the Great Lakes; Canada's side is excluded)
OUTPUT
  --out   GeoJSON FeatureCollection with one MultiPolygon/Polygon feature (EPSG:4326)

Needs: shapely, pyproj, pyshp.
"""
import argparse, json, sys
import shapefile
from shapely.geometry import shape, box, mapping, Polygon
from shapely.ops import unary_union
from pyproj import Geod

CONUS = {"ZAB","ZAU","ZBW","ZDC","ZDV","ZFW","ZHU","ZID","ZJX","ZKC",
         "ZLA","ZLC","ZMA","ZME","ZMP","ZNY","ZOA","ZOB","ZSE","ZTL"}
CONUS_BOX = box(-125.5, 24.0, -66.0, 50.0)
GEOD = Geod(ellps="WGS84")

def sq_mi(g):
    parts = list(g.geoms) if hasattr(g, "geoms") else [g]
    return sum(abs(GEOD.geometry_area_perimeter(p)[0]) for p in parts if p.geom_type == "Polygon") / 2589988.11

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--faa", required=True)
    ap.add_argument("--ne", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    feats = json.load(open(a.faa))["features"]
    artcc = unary_union([shape(f["geometry"]).buffer(0) for f in feats
                         if f["properties"].get("LOCAL_TYPE") == "ARTCC_L"
                         and f["properties"].get("IDENT") in CONUS])
    cz = unary_union([shape(f["geometry"]).buffer(0) for f in feats
                      if f["properties"].get("TYPE_CODE") == "FIR"
                      and str(f["properties"].get("IDENT", "")).startswith("CZ")])
    if artcc.is_empty or cz.is_empty:
        sys.exit("FAA file did not yield ARTCC_L (20 CONUS) and Canadian FIR polygons")

    us = None
    for sr in shapefile.Reader(a.ne).shapeRecords():
        if sr.record.as_dict().get("NAME", sr.record.as_dict().get("name")) in ("United States of America", "United States"):
            us = shape(sr.shape.__geo_interface__).buffer(0)
    if us is None:
        sys.exit("U.S. polygon not found in Natural Earth file")
    us = us.intersection(CONUS_BOX)

    canadian_only = cz.difference(us)
    aor = artcc.difference(canadian_only).union(us.intersection(artcc.buffer(2.0))).buffer(0)

    holes = [sq_mi(Polygon(r)) for p in (aor.geoms if aor.geom_type == "MultiPolygon" else [aor]) for r in p.interiors]
    print(f"ARTCC_L union: {sq_mi(artcc):,.0f} sq mi | AOR: {sq_mi(aor):,.0f} sq mi | "
          f"interior holes: {len(holes)} (largest {max(holes, default=0):,.0f} sq mi)")
    json.dump({"type": "FeatureCollection",
               "features": [{"type": "Feature",
                             "properties": {"name": "CONUS_AOR", "source": "FAA ADDS ARTCC_L + Canadian FIR trim + Natural Earth U.S. territory"},
                             "geometry": mapping(aor)}]}, open(a.out, "w"))

if __name__ == "__main__":
    main()
