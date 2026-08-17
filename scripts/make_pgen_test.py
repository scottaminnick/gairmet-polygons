#!/usr/bin/env python3
"""
make_pgen_test.py -- build one NMAP2 PGEN XML file from the GeoJSON the
pipeline has already generated, so there is something real to load into the
dev server without waiting on a live cycle.

Reads output/{ifr,mtn_obsc}_f{00,03,06,09,12}.geojson for a single hazard and
emits every polygon as a <Gfa> element via pipeline/pgen_xml.py.

Example:
    python scripts/make_pgen_test.py --hazard IFR --cycle-hour 21 --out 21Z_IFR.xml

NOTE ON TAGS: tag assignment here is a deliberate placeholder. See
assign_naive_tags() -- it is not feature tracking and is meant to be replaced.
"""
import argparse
import json
import math
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from pipeline.pgen_xml import gfa_element, build_product_xml

# The five snapshots the pipeline writes per cycle.
FORECAST_HOURS = (0, 3, 6, 9, 12)

OUTPUT_DIR = REPO_ROOT / "output"

# --hazard values -> (GeoJSON filename stem, hazard string NMAP2 expects).
# The GeoJSON says "MTN_OBSC" but the reference samples say "MT_OBSC"; the
# XML has to use the latter, which is also what pgen_xml's color table keys on.
HAZARDS = {
    "IFR":     {"stem": "ifr",      "geojson_hazard": "IFR"},
    "MT_OBSC": {"stem": "mtn_obsc", "geojson_hazard": "MTN_OBSC"},
}

# Centroid separation under which a polygon inherits the previous hour's tag.
TAG_MATCH_RADIUS_NM = 300.0

_EARTH_RADIUS_NM = 3440.065


def _distance_nm(a, b):
    """Great-circle distance in nautical miles between two (lat, lon) pairs."""
    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    h = (math.sin((lat2 - lat1) / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2)
    return 2 * _EARTH_RADIUS_NM * math.asin(math.sqrt(h))


def _centroid(points):
    """Unweighted mean of the ring vertices -- same convention pgen_xml uses
    for its default latText/lonText label anchor."""
    return (sum(p[0] for p in points) / len(points),
            sum(p[1] for p in points) / len(points))


def assign_naive_tags(centroids_by_hour):
    """
    PLACEHOLDER tag assignment. Deliberately naive -- replace wholesale.

    Walks the forecast hours in order. For each polygon, if its centroid lies
    within TAG_MATCH_RADIUS_NM of a polygon at the PREVIOUS hour, it reuses
    that polygon's tag; otherwise it takes the next unused integer.

    This is NOT feature tracking. Specifically, it does NOT:
      - test overlap, area, or shape similarity (centroid proximity only),
      - do any geographic/named-area lookup,
      - handle splits or merges,
      - resolve contention -- the first previous-hour polygon within range
        wins by input order, and one predecessor may be claimed by several
        successors, so two polygons at the same hour can share a tag,
      - look back further than one hour, so a feature that drops out for a
        single snapshot comes back with a brand-new tag.

    The resulting tags are plausible, not correct. They exist so the NMAP2
    file carries stable-looking identifiers for the forecaster to redraw
    against. Swap this out when real tracking lands; nothing else in this
    script depends on how the tags are chosen.

    centroids_by_hour : list, one entry per forecast hour in order, each a
                        list of (lat, lon) polygon centroids.
    returns           : list of the same shape, holding integer tags.
    """
    tags_by_hour = []
    previous = []          # [(centroid, tag)] carried from the prior hour
    next_tag = 1

    for centroids in centroids_by_hour:
        tags = []
        current = []
        for centroid in centroids:
            tag = None
            for prev_centroid, prev_tag in previous:
                if _distance_nm(centroid, prev_centroid) <= TAG_MATCH_RADIUS_NM:
                    tag = prev_tag         # first match wins -- see docstring
                    break
            if tag is None:
                tag = next_tag
                next_tag += 1
            tags.append(tag)
            current.append((centroid, tag))
        tags_by_hour.append(tags)
        previous = current

    return tags_by_hour


def weather_type_string(hazard, properties):
    """
    Compose the Gfa `type` string the way the reference samples spell it.

    The pipeline's `weather_type` property holds only the trailing cause list
    ("PCPN/BR", "CLDS", ...); the samples prefix it with the hazard's standing
    threshold phrase:

        IFR       "CIG BLW 010/VIS BLW 3SM " + weather_type
        MT_OBSC   "MTNS OBSC BY "            + weather_type

    IFR polygons whose `cause` is "CIG" alone carry no `weather_type` at all
    (visibility never crossed its threshold), so they get the ceiling clause
    on its own -- the samples have no example of this case.
    """
    wx = properties.get("weather_type")

    if hazard == "MT_OBSC":
        return "MTNS OBSC BY %s" % (wx or "CLDS")

    if wx:
        return "CIG BLW 010/VIS BLW 3SM %s" % wx
    return "CIG BLW 010"


def iter_rings(feature):
    """
    Yield each drawable exterior ring of a feature as [(lat, lon), ...].

    A MultiPolygon becomes one ring per part, all sharing the parent
    feature's properties -- the same split pipeline/export_xml.py makes.
    Interior rings (holes) are dropped: a PGEN Gfa is a single closed
    outline with no way to express one.
    """
    geometry = feature["geometry"]
    if geometry["type"] == "Polygon":
        parts = [geometry["coordinates"]]
    elif geometry["type"] == "MultiPolygon":
        parts = geometry["coordinates"]
    else:
        raise ValueError("unsupported geometry type %r" % geometry["type"])

    for part in parts:
        ring = part[0]                     # exterior ring; part[1:] are holes
        # GeoJSON closes a ring by repeating the first vertex, PGEN does not.
        if len(ring) > 1 and ring[0] == ring[-1]:
            ring = ring[:-1]
        # GeoJSON stores [lon, lat]; pgen_xml wants (lat, lon).
        yield [(lat, lon) for lon, lat in ring]


def load_hazard_polygons(hazard):
    """
    Read all five snapshots for one hazard.

    Returns a list, one entry per forecast hour in FORECAST_HOURS order, of
    lists of (forecast_hour, points, properties) tuples.
    """
    stem = HAZARDS[hazard]["stem"]
    by_hour = []

    for fcst_hr in FORECAST_HOURS:
        path = OUTPUT_DIR / ("%s_f%02d.geojson" % (stem, fcst_hr))
        if not path.exists():
            raise SystemExit("missing input: %s" % path)

        with open(path, encoding="utf-8") as fh:
            collection = json.load(fh)

        polygons = []
        for feature in collection.get("features", []):
            for points in iter_rings(feature):
                if len(points) < 3:
                    continue           # pgen_xml rejects degenerate outlines
                polygons.append((fcst_hr, points, feature["properties"]))
        by_hour.append(polygons)

    return by_hour


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Build an NMAP2 PGEN XML file from generated GeoJSON.")
    parser.add_argument("--hazard", required=True, choices=sorted(HAZARDS),
                        help="hazard to export")
    parser.add_argument("--cycle-hour", required=True,
                        help="G-AIRMET cycle hour, e.g. 21")
    parser.add_argument("--desk", default="E", help="desk letter (default: E)")
    parser.add_argument("--out", required=True, help="output XML path")
    args = parser.parse_args(argv)

    # Cycles are 03/09/15/21Z; zero-pad so "9" and "09" produce the same file.
    cycle_hour = args.cycle_hour.strip()
    if cycle_hour.isdigit():
        cycle_hour = "%02d" % int(cycle_hour)

    by_hour = load_hazard_polygons(args.hazard)

    tags_by_hour = assign_naive_tags(
        [[_centroid(points) for _, points, _ in hour] for hour in by_hour])

    blocks = []
    for hour, tags in zip(by_hour, tags_by_hour):
        for (fcst_hr, points, properties), tag in zip(hour, tags):
            blocks.append(gfa_element(
                points=points,
                hazard=args.hazard,
                fcst_hr=fcst_hr,
                tag=tag,
                wx_type=weather_type_string(args.hazard, properties),
                cycle_hour=cycle_hour,
                desk=args.desk,
            ))

    out_path = Path(args.out)
    if out_path.parent != Path(""):
        out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(build_product_xml(blocks))

    print("wrote %s -- %d Gfa elements across %d forecast hours"
          % (out_path, len(blocks), len(FORECAST_HOURS)))
    for fcst_hr, hour, tags in zip(FORECAST_HOURS, by_hour, tags_by_hour):
        print("  f%02d  %2d polygons  tags=%s"
              % (fcst_hr, len(hour), ",".join(str(t) for t in tags) or "-"))


if __name__ == "__main__":
    main()
