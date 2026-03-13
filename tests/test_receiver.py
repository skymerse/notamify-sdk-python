import json
import os
import time
import unittest
from urllib import request, error

from notamify_sdk.receiver import ReceiverConfig, WebhookReceiver
from notamify_sdk.signature import compute_signature


class ReceiverTests(unittest.TestCase):
    def test_receiver_strict_and_dev_mode(self):
        receiver = WebhookReceiver(ReceiverConfig(port=18081, secret="sec", require_signature=True))
        receiver.start()
        try:
            body = b'{"hello": "world"}'
            ts = int(time.time())
            sig = compute_signature("sec", ts, body)
            req = request.Request(
                "http://127.0.0.1:18081/",
                method="POST",
                data=body,
                headers={"Content-Type": "application/json", "X-Notamify-Signature": f"t={ts},v1={sig}"},
            )
            with request.urlopen(req, timeout=2) as resp:
                self.assertEqual(resp.status, 202)
        finally:
            receiver.stop()

        dev_receiver = WebhookReceiver(
            ReceiverConfig(port=18082, secret="", require_signature=True, allow_unsigned_dev=True)
        )
        dev_receiver.start()
        try:
            req = request.Request(
                "http://127.0.0.1:18082/",
                method="POST",
                data=b"{}",
                headers={"Content-Type": "application/json"},
            )
            with request.urlopen(req, timeout=2) as resp:
                self.assertEqual(resp.status, 202)
        finally:
            dev_receiver.stop()

    def test_receiver_rejects_unsigned_when_strict(self):
        receiver = WebhookReceiver(ReceiverConfig(port=18083, secret="sec", require_signature=True))
        receiver.start()
        try:
            req = request.Request(
                "http://127.0.0.1:18083/",
                method="POST",
                data=b"{}",
                headers={"Content-Type": "application/json"},
            )
            with self.assertRaises(error.HTTPError) as ctx:
                request.urlopen(req, timeout=2)
            self.assertEqual(ctx.exception.code, 401)
        finally:
            receiver.stop()

    def test_receiver_accepts_query_string_on_path(self):
        receiver = WebhookReceiver(ReceiverConfig(port=18084, secret="sec", require_signature=True))
        receiver.start()
        try:
            body = b'{"ok": true}'
            ts = int(time.time())
            sig = compute_signature("sec", ts, body)
            req = request.Request(
                "http://127.0.0.1:18084/?dev=1",
                method="POST",
                data=body,
                headers={"Content-Type": "application/json", "X-Notamify-Signature": f"t={ts},v1={sig}"},
            )
            with request.urlopen(req, timeout=2) as resp:
                self.assertEqual(resp.status, 202)
        finally:
            receiver.stop()

    def test_receiver_strict_requires_secret(self):
        receiver = WebhookReceiver(
            ReceiverConfig(port=18085, secret="", require_signature=True, allow_unsigned_dev=False)
        )
        with self.assertRaises(ValueError):
            receiver.start()

    def test_receiver_default_secret_from_env(self):
        os.environ["NOTAMIFY_WEBHOOK_SECRET"] = "env-secret"
        try:
            cfg = ReceiverConfig(port=18086, require_signature=True)
        finally:
            del os.environ["NOTAMIFY_WEBHOOK_SECRET"]
        self.assertEqual(cfg.secret, "env-secret")

    def test_receiver_parses_typed_lifecycle_webhook(self):
        receiver = WebhookReceiver(ReceiverConfig(port=18087, secret="sec", require_signature=True))
        receiver.start()
        try:
            body = json.dumps(
                {
                    "listener_id": "listener-1",
                    "kind": "lifecycle",
                    "event_id": "event-1",
                    "notam": {
                        "id": "n1",
                        "notam_number": "A1234/26",
                        "notam_type": "R",
                        "location": "KJFK",
                        "starts_at": "2026-02-25T10:00:00Z",
                        "ends_at": "2026-02-26T10:00:00Z",
                        "issued_at": "2026-02-25T09:00:00Z",
                        "is_estimated": False,
                        "is_permanent": False,
                        "message": "RWY closed",
                    },
                    "change": {"changed_notam_id": "old-n1", "notam_type": "R"},
                    "sent_at": "2026-03-06T12:00:00Z",
                }
            ).encode("utf-8")
            ts = int(time.time())
            sig = compute_signature("sec", ts, body)
            req = request.Request(
                "http://127.0.0.1:18087/",
                method="POST",
                data=body,
                headers={"Content-Type": "application/json", "X-Notamify-Signature": f"t={ts},v1={sig}"},
            )
            with request.urlopen(req, timeout=2) as resp:
                self.assertEqual(resp.status, 202)

            event = receiver.last_event
            self.assertIsNotNone(event)
            self.assertIsNotNone(event.webhook_event)
            self.assertEqual(event.webhook_event.kind.value, "lifecycle")
            self.assertEqual(event.parse_webhook_event().change.changed_notam_id, "old-n1")
        finally:
            receiver.stop()


if __name__ == "__main__":
    unittest.main()
