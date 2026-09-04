"""
tests/test_cache_headers.py
------------------------------
Every response this app serves must say how long it may be cached.

None of them did. Browsers are permitted to guess freshness from
Last-Modified when Cache-Control is absent (RFC 9111 5.2), so they cached
the data, and a forecaster saw a six-hour-old cycle in a normal window
while raw.githubusercontent and a private window both had the current one.
The server was right the whole time, which is what made it hard to spot:
a stale polygon set is pixel-for-pixel as plausible as a current one.

Two classes, checked separately because they want different answers:

  /api/*  must be no-store. The data changes every six hours and a
          recompute is keyed on slider values in the query string.

  the app must be no-cache/must-revalidate, NOT no-store -- StaticFiles
          sends an ETag, so revalidation costs a 304 with no body. The
          failure this prevents is worse than stale data: last week's
          map.js against this morning's data, silently disagreeing about
          what a property is called.

DRIVEN THROUGH THE REAL ASGI APP, not by reading the source: the point is
that the middleware is wired into the stack and reaches every route,
including error responses. Deliberately NOT via fastapi.testclient, which
needs httpx -- not in requirements.txt, which is what CI installs (see
tests/test_artifacts.py for the same constraint).
"""

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from webapp.main import NO_STORE, REVALIDATE, app  # noqa: E402

# One per route family the viewer actually fetches. Query strings are
# included where the real client sends them, since a recompute's response
# depends on them entirely.
DATA_PATHS = [
    "/api/hazards/ifr/manifest",
    "/api/hazards/mtn_obsc/manifest",
    "/api/hazards/ifr",
    "/api/hazards/ifr/00",
    "/api/hazards/mtn_obsc/00",
    "/api/hazards/ifr/00/recompute?threshold_pct=50&neighborhood_radius_nm=50&min_area_sq_mi=3000",
    "/api/hazards/mtn_obsc/00/recompute?threshold_pct=50&mountainous_relief_ft=500",
    "/api/boundaries/states",
    "/api/boundaries/artcc",
    "/api/boundaries/legacy_mtnobsc",
    "/api/hazards/demo",
    "/api/health",
    "/api/data/status",
]

APP_PATHS = ["/", "/index.html", "/map.js", "/style.css"]


def _request(path):
    """Send one GET through the app and return (status, headers)."""
    raw_path, _, query = path.partition("?")
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": "GET", "scheme": "http", "path": raw_path, "raw_path": raw_path.encode(),
        "query_string": query.encode(), "root_path": "",
        "headers": [(b"host", b"testserver")],
        "client": ("127.0.0.1", 12345), "server": ("testserver", 80),
    }
    captured = {}

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        if message["type"] == "http.response.start":
            captured["status"] = message["status"]
            captured["headers"] = {
                k.decode().lower(): v.decode() for k, v in message["headers"]
            }

    asyncio.run(app(scope, receive, send))
    return captured.get("status"), captured.get("headers", {})


@pytest.mark.parametrize("path", DATA_PATHS)
def test_data_routes_are_never_stored(path):
    status, headers = _request(path)
    directive = headers.get("cache-control")

    assert directive is not None, (
        f"{path} sends no Cache-Control, so a browser may cache it on its own "
        f"heuristics -- which is exactly how a six-hour-old cycle stayed on screen"
    )
    assert "no-store" in directive, f"{path} sends {directive!r}, which permits storing"
    assert "no-cache" in directive and "must-revalidate" in directive, (
        f"{path} sends {directive!r}"
    )
    assert directive == NO_STORE, f"{path} sends {directive!r}, expected {NO_STORE!r}"


@pytest.mark.parametrize("path", APP_PATHS)
def test_the_front_end_revalidates_rather_than_being_assumed_fresh(path):
    status, headers = _request(path)
    assert status == 200, f"{path} returned {status}"
    directive = headers.get("cache-control")

    assert directive is not None, (
        f"{path} sends no Cache-Control -- a forecaster can end up running cached "
        f"front-end code against freshly fetched data"
    )
    assert "no-cache" in directive and "must-revalidate" in directive, (
        f"{path} sends {directive!r}"
    )
    assert directive == REVALIDATE


def test_the_front_end_is_revalidated_not_re_downloaded():
    """
    no-cache, not no-store, and the distinction is the whole reason the
    front end is treated differently: StaticFiles sends an ETag, so a
    revalidation is a 304 with no body. Making these no-store would
    re-download the app on every navigation for no benefit.
    """
    for path in APP_PATHS:
        _status, headers = _request(path)
        assert "no-store" not in headers.get("cache-control", ""), (
            f"{path} is no-store; it only needs to revalidate"
        )
        assert headers.get("etag"), (
            f"{path} has no ETag, so no-cache means a full re-download every time"
        )


def test_error_responses_are_not_cacheable_either():
    """
    A cached 404 or 503 outlives the outage that caused it. These come
    from the exception handler rather than a route, so they only carry the
    header if the middleware genuinely wraps the whole stack.
    """
    for path in ("/api/hazards/ifr/99", "/api/boundaries/nope"):
        status, headers = _request(path)
        assert status >= 400, f"{path} unexpectedly returned {status}"
        assert headers.get("cache-control") == NO_STORE, (
            f"error response for {path} sends {headers.get('cache-control')!r}"
        )


def test_every_api_route_the_app_declares_is_covered():
    """
    DATA_PATHS is hand-written, so it goes stale the moment a route is
    added. This walks what the app actually declares and fails on anything
    the list does not exercise -- the seven-hazard version of this app will
    have three times as many routes.
    """
    declared = set()
    for route in app.routes:
        path = getattr(route, "path", "")
        if path.startswith("/api/") and "GET" in (getattr(route, "methods", None) or set()):
            declared.add(path)

    # Reduce the tested paths back to their route templates.
    import re

    def normalise(path):
        raw = path.split("?")[0]
        # Literal routes first: /api/hazards/ifr/manifest is its own route
        # AND matches /api/hazards/ifr/{fxx}, and a set has no useful
        # order, so matching the wildcard first would report the literal
        # one as untested at random.
        if raw in declared:
            return raw
        for template in sorted(declared, key=lambda t: (t.count("{"), -len(t))):
            pattern = "^" + re.sub(r"\{[^}]+\}", "[^/]+", template) + "$"
            if re.match(pattern, raw):
                return template
        return raw

    covered = {normalise(p) for p in DATA_PATHS}
    missing = sorted(declared - covered)
    assert not missing, f"GET routes under /api/ with no cache-header test: {missing}"


def test_the_directives_themselves_did_not_get_weakened():
    """The constants are the contract; max-age must stay at zero."""
    assert "no-store" in NO_STORE and "max-age=0" in NO_STORE
    assert "no-store" not in REVALIDATE
    assert "must-revalidate" in REVALIDATE and "max-age=0" in REVALIDATE


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
