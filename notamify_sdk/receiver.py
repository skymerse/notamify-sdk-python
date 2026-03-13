from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import urlsplit

from pydantic import ValidationError

from .models import WatcherWebhookEvent
from .signature import SignatureVerificationError, verify_signature

_MAX_BODY = 1 << 20


@dataclass
class ReceiverConfig:
    host: str = "127.0.0.1"
    port: int = 8080
    path: str = "/"
    secret: str = field(default_factory=lambda: os.getenv("NOTAMIFY_WEBHOOK_SECRET", "").strip())
    tolerance_seconds: int = 600
    require_signature: bool = True
    allow_unsigned_dev: bool = False


@dataclass
class ReceivedEvent:
    body: dict[str, Any]
    raw_body: bytes
    signature_verified: bool
    webhook_event: WatcherWebhookEvent | None = None

    def parse_webhook_event(self) -> WatcherWebhookEvent:
        if self.webhook_event is not None:
            return self.webhook_event
        parsed = WatcherWebhookEvent.model_validate(self.body)
        self.webhook_event = parsed
        return parsed


class WebhookReceiver:
    def __init__(
        self,
        cfg: ReceiverConfig,
        on_event: Callable[[ReceivedEvent], None] | None = None,
    ) -> None:
        self.cfg = cfg
        self.on_event = on_event
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._last_event: ReceivedEvent | None = None

    @property
    def last_event(self) -> ReceivedEvent | None:
        with self._lock:
            return self._last_event

    def start(self) -> None:
        if self._server is not None:
            return
        if self.cfg.require_signature and not self.cfg.allow_unsigned_dev and not self.cfg.secret.strip():
            raise ValueError(
                "NOTAMIFY_WEBHOOK_SECRET (or ReceiverConfig.secret) is required when strict signature verification is enabled"
            )

        parent = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                if urlsplit(self.path).path != parent.cfg.path:
                    self.send_response(HTTPStatus.NOT_FOUND)
                    self.end_headers()
                    return

                length = int(self.headers.get("Content-Length", "0"))
                if length > _MAX_BODY:
                    self.send_response(HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
                    self.end_headers()
                    return

                raw = self.rfile.read(min(length, _MAX_BODY + 1))
                if len(raw) > _MAX_BODY:
                    self.send_response(HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
                    self.end_headers()
                    return

                header = self.headers.get("X-Notamify-Signature", "")
                verified = False
                if parent.cfg.require_signature:
                    if not header:
                        if not parent.cfg.allow_unsigned_dev:
                            self.send_response(HTTPStatus.UNAUTHORIZED)
                            self.end_headers()
                            return
                    elif not parent.cfg.secret:
                        self.send_response(HTTPStatus.UNAUTHORIZED)
                        self.end_headers()
                        return
                    else:
                        try:
                            verified = verify_signature(
                                header,
                                parent.cfg.secret,
                                raw,
                                tolerance_seconds=parent.cfg.tolerance_seconds,
                            )
                        except SignatureVerificationError:
                            self.send_response(HTTPStatus.UNAUTHORIZED)
                            self.end_headers()
                            return

                try:
                    body = json.loads(raw.decode("utf-8"))
                except json.JSONDecodeError:
                    self.send_response(HTTPStatus.BAD_REQUEST)
                    self.end_headers()
                    return

                webhook_event = None
                try:
                    webhook_event = WatcherWebhookEvent.model_validate(body)
                except ValidationError:
                    webhook_event = None

                event = ReceivedEvent(
                    body=body,
                    raw_body=raw,
                    signature_verified=verified,
                    webhook_event=webhook_event,
                )
                with parent._lock:
                    parent._last_event = event
                if parent.on_event is not None:
                    parent.on_event(event)

                self.send_response(HTTPStatus.ACCEPTED)
                self.end_headers()

            def do_GET(self) -> None:
                if urlsplit(self.path).path != parent.cfg.path:
                    self.send_response(HTTPStatus.NOT_FOUND)
                    self.end_headers()
                    return
                event = parent.last_event
                if event is None:
                    self.send_response(HTTPStatus.NO_CONTENT)
                    self.end_headers()
                    return
                payload = {
                    "body": event.body,
                    "signature_verified": event.signature_verified,
                }
                if event.webhook_event is not None:
                    payload["webhook_event"] = event.webhook_event.model_dump(mode="json")
                raw = json.dumps(payload).encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, fmt: str, *args: object) -> None:
                return

        self._server = ThreadingHTTPServer((self.cfg.host, self.cfg.port), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        self._server = None
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None
