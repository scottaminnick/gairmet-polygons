#!/usr/bin/env python3
"""
build_legacy.py -- rebuild data/boundaries/legacy_mtnobsc.json from the
forecaster's legacy MTN OBSC VOR strings.

The strings in AREAS below are the source of truth; everything else here
just resolves them to coordinates. See data/boundaries/LEGACY_MTNOBSC.md
for what the resulting geometry is and is not worth -- in particular the
TRUE-vs-magnetic question on the offset bearings, which this script
resolves as TRUE.

THE NAVAID TABLE is OurAirports' open navaid dump, not vendored here (it
is ~1.5 MB and changes upstream):

    curl -O https://raw.githubusercontent.com/davidmegginson/ourairports-data/main/navaids.csv

Pass its path with --navaids. tests/fixtures/navaids_subset.csv holds a
verbatim subset of the rows these three strings need, which is what the
reproducibility test runs against.

Usage:
    python scripts/build_legacy.py --navaids navaids.csv
    python scripts/build_legacy.py --navaids navaids.csv --out /tmp/check.json

Output is byte-for-byte reproducible from a given navaid table, which
tests/test_legacy_mtnobsc_overlay.py asserts against the committed file.
"""
import argparse
import csv, json, math
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = REPO_ROOT / "data" / "boundaries" / "legacy_mtnobsc.json"
DEFAULT_NAVAIDS = Path("navaids.csv")

PREF = {'US':0,'CA':1}   # prefer US, then Canada, when an ident is reused abroad

def load_navaids(path):
    nav = {}
    with open(path, newline='') as fh:
        for r in csv.DictReader(fh):
            if not r['type'].startswith('VOR'): continue
            if r['iso_country'] not in PREF: continue
            k = r['ident']
            cand = (PREF[r['iso_country']], float(r['latitude_deg']), float(r['longitude_deg']), r['name'], r['iso_country'])
            if k not in nav or cand[0] < nav[k][0]: nav[k] = cand

    # Manual resolutions, flagged in output
    nav['PHX'] = (0,) + nav['PXR'][1:]                   # Phoenix VORTAC is filed as PXR
    nav['YSC'] = (1, 45.438599, -71.691399, 'Sherbrooke (AIRPORT coords)', 'CA')
    return nav

nav = {}

COMPASS = {'N':0,'NNE':22.5,'NE':45,'ENE':67.5,'E':90,'ESE':112.5,'SE':135,'SSE':157.5,
           'S':180,'SSW':202.5,'SW':225,'WSW':247.5,'W':270,'WNW':292.5,'NW':315,'NNW':337.5}
R_NM = 3440.065

def destination(lat, lon, brg_deg, dist_nm):
    """Spherical great-circle destination. TRUE bearing."""
    d = dist_nm / R_NM
    b = math.radians(brg_deg)
    p1, l1 = math.radians(lat), math.radians(lon)
    p2 = math.asin(math.sin(p1)*math.cos(d) + math.cos(p1)*math.sin(d)*math.cos(b))
    l2 = l1 + math.atan2(math.sin(b)*math.sin(d)*math.cos(p1), math.cos(d)-math.sin(p1)*math.sin(p2))
    return math.degrees(p2), (math.degrees(l2)+540)%360-180

def resolve(tok):
    t = tok.strip().replace(' ', '_')
    if '_' in t:
        head, ident = t.split('_', 1)
        i = 0
        while i < len(head) and head[i].isdigit(): i += 1
        dist, dirn = int(head[:i]), head[i:].upper()
        if dirn not in COMPASS: raise ValueError('bad direction in %r' % tok)
        if ident not in nav: raise ValueError('unknown VOR %r' % ident)
        _, la, lo, name, _ = nav[ident]
        y, x = destination(la, lo, COMPASS[dirn], dist)
        return x, y, '%d nm %s of %s (%s)' % (dist, dirn, ident, name)
    if t not in nav: raise ValueError('unknown VOR %r' % t)
    _, la, lo, name, _ = nav[t]
    return lo, la, '%s (%s)' % (t, name)

AREAS = {
 'Appalachians': "70NW_PQI, MLT, CON, HAR, 30N_GSO, CLT, ATL, GQO, 50WSW_LOZ, HNN, EWC, JHW, SYR, MSS, YSC, 70NW_PQI",
 'Rockies': "TOU, HUH, 40S_YQL, GTF, HVR, SHR, CYS, TBE, CME, 60W_INK, 70WNW_DLF, 90SSE_MRF, ELP, 60SSE_SSO, 50S_TUS, PHX, EED, 30E_HEC, 30S_HEC, 55W_BZA, MZB, LAX, 40W_RZS, PYE, FOT, 70WNW_OED, ONP, HQM, TOU",
 'CentralValleyCutout': "RBL, 10ENE_EHF, 30SSE_EHF, 40W_EHF, 20NNE_OAK, RBL",
}

def build():
    feats, audit = [], {}
    for name, s in AREAS.items():
        ring, notes = [], []
        for tok in s.split(','):
            x, y, note = resolve(tok)
            ring.append([round(x,5), round(y,5)]); notes.append((tok.strip(), round(y,4), round(x,4), note))
        if ring[0] != ring[-1]: ring.append(ring[0])
        audit[name] = notes
        feats.append({'type':'Feature',
                      'properties':{'name':name,'source':'legacy NMAP MTN OBSC boundary',
                                    'bearing_datum':'TRUE (unverified - see notes)'},
                      'geometry':{'type':'Polygon','coordinates':[ring]}})
    return {'type':'FeatureCollection','features':feats}, audit


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--navaids', type=Path, default=DEFAULT_NAVAIDS,
                    help='OurAirports navaids.csv (see the URL in this file\'s header); '
                         'default: navaids.csv in the working directory')
    ap.add_argument('--out', type=Path, default=DEFAULT_OUT,
                    help='where to write the GeoJSON; default: %(default)s')
    ap.add_argument('--quiet', action='store_true', help='skip the per-vertex audit')
    args = ap.parse_args()

    if not args.navaids.exists():
        raise SystemExit(
            f'navaid table not found: {args.navaids}\n'
            'Download it with:\n'
            '  curl -O https://raw.githubusercontent.com/davidmegginson/ourairports-data/main/navaids.csv'
        )

    nav.update(load_navaids(args.navaids))
    fc, audit = build()

    args.out.parent.mkdir(parents=True, exist_ok=True)
    # Trailing newline so the file is byte-identical to what is committed
    # (and so it ends like every other text file in the repo); json.dump
    # does not write one.
    with open(args.out, 'w') as fh:
        json.dump(fc, fh, indent=1)
        fh.write('\n')
    print('wrote %s (%d features)' % (args.out, len(fc['features'])))

    if args.quiet: return
    for name, notes in audit.items():
        print('='*64); print(name, '(%d pts)' % len(notes))
        for tok, la, lo, note in notes[:4] + [('...','','','')] + notes[-2:]:
            if tok=='...': print('   ...'); continue
            print('  %-12s -> %8.4fN %9.4fW   %s' % (tok, la, lo, note))


if __name__ == '__main__':
    main()
