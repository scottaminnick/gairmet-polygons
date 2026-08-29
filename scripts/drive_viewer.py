#!/usr/bin/env python3
"""
drive_viewer.py -- click through the viewer's right rail in a real
browser and report what it did.

NOT part of the pytest suite, and deliberately so: it needs Playwright
and a browser, while CI installs only the light dependency set the web
app deploys with. tests/test_viewer_layout.py covers the same rail
structurally, without a browser; this covers the behaviour that only a
browser can show.

It is worth keeping because it earns its place: it caught the rail
"collapsing" into a strip that was still full width and still showing
every panel -- an author `display: flex` rule silently beating the
`hidden` attribute, which no amount of reading the markup would reveal.

    pip install playwright        # browsers are already installed here
    python scripts/drive_viewer.py

Two things are stubbed, neither of them the thing under test:
  - the /api routes, so no pipeline output or cached grid is needed;
  - Leaflet, via scripts/leaflet_stub.js, because the CDN is blocked in
    this sandbox and map.js dies on its first line without it.

Served over HTTP rather than file:// -- the page's fetches are
same-origin relative URLs, which a file:// origin blocks outright, and a
blocked manifest sends the app down its no-cached-grid fallback instead
of the path being tested.
"""
import functools
import http.server
import json
import socketserver
import sys
import tempfile
import threading
from pathlib import Path

from playwright.sync_api import sync_playwright

REPO_ROOT = Path(__file__).resolve().parent.parent
STATIC = str(REPO_ROOT / "webapp" / "static")
STUB = REPO_ROOT / "scripts" / "leaflet_stub.js"

MANIFEST = {
    "model_cycle": "2026-08-21T21:00:00Z",
    "nbm_source_cycle": "2026-08-21T18:00:00Z",
    "snapshots": [
        {"requested_forecast_hour": h, "actual_forecast_hour": h, "substituted": False}
        for h in (0, 3, 6, 9, 12)
    ],
}
FC = {
    "type": "FeatureCollection",
    "features": [{
        "type": "Feature",
        "geometry": {"type": "Polygon", "coordinates": [[[-100, 40], [-98, 40], [-98, 42], [-100, 42], [-100, 40]]]},
        "properties": {"hazard": "IFR", "threshold_pct": 50, "neighborhood_radius_nm": 50,
                       "min_area_sq_mi": 3000, "clearance_margin_ft": 500,
                       "valid_time": "2026-08-21T21:00:00Z", "cause": "CIG"},
    }],
}

handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=STATIC)
socketserver.TCPServer.allow_reuse_address = True
server = socketserver.TCPServer(("127.0.0.1", 0), handler)
PORT = server.server_address[1]
threading.Thread(target=server.serve_forever, daemon=True).start()

results = []
def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{'  -- ' + detail if detail else ''}")

with sync_playwright() as p:
    browser = p.chromium.launch(executable_path="/opt/pw-browsers/chromium-1194/chrome-linux/chrome")
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    console_errors = []
    page_errors = []
    page.on("console", lambda m: console_errors.append(f"{m.text} @ {m.location.get('url', '')}")
            if m.type == "error" else None)
    page.on("pageerror", lambda e: page_errors.append(str(e)))

    def route_api(route):
        url = route.request.url
        if "/manifest" in url:
            route.fulfill(status=200, content_type="application/json", body=json.dumps(MANIFEST))
        elif "/boundaries/legacy_mtnobsc" in url:
            route.fulfill(status=404, content_type="application/json", body='{"detail":"absent"}')
        elif "/boundaries/" in url:
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"type": "FeatureCollection", "features": []}))
        elif "format=xml" in url:
            route.fulfill(status=200, content_type="application/xml", body="<Products/>")
        elif "/pgen" in url:
            route.fulfill(status=200, content_type="application/json", body=json.dumps(
                {"xml": "<Products/>", "filename": "x.xml", "sidecar": {"total_gfa_elements": 7},
                 "sidecar_filename": "x.json"}))
        else:
            route.fulfill(status=200, content_type="application/json", body=json.dumps(FC))

    page.route("**/api/**", route_api)
    # Leaflet's CDN is blocked by policy in this sandbox, and map.js dies
    # on its first line without it -- so serve a minimal stub instead.
    page.route("**/leaflet.min.js", lambda r: r.fulfill(
        status=200, content_type="application/javascript",
        body=STUB.read_text()))
    page.route("**/leaflet.min.css", lambda r: r.fulfill(status=200, content_type="text/css", body=""))
    page.route("**/fonts.googleapis.com/**", lambda r: r.fulfill(status=200, content_type="text/css", body=""))
    page.route("**/basemaps.cartocdn.com/**", lambda r: r.abort())
    page.goto(f"http://127.0.0.1:{PORT}/index.html")
    page.wait_for_timeout(1200)

    # --- 1. default state: all collapsed ---
    check("hazard bodies start collapsed",
          page.locator("#adjust-ifr-body").is_hidden() and page.locator("#adjust-mtn-body").is_hidden())

    # --- 2. clicking the label expands; only one open at a time ---
    page.click("#expand-ifr")
    check("clicking the IFR label expands it", page.locator("#adjust-ifr-body").is_visible())
    check("aria-expanded follows", page.get_attribute("#expand-ifr", "aria-expanded") == "true")

    page.click("#expand-mtn")
    check("opening MTN closes IFR",
          page.locator("#adjust-mtn-body").is_visible() and page.locator("#adjust-ifr-body").is_hidden())

    page.click("#expand-mtn")
    check("clicking again collapses", page.locator("#adjust-mtn-body").is_hidden())

    # --- 3. checkbox independence, both directions ---
    page.click("#expand-ifr")
    before = page.is_checked("#toggle-ifr")
    page.click("#toggle-ifr")
    check("checkbox click does not collapse the open body", page.locator("#adjust-ifr-body").is_visible())
    check("checkbox click still toggles visibility", page.is_checked("#toggle-ifr") != before)
    check("hidden + expanded is a legal state",
          not page.is_checked("#toggle-ifr") and page.locator("#adjust-ifr-body").is_visible())
    page.click("#toggle-ifr")  # restore

    # --- 4. non-hazard rows are not expandable ---
    states_row = page.locator('label.layer-row-static:has(#toggle-states)')
    check("static row has no expander", states_row.locator("button").count() == 0)
    page.click("#toggle-states")
    check("static row click toggles its checkbox", not page.is_checked("#toggle-states"))
    page.click("#toggle-states")

    # --- 5. slider values and per-hour state survive ---
    page.fill("#adjust-threshold", "70")
    page.dispatch_event("#adjust-threshold", "input")
    page.wait_for_timeout(600)
    check("slider label follows the slider", page.inner_text("#adjust-threshold-val") == "70")
    page.click("text=F06")
    page.wait_for_timeout(800)
    page.click("text=F00")
    page.wait_for_timeout(800)
    check("per-hour setting survives an hour round trip",
          page.input_value("#adjust-threshold") == "70",
          f"got {page.input_value('#adjust-threshold')}")

    # --- 6. export panel ---
    check("export hazard boxes default to visible layers",
          page.is_checked("#export-hazard-ifr") and page.is_checked("#export-hazard-mtn"))
    page.click("#toggle-mtn")
    page.wait_for_timeout(100)
    check("export box follows visibility until edited", not page.is_checked("#export-hazard-mtn"))
    page.click("#toggle-mtn")
    page.wait_for_timeout(100)
    check("export box follows visibility back", page.is_checked("#export-hazard-mtn"))
    page.click("#export-hazard-mtn")           # user edit pins it
    page.click("#toggle-mtn"); page.wait_for_timeout(100)
    check("edited export box stops following", not page.is_checked("#export-hazard-mtn"))
    page.click("#toggle-mtn")

    check("hour checkboxes are disabled", page.locator(".export-hour[disabled]").count() == 5)
    check("all/none toggle is disabled", page.is_disabled("#export-hours-all"))

    with page.expect_download() as dl:
        page.click("#export-generate")
    check("GENERATE downloads a file", dl.value.suggested_filename.endswith((".geojson", ".xml")),
          dl.value.suggested_filename)
    page.wait_for_timeout(400)

    with page.expect_download() as dl2:
        page.click("#export-pgen")
    check("PGEN downloads a file", dl2.value.suggested_filename.endswith((".xml", ".json")),
          dl2.value.suggested_filename)
    page.wait_for_timeout(400)

    # --- 7. whole-rail collapse ---
    rail_before = page.locator("#rail-panels").bounding_box()["width"]
    page.click("#rail-collapse")
    page.wait_for_timeout(200)
    strip = page.locator("#rail-strip").bounding_box()
    check("collapse hides the panels", page.locator("#rail-panels").is_hidden())
    check("collapsed strip is narrow", strip["width"] < 60, f'{rail_before:.0f}px -> {strip["width"]:.0f}px')
    page.click("#rail-expand")
    page.wait_for_timeout(200)
    check("expand restores the panels", page.locator("#rail-panels").is_visible())

    # --- 8. no console errors anywhere in that run ---
    # Uncaught exceptions are the signal that matters -- a broken handler
    # shows up here and nowhere else. (Resource 404s from the bare test
    # server, e.g. favicon, are not interesting.)
    check("no uncaught JS exceptions", not page_errors, "; ".join(page_errors[:3]))
    ignorable = ("carto", "tile", "favicon", "404")
    noisy = [e for e in console_errors if not any(w in e.lower() for w in ignorable)]
    check("no unexplained console errors", not noisy, "; ".join(noisy[:3]))

    SHOT = str(Path(tempfile.mkdtemp(prefix="viewer-shots-")))
    print(f"\nscreenshots: {SHOT}")
    page.reload(); page.wait_for_timeout(1200)      # clean slate for the screenshots
    page.screenshot(path=f"{SHOT}/rail_collapsed_rows.png")
    page.click("#expand-ifr"); page.wait_for_timeout(250)
    page.screenshot(path=f"{SHOT}/rail_open.png")
    page.click("#rail-collapse"); page.wait_for_timeout(250)
    page.screenshot(path=f"{SHOT}/rail_strip.png")

    # --- 9. the no-cached-grid path, which used to hide whole panels ---
    page2 = browser.new_page(viewport={"width": 1440, "height": 900})
    # 500, not 503: fetchIfrManifestWhenReady deliberately RETRIES 503 for
    # two minutes (cold start), so a 503 here would just be waiting.
    page2.route("**/api/**", lambda r: r.fulfill(status=500, content_type="application/json",
                                                 body='{"detail":"broken"}'))
    page2.route("**/leaflet.min.js", lambda r: r.fulfill(
        status=200, content_type="application/javascript", body=STUB.read_text()))
    page2.route("**/leaflet.min.css", lambda r: r.fulfill(status=200, content_type="text/css", body=""))
    page2.route("**/fonts.googleapis.com/**", lambda r: r.fulfill(status=200, content_type="text/css", body=""))
    page2.route("**/basemaps.cartocdn.com/**", lambda r: r.abort())
    page2.goto(f"http://127.0.0.1:{PORT}/index.html")
    page2.wait_for_timeout(1500)
    check("fallback: hazard expanders are disabled", page2.is_disabled("#expand-ifr"))
    check("fallback: export controls are disabled", page2.is_disabled("#export-generate"))
    check("fallback: visibility checkboxes still work", not page2.is_disabled("#toggle-ifr"))
    page2.screenshot(path=f"{SHOT}/rail_fallback.png")

    browser.close()
    server.shutdown()

failed = [r for r in results if not r[1]]
print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
sys.exit(1 if failed else 0)
