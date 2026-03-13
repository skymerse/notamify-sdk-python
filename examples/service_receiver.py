#!/usr/bin/env python3

from __future__ import annotations

import json
import os
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from pydantic import ValidationError

from notamify_sdk import SignatureVerificationError, WatcherWebhookEvent, verify_signature

HOST = os.getenv("HOST", "127.0.0.1").strip() or "127.0.0.1"
PORT = int(os.getenv("PORT", "8080"))
WEBHOOK_PATH = os.getenv("NOTAMIFY_WEBHOOK_PATH", "/webhooks/notamify").strip() or "/webhooks/notamify"
WEBHOOK_SECRET = os.getenv("NOTAMIFY_WEBHOOK_SECRET", "").strip()
MAX_BODY_BYTES = 1 << 20

if not WEBHOOK_SECRET:
    raise SystemExit("NOTAMIFY_WEBHOOK_SECRET is required")


def handle_notamify_event(event: WatcherWebhookEvent) -> None:
    # Replace this function with your service logic.
    notam_id = event.notam.id
    category = event.notam.interpretation.category if event.notam.interpretation else "UNKNOWN"
    changed_notam_id = event.change.changed_notam_id if event.change else "-"
    print(
        f"[notamify] kind={event.kind.value} event_id={event.event_id} "
        f"notam_id={notam_id} changed_notam_id={changed_notam_id} category={category}"
    )


class Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        if self.path.split("?", 1)[0] != WEBHOOK_PATH:
            self.send_response(HTTPStatus.NOT_FOUND)
            self.end_headers()
            return

        length = int(self.headers.get("Content-Length", "0"))
        if length > MAX_BODY_BYTES:
            self.send_response(HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            self.end_headers()
            return

        raw_body = self.rfile.read(length)
        signature = self.headers.get("X-Notamify-Signature", "")
        if not signature:
            self.send_response(HTTPStatus.UNAUTHORIZED)
            self.end_headers()
            self.wfile.write(b"missing X-Notamify-Signature")
            return

        try:
            verify_signature(signature, WEBHOOK_SECRET, raw_body)
        except SignatureVerificationError as exc:
            self.send_response(HTTPStatus.UNAUTHORIZED)
            self.end_headers()
            self.wfile.write(f"signature verification failed: {exc}".encode("utf-8"))
            return

        try:
            payload = json.loads(raw_body.decode("utf-8"))
            event = WatcherWebhookEvent.model_validate(payload)
        except json.JSONDecodeError:
            self.send_response(HTTPStatus.BAD_REQUEST)
            self.end_headers()
            self.wfile.write(b"invalid JSON body")
            return
        except ValidationError as exc:
            self.send_response(HTTPStatus.BAD_REQUEST)
            self.end_headers()
            self.wfile.write(f"invalid Notamify webhook payload: {exc}".encode("utf-8"))
            return

        handle_notamify_event(event)
        self.send_response(HTTPStatus.ACCEPTED)
        self.end_headers()

    def log_message(self, fmt: str, *args: object) -> None:
        return


def main() -> None:
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Notamify receiver listening at http://{HOST}:{PORT}{WEBHOOK_PATH}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
