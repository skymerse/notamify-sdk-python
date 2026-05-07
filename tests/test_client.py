import json
import threading
import unittest
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from pydantic import ValidationError

from notamify_sdk.client import APIError, NotamifyClient, SDK_VERSION


def _sample_notam(notam_id: str = "n1") -> dict:
    return {
        "id": notam_id,
        "notam_number": "A1234/26",
        "notam_type": "N",
        "location": "KJFK",
        "icao_code": "KJFK",
        "classification": "INTL",
        "starts_at": "2026-02-25T10:00:00Z",
        "ends_at": "2026-02-26T10:00:00Z",
        "issued_at": "2026-02-25T09:00:00Z",
        "is_estimated": False,
        "is_permanent": False,
        "message": "RWY closed",
        "icao_message": None,
        "qcode": "QMRLC",
        "interpretation": {
            "description": "Runway closure",
            "excerpt": "Runway closed",
            "category": "AERODROME",
            "subcategory": "RUNWAY",
            "affected_elements": [],
            "schedules": [],
        },
    }


class _Handler(BaseHTTPRequestHandler):
    last_create_body = {}
    last_update_body = {}
    last_sandbox_body = {}
    notam_page_queries = []

    def do_GET(self):
        url = urlsplit(self.path)
        path = url.path
        query = parse_qs(url.query)

        if path == "/listeners":
            self._send(
                200,
                {
                    "listeners": [
                        {
                            "id": "l1",
                            "name": "listener-1",
                            "webhook_url": "https://x",
                            "emails": ["ops@example.com"],
                            "filters": {"notam_icao": ["KJFK"]},
                            "lifecycle": {"enabled": False, "types": []},
                            "metadata": {"notams_shipped": 7},
                            "team": {"owner": "teammate-1"},
                            "active": True,
                            "mode": "prod",
                            "created_at": "2026-02-25T09:00:00Z",
                            "updated_at": "2026-02-25T10:00:00Z",
                        }
                    ]
                },
            )
            return
        if path == "/webhook-secret":
            self._send(200, {"webhook_secret_masked": "nmf_wh_****"})
            return
        if path == "/webhook-logs":
            self._send(200, {"success": [], "errors": []})
            return

        if path == "/notams":
            if query.get("location") == ["PAGED"]:
                _Handler.notam_page_queries.append(query)
                page = int(query.get("page", ["1"])[0])
                per_page = int(query.get("per_page", ["2"])[0])
                all_notams = [
                    _sample_notam("n-page-1"),
                    _sample_notam("n-page-2"),
                    _sample_notam("n-page-3"),
                    _sample_notam("n-page-4"),
                    _sample_notam("n-page-5"),
                ]
                start = (page - 1) * per_page
                end = start + per_page
                self._send(
                    200,
                    {
                        "notams": all_notams[start:end],
                        "total_count": len(all_notams),
                        "page": page,
                        "per_page": per_page,
                    },
                )
                return
            if query.get("affected_element") and query.get("affected_element") != ["effect:CLOSED,type:RUNWAY"]:
                self._send(400, {"error": "affected_element query serialization is invalid"})
                return
            if "starts_at" in query:
                starts_at = query.get("starts_at", [""])[0]
                if starts_at not in {"2026-02-25T10:00:00Z", "2026-02-25T10:00:00+00:00"}:
                    self._send(400, {"error": "datetime query serialization is invalid"})
                    return
                self._send(
                    200,
                    {"notams": [_sample_notam("n-datetime")], "total_count": 1, "page": 1, "per_page": 30},
                )
                return
            if query.get("location") != ["KJFK", "KLAX"]:
                self._send(400, {"error": "location query serialization is invalid"})
                return
            if query.get("always_include_est") != ["true"]:
                self._send(400, {"error": "bool query serialization is invalid"})
                return
            self._send(200, {"notams": [_sample_notam("n-active")], "total_count": 1, "page": 1, "per_page": 30})
            return

        if path == "/notams/raw":
            self._send(200, {"notams": [_sample_notam("n-raw")], "total_count": 1, "page": 1, "per_page": 30})
            return

        if path == "/notams/nearby":
            if query.get("lat") != ["50.1"] or query.get("lon") != ["20.2"]:
                self._send(400, {"error": "missing lat/lon"})
                return
            self._send(200, {"notams": [_sample_notam("n-nearby")], "total_count": 1, "page": 1, "per_page": 30})
            return

        if path == "/notams/archive":
            if query.get("valid_at") != ["2026-02-20"]:
                self._send(400, {"error": "missing valid_at"})
                return
            self._send(200, {"notams": [_sample_notam("n-archive")], "total_count": 1, "page": 1, "per_page": 30})
            return

        if path == "/notams/briefing/job-1":
            self._send(
                200,
                {
                    "uuid": "job-1",
                    "status": "completed",
                    "created_at": "2026-02-25T10:00:00Z",
                    "updated_at": "2026-02-25T10:01:00Z",
                    "response": {
                        "locations": [
                            {
                                "location": "KJFK",
                                "type": "origin",
                                "starts_at": "2026-02-25T10:00:00Z",
                                "ends_at": "2026-02-25T12:00:00Z",
                                "always_include_est": True,
                            }
                        ],
                        "briefing": {
                            "text": "Briefing text",
                            "critical_operational_restrictions": [
                                {
                                    "location_code": "KJFK",
                                    "location_role": "origin",
                                    "items": ["Runway 04L/22R unavailable"],
                                }
                            ],
                        },
                    },
                },
            )
            return

        self._send(404, {"error": "not found"})

    def do_POST(self):
        url = urlsplit(self.path)
        path = url.path

        if path == "/listeners":
            body = self._body_json()
            _Handler.last_create_body = body
            self._send(
                201,
                {
                    "id": "new",
                    "name": body.get("name", ""),
                    "webhook_url": "https://x",
                    "emails": body.get("emails", []),
                    "filters": body.get("filters", {}),
                    "lifecycle": body.get("lifecycle", {"enabled": False, "types": []}),
                    "metadata": {"notams_shipped": 0},
                    "active": True,
                    "mode": body.get("mode", "prod"),
                    "webhook_secret": "nmf_wh_new_listener",
                    "created_at": "2026-02-25T10:00:00Z",
                    "updated_at": "2026-02-25T10:00:00Z",
                },
            )
            return
        if path == "/listeners/l1/sandbox:send":
            body = self._body_json()
            _Handler.last_sandbox_body = body
            self._send(
                200,
                {
                    "listener_id": "l1",
                    "mode": "sandbox",
                    "notam_id": body.get("notam_id", "SANDBOX-NOTAM"),
                    "sent_at": "2026-02-25T10:00:00Z",
                },
            )
            return
        if path == "/webhook-secret:rotate":
            self._send(200, {"webhook_secret": "nmf_wh_new"})
            return
        if path == "/notams/briefing":
            body = self._body_json()
            if not body.get("locations"):
                self._send(400, {"error": "locations are required"})
                return
            self._send(
                201,
                {
                    "uuid": "job-1",
                    "status": "pending",
                    "created_at": "2026-02-25T10:00:00Z",
                    "updated_at": "2026-02-25T10:01:00Z",
                    "message": "Briefing generation started",
                    "status_url": "/api/v2/notams/briefing/job-1",
                },
            )
            return
        if path == "/notams/prioritisation":
            body = self._body_json()
            if not body.get("notam_id"):
                self._send(400, {"error": "notam_id is required"})
                return
            self._send(
                200,
                {
                    "notams": [
                        {
                            "notam": _sample_notam("n-priority"),
                            "priority": "HIGH",
                            "explanation": "Affects destination runway",
                            "considerations": ["Runway unavailable"],
                        }
                    ],
                    "total_count": 1,
                    "page": 1,
                    "per_page": 30,
                },
            )
            return

        self._send(404, {"error": "not found"})

    def do_PUT(self):
        if self.path == "/listeners/l1":
            body = self._body_json()
            _Handler.last_update_body = body
            self._send(
                200,
                {
                    "id": "l1",
                    "name": body.get("name", ""),
                    "webhook_url": "https://x2",
                    "emails": body.get("emails", []),
                    "filters": body.get("filters", {}),
                    "lifecycle": body.get("lifecycle", {"enabled": False, "types": []}),
                    "metadata": {"notams_shipped": 10},
                    "active": True,
                    "mode": body.get("mode", "prod"),
                    "created_at": "2026-02-25T09:00:00Z",
                    "updated_at": "2026-02-25T10:30:00Z",
                },
            )
            return
        self._send(404, {"error": "not found"})

    def do_DELETE(self):
        if self.path == "/listeners/l1":
            self._send(204, None)
            return
        self._send(404, {"error": "not found"})

    def _body_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def _send(self, status, payload):
        self.send_response(status)
        if payload is not None:
            raw = json.dumps(payload).encode("utf-8")
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        else:
            self.end_headers()

    def log_message(self, fmt, *args):
        return


class ClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        _Handler.last_create_body = {}
        _Handler.last_update_body = {}
        _Handler.last_sandbox_body = {}
        _Handler.notam_page_queries = []

    def test_watcher_methods(self):
        client = NotamifyClient(token="t", watcher_base_url=self.base_url, api_base_url=self.base_url)
        listeners = client.list_listeners()
        self.assertEqual(len(listeners), 1)
        self.assertEqual(listeners[0].metadata.notams_shipped, 7)
        self.assertFalse(listeners[0].lifecycle.enabled)
        self.assertFalse(listeners[0].lifecycle_enabled)
        self.assertEqual(listeners[0].team.owner, "teammate-1")
        self.assertEqual(listeners[0].emails, ["ops@example.com"])

        created = client.create_listener(
            "https://x",
            emails=["created@example.com"],
            mode="sandbox",
            lifecycle={"enabled": True, "types": ["cancelled"]},
        )
        self.assertEqual(created.id, "new")
        self.assertEqual(created.mode.value, "sandbox")
        self.assertTrue(created.lifecycle.enabled)
        self.assertEqual(created.lifecycle.types[0].value, "CANCELLED")
        self.assertTrue(created.lifecycle_enabled)
        self.assertEqual(created.webhook_secret, "nmf_wh_new_listener")
        self.assertEqual(created.emails, ["created@example.com"])
        self.assertEqual(_Handler.last_create_body.get("emails"), ["created@example.com"])
        self.assertNotIn("email", _Handler.last_create_body)
        self.assertEqual(_Handler.last_create_body.get("mode"), "sandbox")
        self.assertEqual(
            _Handler.last_create_body.get("lifecycle"),
            {"enabled": True, "types": ["CANCELLED"]},
        )
        self.assertNotIn("lifecycle_enabled", _Handler.last_create_body)

        updated = client.update_listener("l1", "https://x2", mode="prod", lifecycle_enabled=False)
        self.assertEqual(updated.id, "l1")
        self.assertEqual(updated.mode.value, "prod")
        self.assertFalse(updated.lifecycle.enabled)
        self.assertFalse(updated.lifecycle_enabled)
        self.assertEqual(_Handler.last_update_body.get("mode"), "prod")
        self.assertEqual(_Handler.last_update_body.get("lifecycle"), {"enabled": False})
        self.assertNotIn("lifecycle_enabled", _Handler.last_update_body)

        client.delete_listener("l1")
        self.assertTrue(client.get_webhook_secret_masked().startswith("nmf_wh_"))
        self.assertTrue(client.rotate_webhook_secret().startswith("nmf_wh_"))
        self.assertEqual(client.get_webhook_logs().success, [])
        sandbox = client.send_sandbox_message("l1", "SBX-1")
        self.assertEqual(sandbox.listener_id, "l1")
        self.assertEqual(sandbox.mode.value, "sandbox")
        self.assertEqual(sandbox.notam_id, "SBX-1")
        self.assertEqual(_Handler.last_sandbox_body.get("notam_id"), "SBX-1")

    def test_update_listener_preserves_explicit_empty_fields(self):
        client = NotamifyClient(token="t", watcher_base_url=self.base_url, api_base_url=self.base_url)
        client.update_listener("l1", "https://x2", emails=[], name="")
        self.assertIn("emails", _Handler.last_update_body)
        self.assertIn("name", _Handler.last_update_body)
        self.assertEqual(_Handler.last_update_body["emails"], [])
        self.assertEqual(_Handler.last_update_body["name"], "")

    def test_default_user_agent_tracks_sdk_version(self):
        client = NotamifyClient(token="t")
        self.assertEqual(client.user_agent, f"notamify-sdk-python/{SDK_VERSION}")

    def test_notam_methods(self):
        client = NotamifyClient(token="t", watcher_base_url=self.base_url, api_base_url=self.base_url)

        active = client.get_active_notams(
            {
                "location": ["KJFK", "KLAX"],
                "always_include_est": True,
                "affected_element": [{"effect": "closed", "type": "runway"}],
            }
        )
        self.assertEqual(active.notams[0].id, "n-active")

        raw = client.get_raw_notams({"location": ["KJFK"]})
        self.assertEqual(raw.notams[0].id, "n-raw")

        nearby = client.get_nearby_notams({"lat": 50.1, "lon": 20.2})
        self.assertEqual(nearby.notams[0].id, "n-nearby")

        historical = client.get_historical_notams({"valid_at": "2026-02-20"})
        self.assertEqual(historical.notams[0].id, "n-archive")

        briefing = client.create_async_briefing(
            {
                "locations": [
                    {
                        "location": "KJFK",
                        "type": "origin",
                        "starts_at": "2026-02-25T10:00:00Z",
                        "ends_at": "2026-02-25T12:00:00Z",
                    }
                ],
                "aircraft_type": "A320",
            }
        )
        self.assertEqual(briefing.uuid, "job-1")
        self.assertEqual(briefing.status.value, "pending")
        self.assertEqual(briefing.message, "Briefing generation started")

        status = client.get_async_briefing_status("job-1")
        self.assertEqual(status.status.value, "completed")
        self.assertEqual(
            status.response.briefing.critical_operational_restrictions[0].location_code,
            "KJFK",
        )

        prio = client.prioritize_notam(
            {
                "notam_id": "n-priority",
                "locations": [
                    {
                        "location": "KJFK",
                        "type": "origin",
                        "starts_at": "2026-02-25T10:00:00Z",
                        "ends_at": "2026-02-25T12:00:00Z",
                    }
                ],
            }
        )
        self.assertEqual(prio.notams[0].priority.value, "HIGH")

    def test_api_error(self):
        client = NotamifyClient(token="t", watcher_base_url=self.base_url, api_base_url=self.base_url)
        with self.assertRaises(APIError):
            client.delete_listener("missing")

    def test_listener_mode_validation(self):
        client = NotamifyClient(token="t", watcher_base_url=self.base_url, api_base_url=self.base_url)
        with self.assertRaises(ValueError):
            client.create_listener("https://x", mode="invalid")

    def test_datetime_query_serialization(self):
        client = NotamifyClient(token="t", watcher_base_url=self.base_url, api_base_url=self.base_url)
        starts = datetime(2026, 2, 25, 10, 0, 0, tzinfo=timezone.utc)
        result = client.get_active_notams({"starts_at": starts})
        self.assertEqual(result.notams[0].id, "n-datetime")

    def test_notams_resource_iterates_all_items(self):
        client = NotamifyClient(token="t", watcher_base_url=self.base_url, api_base_url=self.base_url)
        pager = client.notams.active({"location": ["PAGED"]}, per_page=2)

        ids = [notam.id for notam in pager]

        self.assertEqual(
            ids,
            ["n-page-1", "n-page-2", "n-page-3", "n-page-4", "n-page-5"],
        )
        self.assertIs(client.notams, client.notams)
        self.assertEqual(
            [query["page"][0] for query in _Handler.notam_page_queries],
            ["1", "2", "3"],
        )
        self.assertTrue(all(query["per_page"] == ["2"] for query in _Handler.notam_page_queries))

    def test_notams_resource_pages_property(self):
        client = NotamifyClient(token="t", watcher_base_url=self.base_url, api_base_url=self.base_url)
        pager = client.notams.active({"location": ["PAGED"]}, per_page=2, max_pages=2)

        pages = list(pager.pages)

        self.assertEqual([page.page for page in pages], [1, 2])
        self.assertEqual([len(page.notams) for page in pages], [2, 2])
        self.assertEqual(
            [query["page"][0] for query in _Handler.notam_page_queries],
            ["1", "2"],
        )

    def test_notams_resource_preserves_query_and_supports_page_overrides(self):
        client = NotamifyClient(token="t", watcher_base_url=self.base_url, api_base_url=self.base_url)
        query = {"location": ["PAGED"], "page": 2, "per_page": 4}

        ids = [
            notam.id
            for notam in client.notams.active(query, per_page=1, max_pages=2)
        ]

        self.assertEqual(ids, ["n-page-2", "n-page-3"])
        self.assertEqual(query, {"location": ["PAGED"], "page": 2, "per_page": 4})
        self.assertEqual(
            [query["page"][0] for query in _Handler.notam_page_queries],
            ["2", "3"],
        )
        self.assertTrue(all(query["per_page"] == ["1"] for query in _Handler.notam_page_queries))

    def test_notams_resource_rejects_invalid_limits(self):
        client = NotamifyClient(token="t", watcher_base_url=self.base_url, api_base_url=self.base_url)

        with self.assertRaises(ValueError):
            client.notams.active({"location": ["PAGED"]}, max_pages=0)

        with self.assertRaises(ValidationError):
            client.notams.active({"location": ["PAGED"]}, per_page=0)

        with self.assertRaises(ValidationError):
            client.notams.active({"location": ["PAGED"]}, per_page=31)

    def test_single_page_notam_calls_reject_per_page_over_30(self):
        client = NotamifyClient(token="t", watcher_base_url=self.base_url, api_base_url=self.base_url)

        with self.assertRaises(ValidationError):
            client.get_active_notams({"location": ["KJFK"], "per_page": 31})


if __name__ == "__main__":
    unittest.main()
