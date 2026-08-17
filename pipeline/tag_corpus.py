"""
tag_corpus.py -- harvest AWC G-AIRMET issuances and test whether tag numbers
are geographically stable.

THE QUESTION THIS ANSWERS
-------------------------
Are tags a stable geographic layout, or minted per cycle?

    Stable    -> tag 2E clusters tightly in one region across cycles.
                 Assign tags by location lookup. Problem collapses.
    Per-cycle -> tag 2E scatters across the desk's area.
                 Assign by clustering + assert the disjointness invariant.

DESIGN NOTE: WHY TWO INPUT MODES
--------------------------------
The AWC Data API may only serve *current* products rather than a historical
range -- verify this before planning a backfill. So this tool accepts either:

    --from-dir   a directory of saved response XML files
    --from-url   a live API endpoint (single fetch)

and always appends to the corpus with deduplication. That way the same code
works for a one-shot backfill (if lookback is supported) or for scheduled
accumulation every 6 hours via GitHub Actions, which you already have the
infrastructure for. If lookback is NOT supported, the accumulate path is your
only option and the corpus grows from today forward.

NO SHAPELY. Centroids use the shoelace formula on plain floats so this stays
dependency-light and safe to run anywhere.

USAGE
-----
    python tag_corpus.py harvest --from-dir ./responses --corpus corpus.jsonl
    python tag_corpus.py analyze --corpus corpus.jsonl
"""

import argparse
import json
import math
import os
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict

FCST_HR_FIELD = (
    "roughly_the_number_of_hours_between_the_issue_time_and_the_valid_time"
)


# ---------------------------------------------------------------------------
# Geometry -- shoelace centroid, no dependencies
# ---------------------------------------------------------------------------
def polygon_centroid(points):
    """
    Area-weighted centroid of a closed ring via the shoelace formula.

    points: [(lon, lat), ...]

    Deliberately NOT the mean of the vertices -- vertex mean is biased toward
    wherever vertices are dense, and the whole analysis hinges on centroid
    position being meaningful. Falls back to vertex mean for degenerate
    (zero-area) rings.
    """
    n = len(points)
    if n < 3:
        return _vertex_mean(points)

    a2 = 0.0   # twice the signed area
    cx = 0.0
    cy = 0.0
    for i in range(n):
        x0, y0 = points[i]
        x1, y1 = points[(i + 1) % n]
        cross = x0 * y1 - x1 * y0
        a2 += cross
        cx += (x0 + x1) * cross
        cy += (y0 + y1) * cross

    if abs(a2) < 1e-12:
        return _vertex_mean(points)

    return (cx / (3.0 * a2), cy / (3.0 * a2))


def _vertex_mean(points):
    if not points:
        return (float("nan"), float("nan"))
    return (sum(p[0] for p in points) / len(points),
            sum(p[1] for p in points) / len(points))


def split_tag(raw):
    """
    '2E' -> (2, 'E').  Confirmed against the API: the tag is the numeric
    tag concatenated with the desk letter, matching PGEN's separate
    tag="2" desk="E" attributes.
    """
    if not raw:
        return (None, "")
    digits = "".join(c for c in raw if c.isdigit())
    letters = "".join(c for c in raw if c.isalpha()).upper()
    return (int(digits) if digits else None, letters)


def due_to_api_to_pgen(due_to):
    """
    API uses spaces where PGEN uses slashes:
        API   'CIG BLW 010 VIS BLW 3SM PCPN BR'
        PGEN  'CIG BLW 010/VIS BLW 3SM PCPN/BR'

    Rule: slash before 'VIS', and the weather tokens trailing '3SM' are
    slash-joined. Returns the input unchanged if it does not match the
    expected shape -- better to pass something through than to mangle it.
    """
    if not due_to:
        return due_to
    toks = due_to.split()
    try:
        i = toks.index("3SM")
    except ValueError:
        return due_to
    head = " ".join(toks[:i + 1])
    wx = toks[i + 1:]
    head = head.replace("CIG BLW 010 VIS", "CIG BLW 010/VIS")
    return head + ("/" + "/".join(wx) if wx else "")


# ---------------------------------------------------------------------------
# Harvest
# ---------------------------------------------------------------------------
def parse_response(path_or_text, is_text=False):
    """Yield one record dict per <GAIRMET> element."""
    root = (ET.fromstring(path_or_text) if is_text
            else ET.parse(path_or_text).getroot())
    data = root.find("data")
    if data is None:
        return

    for g in data.findall("GAIRMET"):
        area = g.find("area")
        pts = []
        if area is not None:
            for p in area.findall("point"):
                try:
                    pts.append((float(p.findtext("longitude")),
                                float(p.findtext("latitude"))))
                except (TypeError, ValueError):
                    continue          # tolerate malformed numerics
        if len(pts) < 3:
            continue

        raw_tag = g.findtext("tag") or ""
        tag_num, desk = split_tag(raw_tag)
        hazard_el = g.find("hazard")
        clon, clat = polygon_centroid(pts)

        yield {
            "issue_time": g.findtext("issue_time"),
            "valid_time": g.findtext("valid_time"),
            "product": g.findtext("product"),
            "raw_tag": raw_tag,
            "tag": tag_num,
            "desk": desk,
            "fcst_hr": _safe_int(g.findtext(FCST_HR_FIELD)),
            "hazard": hazard_el.get("type") if hazard_el is not None else None,
            "due_to": g.findtext("due_to"),
            "due_to_pgen": due_to_api_to_pgen(g.findtext("due_to")),
            "num_points": len(pts),
            "centroid_lon": round(clon, 4),
            "centroid_lat": round(clat, 4),
            "points": [[round(x, 4), round(y, 4)] for x, y in pts],
        }


def _safe_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def record_key(rec):
    """Dedup key. Includes geometry so a re-issued amended polygon counts."""
    pts = ";".join("%.4f,%.4f" % (x, y) for x, y in rec["points"])
    return "|".join(str(rec.get(k)) for k in
                    ("issue_time", "product", "raw_tag", "fcst_hr")) + "|" + \
        str(hash(pts))


def harvest(args):
    existing = set()
    if os.path.exists(args.corpus):
        with open(args.corpus, encoding="utf-8") as fh:
            for line in fh:
                try:
                    existing.add(record_key(json.loads(line)))
                except Exception:
                    continue
    print("corpus already holds %d records" % len(existing))

    new = []
    if args.from_dir:
        files = sorted(f for f in os.listdir(args.from_dir)
                       if f.lower().endswith(".xml"))
        print("reading %d file(s) from %s" % (len(files), args.from_dir))
        for fn in files:
            try:
                for rec in parse_response(os.path.join(args.from_dir, fn)):
                    if record_key(rec) not in existing:
                        new.append(rec)
                        existing.add(record_key(rec))
            except ET.ParseError as e:
                print("  SKIP %s (%s)" % (fn, e))
    elif args.from_url:
        # Kept dependency-free on purpose; requests is not needed for one GET.
        from urllib.request import urlopen
        print("fetching %s" % args.from_url)
        with urlopen(args.from_url, timeout=60) as r:
            text = r.read().decode("utf-8", "replace")
        for rec in parse_response(text, is_text=True):
            if record_key(rec) not in existing:
                new.append(rec)
                existing.add(record_key(rec))
    else:
        sys.exit("need --from-dir or --from-url")

    with open(args.corpus, "a", encoding="utf-8") as fh:
        for rec in new:
            fh.write(json.dumps(rec) + "\n")

    print("appended %d new record(s) -> %s" % (len(new), args.corpus))
    if new:
        hrs = sorted({r["fcst_hr"] for r in new if r["fcst_hr"] is not None})
        print("forecast hours seen: %s" % hrs)
        if hrs and set(hrs) == {0}:
            print("  NOTE: only F00 present. Widen the query to capture "
                  "F03-F12, or the corpus cannot speak to smear behavior.")


# ---------------------------------------------------------------------------
# Analyze
# ---------------------------------------------------------------------------
def haversine_nm(lon1, lat1, lon2, lat2):
    R_NM = 3440.065
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = (math.sin(dp / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2)
    return 2 * R_NM * math.asin(min(1.0, math.sqrt(a)))


def analyze(args):
    recs = []
    with open(args.corpus, encoding="utf-8") as fh:
        for line in fh:
            try:
                recs.append(json.loads(line))
            except Exception:
                continue

    if not recs:
        sys.exit("corpus is empty")

    cycles = sorted({r["issue_time"] for r in recs if r["issue_time"]})
    print("=" * 72)
    print("corpus: %d records, %d distinct issuances" % (len(recs), len(cycles)))
    if cycles:
        print("        %s .. %s" % (cycles[0], cycles[-1]))
    print("=" * 72)

    # Group by (product, desk, tag)
    groups = defaultdict(list)
    for r in recs:
        if r["tag"] is None:
            continue
        groups[(r["product"], r["desk"], r["tag"])].append(r)

    print("\n%-8s %-5s %-5s %6s %7s  %9s  %s"
          % ("PRODUCT", "DESK", "TAG", "N", "CYCLES", "SPREAD_NM", "VERDICT"))
    print("-" * 72)

    verdicts = defaultdict(int)
    for key in sorted(groups, key=lambda k: (str(k[0]), str(k[1]), k[2])):
        product, desk, tag = key
        g = groups[key]
        n_cycles = len({r["issue_time"] for r in g})

        lons = [r["centroid_lon"] for r in g]
        lats = [r["centroid_lat"] for r in g]
        mlon, mlat = sum(lons) / len(lons), sum(lats) / len(lats)
        dists = [haversine_nm(mlon, mlat, x, y) for x, y in zip(lons, lats)]
        # Robust spread: mean distance from the group's mean centroid.
        spread = sum(dists) / len(dists)

        # Thresholds are judgment calls, not physics. A G-AIRMET area is
        # commonly several hundred nm across, so a tag whose centroid stays
        # within ~200nm is plausibly a fixed region; beyond ~600nm it is
        # wandering across the desk.
        if n_cycles < 3:
            verdict = "insufficient"
        elif spread < 200:
            verdict = "STABLE"
        elif spread > 600:
            verdict = "SCATTERED"
        else:
            verdict = "ambiguous"
        verdicts[verdict] += 1

        print("%-8s %-5s %-5s %6d %7d  %9.0f  %s"
              % (product, desk, tag, len(g), n_cycles, spread, verdict))

    print("-" * 72)
    print("summary: " + ", ".join("%s=%d" % kv for kv in sorted(verdicts.items())))

    stable = verdicts.get("STABLE", 0)
    scattered = verdicts.get("SCATTERED", 0)
    print()
    if stable and stable >= 2 * max(scattered, 1):
        print("READ: tags look GEOGRAPHICALLY STABLE. Build a tag->region")
        print("      lookup table from these mean centroids and assign by")
        print("      location. Still assert the disjointness invariant.")
    elif scattered and scattered >= 2 * max(stable, 1):
        print("READ: tags look PER-CYCLE. Do not chase AWC's numbering.")
        print("      Assign provisional tags by geographic clustering and")
        print("      let the disjointness checker be the contract.")
    else:
        print("READ: inconclusive. Most likely the corpus is too small --")
        print("      check the CYCLES column. Accumulate more issuances")
        print("      before committing to a tag-assignment design.")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    h = sub.add_parser("harvest", help="append API responses to the corpus")
    h.add_argument("--from-dir", help="directory of saved response XML")
    h.add_argument("--from-url", help="live API endpoint (single fetch)")
    h.add_argument("--corpus", default="tag_corpus.jsonl")
    h.set_defaults(func=harvest)

    a = sub.add_parser("analyze", help="test tag geographic stability")
    a.add_argument("--corpus", default="tag_corpus.jsonl")
    a.set_defaults(func=analyze)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
