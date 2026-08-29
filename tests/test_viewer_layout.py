"""
tests/test_viewer_layout.py
------------------------------
Structural checks on the viewer's right rail (webapp/static/index.html +
map.js).

There is no browser or DOM library here -- no new frontend dependencies,
by constraint -- so these are not behavioural tests and do not pretend to
be. What they pin is the contract between the two files, which is where a
restructure actually breaks: map.js reaches for elements by id, and a
markup change that renames or drops one fails silently in the console
rather than loudly anywhere a person would see it.

The rail is also the pattern the remaining five hazards (icing,
turbulence, LLWS, surface winds, freezing levels) will follow, so the
shape itself is worth pinning: hazard rows expandable with a disclosure,
non-hazard rows not, exports in one panel rather than repeated per
hazard.
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

STATIC = Path(__file__).resolve().parent.parent / "webapp" / "static"
HTML = (STATIC / "index.html").read_text()
JS = (STATIC / "map.js").read_text()
CSS = (STATIC / "style.css").read_text()

HTML_IDS = set(re.findall(r'\bid="([^"]+)"', HTML))

# The hazards that own adjustors. Everything else in LAYERS is a plain
# visibility toggle.
HAZARD_ROWS = ["ifr", "mtn"]


def test_every_element_map_js_reaches_for_exists_in_the_markup():
    """
    The contract that breaks first and quietest. getElementById on a
    missing id returns null, and the TypeError lands in a console nobody
    has open.
    """
    referenced = set(re.findall(r"getElementById\(['\"]([^'\"]+)['\"]\)", JS))
    # Ids created at runtime rather than authored in the markup.
    runtime_ids = set()
    missing = sorted(referenced - HTML_IDS - runtime_ids)
    assert not missing, f"map.js reaches for ids that index.html does not define: {missing}"


def test_slider_and_reset_ids_survived_the_restructure():
    """
    Presentation-only means exactly that: every control the per-hour
    state machinery drives still exists, under the same id. These are the
    ids makeHourStore's field lists and the reset handlers hard-code, so
    a rename here silently changes slider values and reset semantics --
    the two things the restructure was explicitly not allowed to touch.
    """
    required = [
        # IFR_FIELDS
        "adjust-threshold", "adjust-threshold-val",
        "adjust-radius", "adjust-radius-val",
        "adjust-minarea", "adjust-minarea-val",
        # MTN_FIELDS
        "adjust-mtn-threshold", "adjust-mtn-threshold-val",
        "adjust-mtn-clearance", "adjust-mtn-clearance-val",
        "adjust-mtn-radius", "adjust-mtn-radius-val",
        "adjust-mtn-minarea", "adjust-mtn-minarea-val",
        # per-hazard recompute status
        "adjust-status", "adjust-mtn-status",
        # resets stay in the adjustor, not the export panel
        "adjust-reset", "adjust-reset-all",
        "adjust-mtn-reset", "adjust-mtn-reset-all",
        # visibility toggles
        "toggle-ifr", "toggle-mtn", "toggle-states", "toggle-artcc", "toggle-legacy-mtnobsc",
    ]
    missing = [element_id for element_id in required if element_id not in HTML_IDS]
    assert not missing, f"controls lost in the restructure: {missing}"


def test_resets_are_inside_the_hazard_body_and_exports_are_not():
    """
    Reset is adjustor scope -- it undoes what these sliders did -- so it
    belongs with the sliders. Export is not, so it does not.
    """
    for hazard in HAZARD_ROWS:
        body = _hazard_body(hazard)
        assert "RESET THIS HOUR" in body, f"{hazard}: reset-this-hour left the adjustor"
        assert "RESET ALL HOURS" in body, f"{hazard}: reset-all-hours left the adjustor"
        assert "PGEN" not in body, f"{hazard}: a PGEN button is still inside the adjustor"
        assert "GENERATE" not in body, f"{hazard}: a GENERATE button is still inside the adjustor"


def test_the_old_per_hazard_export_buttons_are_gone():
    """Both panels used to carry their own pair; there is one pair now."""
    for stale in ("adjust-generate", "adjust-pgen", "adjust-mtn-generate", "adjust-mtn-pgen",
                  "generate-status", "generate-mtn-status"):
        assert stale not in HTML_IDS, f"{stale} still in the markup"
        assert f"'{stale}'" not in JS, f"map.js still references {stale}"

    assert HTML.count("DOWNLOAD PGEN") == 1, "expected exactly one PGEN button"
    assert HTML.count("GENERATE GEOJSON") == 1, "expected exactly one generate button"


def test_export_buttons_state_their_scope():
    """
    GENERATE covers the hour on screen and PGEN covers all five, which is
    what they both already did. With the hour checkboxes disabled, the
    panel has to say so somewhere rather than leaving it to be inferred.
    """
    panel = _slice_between(HTML, 'id="export-panel"', "</div>\n  </div>\n</div>")
    assert "hour on screen" in panel and "all hours" in panel, (
        "the export panel does not state which hours each button covers"
    )


def test_export_panel_has_hazard_checkboxes_and_both_buttons():
    for element_id in ("export-panel", "export-hazard-ifr", "export-hazard-mtn",
                       "export-generate", "export-pgen", "export-status"):
        assert element_id in HTML_IDS, f"export panel is missing {element_id}"


def test_hour_checkboxes_are_disabled_and_say_why():
    """
    Per-hour splitting isn't built. The controls may be present, but a
    control that silently ignores its own state is worse than a disabled
    one -- so every hour checkbox is disabled, and the group carries a
    tooltip explaining it.
    """
    group = _slice_between(HTML, 'id="export-hours-group"', "</div>\n\n      <button")
    hour_boxes = re.findall(r'<input[^>]*class="export-hour"[^>]*>', group)
    assert len(hour_boxes) == 5, f"expected F00-F12, found {len(hour_boxes)}"
    for box in hour_boxes:
        assert "disabled" in box, f"hour checkbox is live but per-hour export is not built: {box}"

    assert "title=" in group, "the disabled group should say why it is disabled"
    assert "export-hours-all" in group and "disabled" in group, "the all/none toggle should be disabled too"


def test_hazard_rows_are_expandable_and_other_rows_are_not():
    """
    A hazard row gets a disclosure triangle and an aria-expanded button;
    a boundary row gets neither, so the difference is visible without
    clicking. This is the shape the other five hazards will copy.
    """
    for hazard in HAZARD_ROWS:
        row = _hazard_row(hazard)
        assert 'aria-expanded="false"' in row, f"{hazard} row has no expander (or starts expanded)"
        assert "disclosure" in row, f"{hazard} row has no disclosure affordance"
        assert "aria-controls=" in row, f"{hazard} expander does not name the body it controls"

    for static_row in re.findall(r'<label class="layer-row layer-row-static[^"]*">.*?</label>', HTML, re.S):
        assert "disclosure" not in static_row, f"a non-hazard row has a disclosure triangle: {static_row[:80]}"
        assert "aria-expanded" not in static_row, f"a non-hazard row looks expandable: {static_row[:80]}"


def test_the_checkbox_is_not_inside_the_expander():
    """
    Clicking the checkbox must not expand, and clicking the label must
    not toggle visibility. Nesting the input inside the expander button
    would make one click do both -- and is invalid HTML besides.
    """
    for hazard in HAZARD_ROWS:
        head = _slice_between(_hazard_row(hazard), '<div class="layer-head">', "</div>")
        expander = _slice_between(head, "<button", "</button>")
        assert "<input" not in expander, f"{hazard}: the visibility checkbox is inside the expander button"
        assert "<input" in head, f"{hazard}: the row lost its visibility checkbox"


def test_all_hazard_bodies_start_collapsed():
    for hazard in HAZARD_ROWS:
        body_tag = re.search(rf'<div class="layer-body" id="adjust-{hazard}-body"[^>]*>', HTML)
        assert body_tag, f"{hazard} body not found"
        assert "hidden" in body_tag.group(0), f"{hazard} body does not start collapsed"


def test_rail_collapses_without_browser_storage():
    """
    Session-only by requirement: localStorage isn't available in this
    environment, so the collapsed state lives in the DOM and a reload
    comes back expanded.
    """
    assert "rail-strip" in HTML_IDS and "rail-collapse" in HTML_IDS and "rail-expand" in HTML_IDS
    assert "setRailCollapsed" in JS

    # Checked against CODE, not prose: the comment beside setRailCollapsed
    # explains why localStorage isn't used, and a naive substring search
    # would fail on the explanation itself.
    code = re.sub(r"//[^\n]*", "", re.sub(r"/\*.*?\*/", "", JS, flags=re.S))
    for api in ("localStorage", "sessionStorage"):
        assert api not in code, f"the rail state must not be persisted in {api}"


def test_no_new_frontend_dependencies():
    """Leaflet and the two font/CSS links, as before -- nothing added."""
    external = set(re.findall(r'(?:src|href)="(https?://[^"]+)"', HTML))
    allowed_hosts = {"cdnjs.cloudflare.com", "fonts.googleapis.com", "fonts.gstatic.com"}
    for url in external:
        host = url.split("/")[2]
        assert host in allowed_hosts, f"new external dependency: {url}"
    assert len([u for u in external if "leaflet" in u]) == 2, "expected exactly Leaflet's css + js"


def test_panel_structure_is_documented_for_the_next_five_hazards():
    methods = (Path(__file__).resolve().parent.parent / "docs" / "METHODS.md").read_text()
    assert "HAZARD_PANELS" in methods, "docs/METHODS.md should name the list a new hazard is added to"


# --- helpers ---------------------------------------------------------------

def _slice_between(text, start_marker, end_marker):
    start = text.index(start_marker)
    end = text.index(end_marker, start)
    return text[start:end]


def _hazard_row(hazard):
    return _slice_between(HTML, f'<div class="layer-row" data-hazard="{hazard}">', "<!-- NON-HAZARD"
                          if hazard == "mtn" else f'<div class="layer-row" data-hazard="mtn">')


def _hazard_body(hazard):
    return _slice_between(HTML, f'id="adjust-{hazard}-body"', "</div>\n      </div>")


if __name__ == "__main__":
    import pytest

    sys.exit(pytest.main([__file__, "-v"]))
