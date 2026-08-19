"""
Round-trip proof: parse each reference sample, re-emit it with pgen_xml,
and diff against the original bytes. If they match, the writer reproduces
NMAP2's format exactly -- including whitespace and numeric widths.
"""
import difflib
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

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


def _diff(original, produced, name):
    """The first 25 lines of a unified diff, for a failure message that says WHAT differs."""
    lines = difflib.unified_diff(
        original.splitlines(), produced.splitlines(), "original", "produced", lineterm="", n=1
    )
    body = []
    for i, line in enumerate(lines):
        if i > 25:
            body.append("      ... diff truncated")
            break
        body.append("      " + line)
    return "%s is not byte-identical:\n%s" % (name, "\n".join(body))


@pytest.mark.parametrize("path", SAMPLES, ids=lambda p: p.rsplit("/", 1)[-1])
def test_reemitted_sample_is_byte_identical(path):
    """
    Parse a reference sample, re-emit it, and require the bytes to match
    exactly -- whitespace and numeric widths included, which is the only
    standard that proves the writer reproduces NMAP2's format.

    Was a module-level loop ending in sys.exit() when this file was a
    standalone script. That SystemExit fired during pytest's COLLECTION
    (import time), which pytest reports as an INTERNALERROR and which
    aborted the entire suite -- every other test file included -- rather
    than failing just this one. Hence a real test function; the
    __main__ block below keeps it runnable as a script.
    """
    original = open(path, encoding="utf-8").read()
    produced = reemit(path)
    assert produced == original, _diff(original, produced, path.rsplit("/", 1)[-1])


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
