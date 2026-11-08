"""A local fake of the Notamify API and Watcher API for SDK and CLI tests."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit


def sample_notam(notam_id: str = "11111111-1111-1111-1111-111111111111", number: str = "A1234/26", **extra: Any) -> dict:
    notam = {
        "id": notam_id,
        "notam_number": number,
        "notam_type": "N",
        "location": "EPWA",
        "icao_code": "EPWA",
        "classification": "INTL",
        "starts_at": "2026-11-08T10:00:00Z",
        "ends_at": "2026-11-09T10:00:00Z",
        "issued_at": "2026-11-08T09:00:00Z",
        "is_estimated": False,
        "is_permanent": False,
        "message": "RWY 11/29 CLSD",
        "icao_message": None,
        "qcode": "QMRLC",
        "interpretation": {
            "description": "Runway 11/29 is closed.",
            "excerpt": "RWY 11/29 closed",
            "category": "AERODROME",
            "subcategory": "RUNWAY",
            "affected_elements": [
                {"type": "RUNWAY", "identifier": "11/29", "effect": "CLOSED", "details": "Daily 20:00-04:00"}
            ],
            "schedules": [],
            "schedule_description": "Daily 20:00-04:00 UTC",
        },
    }
    notam.update(extra)
    return notam


Route = Callable[["Request"], tuple[int, Any]]


class Request:
    def __init__(self, method: str, path: str, query: dict[str, list[str]], body: Any, headers: dict[str, str]) -> None:
        self.method = method
        self.path = path
        self.query = query
        self.body = body
        self.headers = headers


class FakeAPI:
    """Routes are ``{(METHOD, path): handler}``; every request is recorded."""

    def __init__(self) -> None:
        self.routes: dict[tuple[str, str], Route] = {}
        self.requests: list[Request] = []
        api = self

        class Handler(BaseHTTPRequestHandler):
            def _handle(self, method: str) -> None:
                url = urlsplit(self.path)
                length = int(self.headers.get("Content-Length", "0") or 0)
                raw = self.rfile.read(length) if length else b""
                body = json.loads(raw) if raw else None
                req = Request(method, url.path, parse_qs(url.query), body, dict(self.headers))
                api.requests.append(req)
                route = api.routes.get((method, url.path))
                if route is None:
                    status, payload = 404, {"error": "no route"}
                else:
                    status, payload = route(req)
                data = b"" if payload is None else (payload.encode() if isinstance(payload, str) else json.dumps(payload).encode())
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:
                self._handle("GET")

            def do_POST(self) -> None:
                self._handle("POST")

            def do_PUT(self) -> None:
                self._handle("PUT")

            def do_DELETE(self) -> None:
                self._handle("DELETE")

            def log_message(self, *args: Any) -> None:
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def start(self) -> "FakeAPI":
        self.thread.start()
        return self

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def route(self, method: str, path: str, handler: Route | tuple[int, Any]) -> None:
        self.routes[(method, path)] = handler if callable(handler) else (lambda _req, value=handler: value)

    def last(self, method: str, path: str) -> Request:
        for req in reversed(self.requests):
            if req.method == method and req.path == path:
                return req
        raise AssertionError(f"no {method} {path} request")


def paged_notams(total: int) -> Route:
    """Serve ``total`` NOTAMs, honouring page and per_page."""

    def handler(req: Request) -> tuple[int, Any]:
        page = int(req.query.get("page", ["1"])[0])
        per_page = int(req.query.get("per_page", ["10"])[0])
        start = (page - 1) * per_page
        items = [
            sample_notam(f"00000000-0000-0000-0000-{i:012d}", f"A{i:04d}/26")
            for i in range(start, min(start + per_page, total))
        ]
        return 200, {"notams": items, "total_count": total, "page": page, "per_page": per_page}

    return handler
