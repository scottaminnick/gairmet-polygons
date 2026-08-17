"""
pgen_xml.py -- emit NMAP2 PGEN product XML containing Gfa elements.

This is NOT the IWXXM/USWX G-AIRMET schema. It is NMAP2's own product
generation serialization (Products/Product/Layer/DrawableElement/Gfa), which
is what the dev-server XML->VG parser consumes.

Written to byte-match the 21Z_CV.xml / 21Z_MT_OBSC.xml reference samples,
including their (slightly odd) whitespace and numeric field widths. Some of
those quirks are almost certainly irrelevant to a real XML parser, but
matching them costs nothing and removes a variable.

NUMERIC FORMATTING (reverse-engineered from the samples)
--------------------------------------------------------
    sizeScale        "%5.1f"        ->  "  1.0"
    latText/lonText  "%7.2f"        ->  "  47.84" / " -75.77" / "-117.61"
    Point Lat/Lon    "% .6f"        ->  " 47.443737" / "-119.970589"

Note the space-flag on Point coords: positive values get a leading space,
negative values use that slot for the minus sign. Python's "% .6f" /
f"{v: .6f}" does exactly this.

WHITESPACE QUIRK
----------------
The reference files use TWO spaces before these attributes:
    lonText, desk, cycleHour, beginning, fzlTopBottom, fzlRange, dueto,
    frequency
and one space elsewhere. Reproduced here verbatim.
"""

# Attribute order is fixed, matching the reference samples exactly.
# The bool in each tuple is "preceded by two spaces instead of one".
_GFA_ATTR_ORDER = [
    ("pgenCategory",  False),
    ("pgenType",      False),
    ("lineWidth",     False),
    ("sizeScale",     False),
    ("smoothFactor",  False),
    ("closed",        False),
    ("filled",        False),
    ("fillPattern",   False),
    ("latText",       False),
    ("lonText",       True),
    ("hazard",        False),
    ("fcstHr",        False),
    ("tag",           False),
    ("desk",          True),
    ("issueType",     False),
    ("cycleDay",      False),
    ("cycleHour",     True),
    ("area",          False),
    ("rg",            False),
    ("beginning",     True),
    ("ending",        False),
    ("states",        False),
    ("type",          False),
    ("top",           False),
    ("bottom",        False),
    ("coverage",      False),
    ("fzlTopBottom",  True),
    ("level",         False),
    ("fzlRange",      True),
    ("contour",       False),
    ("severity",      False),
    ("dueto",         True),
    ("speed",         False),
    ("intensity",     False),
    ("category",      False),
    ("frequency",     True),
    ("isOutlook",     False),
    ("textVor",       False),
]

# Defaults for every attribute. Only a handful ever vary per polygon.
_GFA_DEFAULTS = {
    "pgenCategory": "MET",
    "pgenType": "GFA",
    "lineWidth": "2",
    "sizeScale": "  1.0",
    "smoothFactor": "0",
    "closed": "true",
    "filled": "false",
    "fillPattern": "SOLID",
    "issueType": "NRML",
    "cycleDay": "",
    "area": "", "rg": "", "beginning": "", "ending": "", "states": "",
    "top": "", "bottom": "", "coverage": "", "fzlTopBottom": "",
    "level": "", "fzlRange": "", "contour": "", "severity": "",
    "dueto": "", "speed": "", "intensity": "", "category": "",
    "frequency": "", "isOutlook": "false", "textVor": "",
}

# Element outline colors observed in the samples, by hazard.
HAZARD_COLORS = {
    "IFR":     (255, 0, 0),      # red
    "MT_OBSC": (255, 255, 0),    # yellow
}
LABEL_COLOR = (255, 255, 255)    # white; second <Color> child, both samples
LAYER_COLOR = (255, 255, 0)      # <Layer> color, yellow in both samples


def fmt_size(v):
    return "%5.1f" % float(v)


def fmt_text_coord(v):
    """latText / lonText -- 7-char fixed width, 2 decimals."""
    return "%7.2f" % float(v)


def fmt_point(v):
    """Point Lat / Lon -- space-flagged, 6 decimals, variable width."""
    return "% .6f" % float(v)


def _color_tag(rgb, indent):
    r, g, b = rgb
    return '%s<Color red="%d" green="%d" blue="%d" alpha="255"/>' % (
        indent, r, g, b)


def gfa_element(points, hazard, fcst_hr, tag, wx_type, cycle_hour,
                desk="E", lat_text=None, lon_text=None, color=None,
                indent="          "):
    """
    Build one <Gfa> block.

    points     : sequence of (lat, lon), decimal degrees, west negative.
    hazard     : "IFR" or "MT_OBSC"
    fcst_hr    : 0, 3, 6, 9, 12  (as int or str)
    tag        : polygon identifier -- see README note on tag stability
    wx_type    : e.g. "CIG BLW 010/VIS BLW 3SM PCPN/BR"
                      "MTNS OBSC BY CLDS/PCPN/BR"
    cycle_hour : "21" etc.
    lat_text / lon_text : label anchor. Defaults to the polygon centroid,
                 which is a sane starting point the forecaster can drag.
    """
    if len(points) < 3:
        raise ValueError("a closed GFA polygon needs at least 3 points")

    lats = [float(p[0]) for p in points]
    lons = [float(p[1]) for p in points]

    if lat_text is None:
        lat_text = sum(lats) / len(lats)
    if lon_text is None:
        lon_text = sum(lons) / len(lons)

    attrs = dict(_GFA_DEFAULTS)
    attrs.update({
        "latText": fmt_text_coord(lat_text),
        "lonText": fmt_text_coord(lon_text),
        "hazard": str(hazard),
        "fcstHr": str(fcst_hr),
        "tag": str(tag),
        "desk": str(desk),
        "cycleHour": str(cycle_hour),
        "type": str(wx_type),
    })

    parts = []
    for name, double_space in _GFA_ATTR_ORDER:
        sep = "  " if double_space else " "
        parts.append('%s%s="%s"' % (sep, name, attrs[name]))

    if color is None:
        color = HAZARD_COLORS.get(hazard, (255, 255, 255))

    lines = ["%s<Gfa%s>" % (indent, "".join(parts))]
    lines.append(_color_tag(color, indent + "  "))
    lines.append(_color_tag(LABEL_COLOR, indent + "  "))
    for la, lo in zip(lats, lons):
        lines.append('%s<Point Lat="%s" Lon="%s"/>'
                     % (indent + "  ", fmt_point(la), fmt_point(lo)))
    lines.append("%s</Gfa>" % indent)
    return "\n".join(lines)


def build_product_xml(gfa_blocks, center="OAX", layer_color=LAYER_COLOR):
    """
    Wrap a list of <Gfa> block strings in the Product/Layer scaffolding.

    center: "OAX" in the reference samples, which looks like a template
            leftover (that is Omaha, not KKCI). Left configurable -- see
            the open question in chat before changing it.
    """
    out = []
    out.append('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>')
    out.append('  <Products xmlns:ns2="http://www.example.org/productType">')
    out.append('    <Product name="Default" type="Default" '
               'forecaster="Default" center="%s" status="UNKNOWN" '
               'onOff="true" useFile="true" filePath="" fileName="">' % center)
    out.append('      <Layer name="Default" onOff="true" monoColor="false" '
               'filled="false">')
    out.append(_color_tag(layer_color, "        "))
    out.append('        <DrawableElement>')
    out.extend(gfa_blocks)
    out.append('        </DrawableElement>')
    out.append('      </Layer>')
    out.append('    </Product>')
    out.append('  </Products>')
    return "\n".join(out) + "\n"


def write_product(path, gfa_blocks, **kwargs):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(build_product_xml(gfa_blocks, **kwargs))
