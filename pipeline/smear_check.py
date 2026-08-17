"""
smear_check.py -- test the tag disjointness invariant.

MODEL BEING TESTED
------------------
A G-AIRMET cycle contains two smear windows that share F06:

    BODY    = union(F00, F03, F06)   -> the TAC AIRMET valid period
    OUTLOOK = union(F06, F09, F12)   -> the legacy TAC Outlook

Claim: within EACH window independently, the smear unions for different
tags must not overlap. Tags are geographic, so spatially separate areas
get separate tags, and the smear is what must stay disjoint.

If this holds on real forecaster-drawn output, the invariant is confirmed
and can be asserted on generated first-guess polygons.

NOTE ON GEOMETRY: treating lat/lon as planar. Fine for a yes/no overlap
test at CONUS scale -- we are not measuring area, just asking whether two
shapes intersect. A tiny shared edge is not a real violation, so overlaps
below MIN_OVERLAP_DEG2 are reported separately as touches.
"""
import xml.etree.ElementTree as ET
from collections import defaultdict
from itertools import combinations

from shapely.geometry import Polygon
from shapely.ops import unary_union

WINDOWS = {
    "BODY":    ["0", "3", "6"],
    "OUTLOOK": ["6", "9", "12"],
}

# Shared boundaries / slivers are not real violations.
MIN_OVERLAP_DEG2 = 1e-6


def load(path):
    """
    Return {tag: {fcstHr: [Polygon, ...]}}.

    NOTE: the value is a LIST, not a single Polygon. Confirmed against the
    AWC API, where tag 2E carried two separate polygons at the same
    forecast hour -- multi-part smears are legal. Keying one polygon per
    (tag, fcstHr) silently drops geometry and can turn a real overlap into
    a false pass.
    """
    root = ET.parse(path).getroot()
    de = root.find("Product").find("Layer").find("DrawableElement")

    by_tag = defaultdict(lambda: defaultdict(list))
    for gfa in de.findall("Gfa"):
        pts = [(float(p.get("Lon")), float(p.get("Lat")))
               for p in gfa.findall("Point")]
        poly = Polygon(pts)
        if not poly.is_valid:
            poly = poly.buffer(0)          # repair self-intersections
        by_tag[gfa.get("tag")][gfa.get("fcstHr")].append(poly)
    return by_tag


def check(path):
    name = path.rsplit("/", 1)[-1]
    print("=" * 68)
    print(name)
    print("=" * 68)

    by_tag = load(path)

    for win_name, hours in WINDOWS.items():
        smears = {}
        for tag, snaps in by_tag.items():
            parts = []
            for h in hours:
                parts.extend(snaps.get(h, []))     # flatten multi-part
            if parts:
                smears[tag] = unary_union(parts)

        present = sorted(smears, key=int)
        print("\n  %-8s hours %-12s tags present: %s"
              % (win_name, ",".join("F%02d" % int(h) for h in hours),
                 ", ".join(present) or "(none)"))

        if len(present) < 2:
            print("           nothing to compare")
            continue

        violations, touches = [], []
        for a, b in combinations(present, 2):
            inter = smears[a].intersection(smears[b])
            if inter.is_empty:
                continue
            if inter.area > MIN_OVERLAP_DEG2:
                violations.append((a, b, inter.area))
            else:
                touches.append((a, b))

        for a, b in touches:
            print("           tags %s/%s touch (sliver, not a violation)"
                  % (a, b))

        if violations:
            for a, b, area in violations:
                print("           VIOLATION tags %s/%s overlap %.4f deg^2"
                      % (a, b, area))
        else:
            print("           OK -- all %d smears pairwise disjoint"
                  % len(present))

    # Which window does each tag live in? This is the structural signature.
    print("\n  tag -> snapshots present")
    for tag in sorted(by_tag, key=int):
        hrs = sorted(by_tag[tag], key=int)
        nparts = sum(len(by_tag[tag][h]) for h in hrs)
        in_body = any(h in WINDOWS["BODY"] for h in hrs)
        in_out = any(h in WINDOWS["OUTLOOK"] for h in hrs)
        where = ("body+outlook" if in_body and in_out
                 else "body only" if in_body else "outlook only")
        print("    tag %-3s F%-22s %s"
              % (tag, ",".join(hrs), where), "(%d polys)" % nparts)
    print()


for p in ["/mnt/user-data/uploads/21Z_CV.xml",
          "/mnt/user-data/uploads/21Z_MT_OBSC.xml"]:
    check(p)
