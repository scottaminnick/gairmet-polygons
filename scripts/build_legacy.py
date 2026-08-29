import csv, json, math

PREF = {'US':0,'CA':1}   # prefer US, then Canada, when an ident is reused abroad
nav = {}
for r in csv.DictReader(open('navaids.csv')):
    if not r['type'].startswith('VOR'): continue
    if r['iso_country'] not in PREF: continue
    k = r['ident']
    cand = (PREF[r['iso_country']], float(r['latitude_deg']), float(r['longitude_deg']), r['name'], r['iso_country'])
    if k not in nav or cand[0] < nav[k][0]: nav[k] = cand

# Manual resolutions, flagged in output
nav['PHX'] = (0,) + nav['PXR'][1:]                       # Phoenix VORTAC is filed as PXR
nav['YSC'] = (1, 45.438599, -71.691399, 'Sherbrooke (AIRPORT coords)', 'CA')

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

json.dump({'type':'FeatureCollection','features':feats},
          open('/home/claude/legacy_mtnobsc_boundaries.geojson','w'), indent=1)

for name, notes in audit.items():
    print('='*64); print(name, '(%d pts)' % len(notes))
    for tok, la, lo, note in notes[:4] + [('...','','','')] + notes[-2:]:
        if tok=='...': print('   ...'); continue
        print('  %-12s -> %8.4fN %9.4fW   %s' % (tok, la, lo, note))
