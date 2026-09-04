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
        "adjust-mtn-relief", "adjust-mtn-relief-val", "adjust-mtn-relief-area",
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


# --- The mountainous relief threshold -------------------------------------
#     A slider like the others, but the only one whose effect a forecaster
#     cannot judge from the map alone -- it changes how much of the CONUS
#     the hazard is allowed to claim before any weather is consulted -- so
#     the panel carries a live figure next to it.

def test_relief_slider_covers_the_requested_range_at_its_current_default():
    slider = re.search(r'<input type="range" id="adjust-mtn-relief"[^>]*>', HTML)
    assert slider, "the RELIEF slider is missing from the MTN OBSC adjustor"
    attrs = dict(re.findall(r'(\w+)="([^"]+)"', slider.group(0)))
    assert (attrs["min"], attrs["max"], attrs["step"]) == ("500", "5000", "250")
    assert attrs["value"] == "500", (
        "RELIEF must default to the pipeline's current 500 ft so nothing moves "
        "until a forecaster moves it"
    )


def test_relief_sits_directly_above_clearance():
    """
    Panel order is upstream-first: relief decides what counts as
    mountainous at all, and clearance, radius and min-area act on what it
    admits. Reading them out of that order invites tuning clearance
    against a mask that is itself wrong.
    """
    body = _hazard_body("mtn")
    order = re.findall(r'id="adjust-mtn-(threshold|relief|clearance|radius|minarea)"', body)
    assert order == ["threshold", "relief", "clearance", "radius", "minarea"], order


def test_the_mountainous_area_figure_is_next_to_the_slider_with_its_references():
    """
    The number is meaningless without something to compare it against, so
    the legacy broad-brush total and CONUS land area travel with it.
    """
    body = _hazard_body("mtn")
    relief_row = body.index('id="adjust-mtn-relief"')
    area_row = body.index('id="adjust-mtn-relief-area"')
    clearance_row = body.index('id="adjust-mtn-clearance"')
    assert relief_row < area_row < clearance_row, "the area figure is not attached to RELIEF"
    assert "1.20M" in body and "3.12M" in body, (
        "the area figure should carry its reference points (legacy ~1.20M, CONUS land ~3.12M)"
    )


def test_relief_is_wired_through_the_same_machinery_as_every_other_slider():
    """
    Per-hour state, live recompute and both resets are all driven off
    MTN_FIELDS and the shared slider wiring, so being in those two lists
    IS being wired up -- there is no separate path to forget.
    """
    fields = _slice_between(JS, "const MTN_FIELDS = [", "];")
    assert "'adjust-mtn-relief'" in fields, "RELIEF is not in MTN_FIELDS (no per-hour state, no reset)"
    assert "'mountainous_relief_ft'" in fields, "RELIEF is not mapped to its GeoJSON property"
    assert "['adjust-mtn-relief', 'adjust-mtn-relief-val']" in JS, "the RELIEF slider has no input handler"
    assert "mountainous_relief_ft=${relief}" in JS, "recompute does not send the relief threshold"


def test_an_absent_property_resets_to_the_default_not_to_the_current_slider():
    """
    fromProps is the reset path. Every cached snapshot predates
    mountainous_relief_ft, so its fallback branch is the only one relief
    takes -- and falling back to the slider's current value would make
    RESET a no-op for it.
    """
    from_props = _slice_between(JS, "fromProps(props) {", "\n    },")
    assert "defaultValue" in from_props, (
        "fromProps falls back to the live slider value, so RESET cannot restore a "
        "parameter the snapshot does not carry"
    )
    assert "this.read()" not in from_props


def test_markup_defaults_match_the_route_defaults_that_reset_relies_on():
    """
    fromProps resets to the markup's default value, which is only correct
    while that value IS the pipeline default. Read out of webapp/main.py's
    source rather than by importing it, so the check does not drag FastAPI
    into the test run.
    """
    import ast

    repo_root = Path(__file__).resolve().parent.parent
    main_py = (repo_root / "webapp" / "main.py").read_text()
    tree = ast.parse(main_py)

    # A route default may be a literal or an imported pipeline constant
    # (mountainous_relief_ft is the latter). Resolve names against the
    # module they come from, so the check follows the value rather than
    # quietly skipping the one parameter that is not a literal.
    def resolve(node):
        if isinstance(node, ast.Constant):
            return float(node.value) if isinstance(node.value, (int, float)) else None
        if isinstance(node, ast.Name):
            return _module_constant(repo_root, node.id)
        return None

    routes = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name not in ("recompute_ifr_snapshot", "recompute_mtn_obsc_snapshot"):
            continue
        args = node.args
        positional = args.posonlyargs + args.args
        pairs = list(zip(positional[len(positional) - len(args.defaults):], args.defaults))
        pairs += [(a, d) for a, d in zip(args.kwonlyargs, args.kw_defaults) if d is not None]
        routes[node.name] = {arg.arg: resolve(default) for arg, default in pairs}

    markup = {
        re.search(r'id="([^"]+)"', tag).group(1): float(re.search(r'value="([^"]+)"', tag).group(1))
        for tag in re.findall(r'<input type="range"[^>]*>', HTML)
    }
    expected = {
        "recompute_ifr_snapshot": {
            "adjust-threshold": "threshold_pct", "adjust-radius": "neighborhood_radius_nm",
            "adjust-minarea": "min_area_sq_mi"},
        "recompute_mtn_obsc_snapshot": {
            "adjust-mtn-threshold": "threshold_pct", "adjust-mtn-relief": "mountainous_relief_ft",
            "adjust-mtn-clearance": "clearance_margin_ft",
            "adjust-mtn-radius": "neighborhood_radius_nm", "adjust-mtn-minarea": "min_area_sq_mi"},
    }
    for route, mapping in expected.items():
        for slider, param in mapping.items():
            assert param in routes[route], f"{route} lost {param}"
            assert markup[slider] == routes[route][param], (
                f"{slider} defaults to {markup[slider]} but {route}'s {param} "
                f"defaults to {routes[route][param]}; RESET would restore the wrong value"
            )


# --- The legacy overlay's wrong-key warning --------------------------------
#     A file keyed `area` instead of `name` is not detectably broken to the
#     browser: it parses, it draws three areas, and only the Central Valley
#     cutout silently loses the styling that says it is a cutout. So the
#     viewer has to say so itself.

def test_the_legacy_overlay_has_somewhere_to_put_a_data_warning():
    assert "legacy-mtnobsc-warning" in HTML_IDS, "no warning element for the legacy overlay"
    tag = re.search(r'<div class="layer-warning[^"]*" id="legacy-mtnobsc-warning"[^>]*>', HTML)
    assert tag, "the warning element is not a .layer-warning"
    assert "hidden" in tag.group(0), "the warning must start hidden -- it is not the normal state"


def test_the_warning_sits_with_the_layer_it_is_about():
    legacy_row = HTML.index('id="toggle-legacy-mtnobsc"')
    warning = HTML.index('id="legacy-mtnobsc-warning"')
    panels_end = HTML.index('id="export-panel"')
    assert legacy_row < warning < panels_end, (
        "the warning should follow the LEGACY MTN OBSC row inside the layers panel"
    )


def test_a_missing_name_is_reported_rather_than_papered_over():
    """
    The old code substituted the string 'legacy area' for a missing name,
    which is what made the failure invisible. Both the styling and the
    tooltip now have to distinguish "absent" from "named".
    """
    assert "checkLegacyFeatureNames" in JS, "nothing checks the legacy features' names"
    assert "|| 'legacy area'" not in JS, (
        "the tooltip still falls back to a plausible-looking label for an unnamed feature"
    )

    check = _slice_between(JS, "function checkLegacyFeatureNames(", "\n}")
    assert "console.error" in check, "a wrong-keyed file should also reach whoever deployed it"
    assert "build_legacy.py" in check, "the warning should say how to fix it"
    assert "hidden = false" in check and "hidden = true" in check, (
        "the warning must both appear and go away again"
    )


def test_a_wrong_keyed_file_does_not_disable_the_layer():
    """
    The geometry is still worth looking at, and a forecaster mid-
    calibration should not lose the overlay over a property name. Absent
    data disables the toggle; wrong-keyed data warns.
    """
    check = _slice_between(JS, "function checkLegacyFeatureNames(", "\n}")
    assert "disableLegacyToggle" not in check, (
        "a naming problem should warn, not disable -- disabling is for a missing file"
    )


# --- Cycle staleness -------------------------------------------------------
#     The panel always showed the loaded cycle honestly; it could not say
#     when that cycle had stopped being the current one. A six-hour-old
#     polygon set looks exactly like a fresh one.

def test_the_info_panel_can_report_a_stale_cycle():
    assert "cycle-staleness" in HTML_IDS, "no staleness indicator in the info panel"
    tag = re.search(r'<div class="panel-warning[^"]*" id="cycle-staleness"[^>]*>', HTML)
    assert tag and "hidden" in tag.group(0), "the staleness warning must start hidden"

    # In the info panel, with the cycle it contradicts -- not in the rail.
    assert HTML.index('id="model-cycle"') < HTML.index('id="cycle-staleness"') < HTML.index('id="right-rail"')


def test_the_expected_cycle_comes_from_the_publish_schedule():
    """
    The schedule moved: runs now start at +1:15 and retry to +3:00, and
    the app normally holds a cycle AHEAD of wall clock (a 15Z package at
    10:15Z). The numbers themselves are pinned against
    pipeline/publish_schedule.py in tests/test_publish_schedule.py; what
    this checks is that map.js still derives the expectation from the
    publish window rather than from the clock.
    """
    assert "const CYCLE_HOURS = [3, 9, 15, 21]" in JS, "the cycle hours no longer match the crons"
    assert "PUBLISH_WINDOW_CLOSE_MINUTES" in JS, (
        "the staleness check no longer knows when the publish window closes"
    )
    assert "NBM_LEAD_OFFSET_HOURS" in JS, (
        "without the lead offset the check compares a package against the NBM cycle that "
        "produced it and calls every healthy state stale"
    )
    assert "PUBLISH_GRACE_MINUTES" not in JS, "the old +90-minute grace is still there"


def test_staleness_is_rechecked_while_the_page_sits_open():
    """A page left open crosses a cycle boundary with no fetch to trigger
    a re-evaluation."""
    assert "STALENESS_RECHECK_MS" in JS and "setInterval" in JS, (
        "the staleness check only runs on load, so an open page never notices"
    )


def test_the_warning_says_what_to_do_about_it():
    """
    The likeliest cause is the reader's own cache, which they can fix; the
    next likeliest is a failed publish, which they cannot. It should say
    both rather than only announcing a problem.
    """
    body = _slice_between(JS, "function updateStalenessIndicator(", "\n}")
    assert "Ctrl-Shift-R" in body, "the warning does not say how to clear a stale cache"
    assert "/api/data/status" in body, "the warning does not point at the publish status"


# --- helpers ---------------------------------------------------------------

def _module_constant(repo_root, name):
    """The value of a module-level `NAME = <number>` anywhere in pipeline/."""
    import ast

    for path in (repo_root / "pipeline").rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in tree.body:
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
                if any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
                    return float(node.value.value)
    raise AssertionError(f"could not resolve the constant {name}")

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
