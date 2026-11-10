"""
tests/asgi_client.py
------------------------
One GET straight through an ASGI app, for the tests that drive the real
webapp stack (tests/test_cache_headers.py, tests/test_tango_routes.py).

Deliberately NOT fastapi.testclient, which needs httpx -- not in
requirements.txt, which is what CI installs (see tests/test_artifacts.py
for the same constraint).

Shared, because the two copies drifted: one kept a fake receive() that
answered http.request on every call, which no server does. starlette
1.10's FileResponse listens for the client's disconnect while it streams,
so BaseHTTPMiddleware rejected the second http.request ("Unexpected message
received: http.request") and every FileResponse route failed in the tests
while the app served them correctly under uvicorn.
"""

import asyncio


def asgi_get(app, path):
    """
    Send one GET through `app`. Returns (status, headers, body), headers
    lower-cased, body the raw bytes of the whole response.

    receive() behaves like a real server (uvicorn): the request body once,
    then nothing until the response is finished, then a disconnect.
    """
    raw_path, _, query = path.partition("?")
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": "GET", "scheme": "http", "path": raw_path, "raw_path": raw_path.encode(),
        "query_string": query.encode(), "root_path": "",
        "headers": [(b"host", b"testserver")],
        "client": ("127.0.0.1", 12345), "server": ("testserver", 80),
    }
    captured = {"status": None, "headers": {}, "body": b""}

    async def run():
        request_sent = False
        response_done = asyncio.Event()

        async def receive():
            nonlocal request_sent
            if not request_sent:
                request_sent = True
                return {"type": "http.request", "body": b"", "more_body": False}
            await response_done.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            if message["type"] == "http.response.start":
                captured["status"] = message["status"]
                captured["headers"] = {
                    k.decode().lower(): v.decode() for k, v in message["headers"]
                }
            elif message["type"] == "http.response.body":
                captured["body"] += message.get("body", b"")
                if not message.get("more_body", False):
                    response_done.set()

        await app(scope, receive, send)

    asyncio.run(run())
    return captured["status"], captured["headers"], captured["body"]
