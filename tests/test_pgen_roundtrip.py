"""
Round-trip proof: parse each reference sample, re-emit it with pgen_xml,
and diff against the original bytes. If they match, the writer reproduces
NMAP2's format exactly -- including whitespace and numeric widths.
"""
import sys
import difflib
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.pgen_xml import gfa_element, build_product_xml

_FIXTURES = Path(__file__).resolve().parent / "fixtures"

SAMPLES = [
    str(_FIXTURES / "21Z_CV.xml"),
    str(_FIXTURES / "21Z_MT_OBSC.xml"),
]


def reemit(path):
    tree = ET.parse(path)
    root = tree.getroot()
    product = root.find("Product")
    layer = product.find("Layer")
    layer_color_el = layer.find("Color")
    layer_color = (int(layer_color_el.get("red")),
                   int(layer_color_el.get("green")),
                   int(layer_color_el.get("blue")))

    blocks = []
    for gfa in layer.find("DrawableElement").findall("Gfa"):
        colors = gfa.findall("Color")
        elem_color = (int(colors[0].get("red")),
                      int(colors[0].get("green")),
                      int(colors[0].get("blue")))
        points = [(p.get("Lat"), p.get("Lon")) for p in gfa.findall("Point")]
        blocks.append(gfa_element(
            points=points,
            hazard=gfa.get("hazard"),
            fcst_hr=gfa.get("fcstHr"),
            tag=gfa.get("tag"),
            wx_type=gfa.get("type"),
            cycle_hour=gfa.get("cycleHour"),
            desk=gfa.get("desk"),
            lat_text=gfa.get("latText"),
            lon_text=gfa.get("lonText"),
            color=elem_color,
        ))

    return build_product_xml(blocks,
                             center=product.get("center"),
                             layer_color=layer_color)


failed = 0
for path in SAMPLES:
    original = open(path, encoding="utf-8").read()
    produced = reemit(path)

    name = path.rsplit("/", 1)[-1]
    if original == produced:
        n = produced.count("<Gfa ")
        print("PASS  %-20s byte-identical (%d Gfa elements, %d bytes)"
              % (name, n, len(produced)))
    else:
        failed += 1
        print("FAIL  %s" % name)
        diff = difflib.unified_diff(
            original.splitlines(), produced.splitlines(),
            "original", "produced", lineterm="", n=1)
        for i, line in enumerate(diff):
            if i > 25:
                print("      ... diff truncated")
                break
            print("      " + line)

sys.exit(1 if failed else 0)
