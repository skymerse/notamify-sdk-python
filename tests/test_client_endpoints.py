import os
import unittest
from pathlib import Path
from unittest import mock

from notamify_sdk import NotamifyClient
from notamify_sdk.client import APIError

from tests._fake_api import FakeAPI, paged_notams, sample_notam

LISTENER = {
    "id": "l1",
    "name": "ops",
    "webhook_url": "https://example.com/hook",
    "emails": [],
    "filters": {"notam_icao": ["EPWA"], "category": ["AERODROME"]},
    "lifecycle": {"enabled": True, "types": ["CANCELLED"]},
    "metadata": {"notams_shipped": 3},
    "active": True,
    "mode": "prod",
}


class EndpointTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeAPI().start()
        self.client = NotamifyClient(token="t", api_base_url=self.api.url, watcher_base_url=self.api.url)

    def tearDown(self):
        self.api.stop()

    def test_recently_expired(self):
        expired = sample_notam(expired_at="2026-11-08T08:00:00Z", status="INACTIVE", referenced_notam_number="A0001/26")
        self.api.route("GET", "/notams/recently-expired", (200, {"notams": [expired], "total_count": 1, "page": 1, "per_page": 30}))
        result = self.client.get_recently_expired_notams({"location": ["EPWA"], "expired_after": "2026-11-07T00:00:00Z"})
        self.assertEqual(result.notams[0].status, "INACTIVE")
        self.assertEqual(result.notams[0].expired_at.year, 2026)
        self.assertEqual(result.notams[0].referenced_notam_number, "A0001/26")
        query = self.api.last("GET", "/notams/recently-expired").query
        self.assertEqual(query["location"], ["EPWA"])
        self.assertEqual(query["expired_after"], ["2026-11-07T00:00:00Z"])

    def test_recently_expired_pager(self):
        self.api.route("GET", "/notams/recently-expired", paged_notams(35))
        self.assertEqual(len(list(self.client.notams.recently_expired({"location": ["EPWA"]}, per_page=30))), 35)

    def test_get_notam(self):
        notam_id = "11111111-1111-1111-1111-111111111111"
        self.api.route("GET", f"/notams/{notam_id}", (200, sample_notam(notam_id)))
        notam = self.client.get_notam(notam_id)
        self.assertEqual(notam.id, notam_id)
        self.assertEqual(self.api.last("GET", f"/notams/{notam_id}").query, {})

    def test_get_notam_encodes_the_id(self):
        with self.assertRaises(APIError):
            self.client.get_notam("../briefing")
        self.assertEqual(self.api.requests[-1].path, "/notams/..%2Fbriefing")

    def test_airport_briefing(self):
        self.api.route(
            "GET",
            "/notams/briefing/simple",
            (200, {"location": "EPWA", "summary": "### EPWA\n- RWY closed", "notam_ids": ["A1234/26"]}),
        )
        result = self.client.generate_airport_briefing({"location": "EPWA", "type": "detailed"})
        self.assertEqual(result.notam_ids, ["A1234/26"])
        self.assertEqual(self.api.last("GET", "/notams/briefing/simple").query["type"], ["detailed"])

    def test_failed_briefing_is_returned_not_raised(self):
        job = "22222222-2222-2222-2222-222222222222"
        body = {"uuid": job, "status": "failed", "created_at": "2026-11-08T10:00:00Z", "updated_at": "2026-11-08T10:01:00Z"}
        self.api.route("GET", f"/notams/briefing/{job}", (500, body))
        status = self.client.get_briefing_status(job)
        self.assertEqual(status.status.value, "failed")

    def test_server_error_without_job_body_still_raises(self):
        job = "22222222-2222-2222-2222-222222222222"
        self.api.route("GET", f"/notams/briefing/{job}", (500, {"error": "boom"}))
        with self.assertRaises(APIError) as ctx:
            self.client.get_briefing_status(job)
        self.assertEqual(ctx.exception.status, 500)

    def test_patch_listener_keeps_unchanged_fields(self):
        self.api.route("GET", "/listeners", (200, {"listeners": [LISTENER]}))
        self.api.route("PUT", "/listeners/l1", lambda req: (200, {**LISTENER, **req.body}))
        self.client.patch_listener("l1", filters={"notam_icao": ["EPKK"], "category": None}, lifecycle={"enabled": False})
        body = self.api.last("PUT", "/listeners/l1").body
        self.assertEqual(body["name"], "ops")
        self.assertEqual(body["webhook_url"], "https://example.com/hook")
        self.assertEqual(body["filters"], {"notam_icao": ["EPKK"]})
        self.assertEqual(body["lifecycle"], {"enabled": False, "types": ["CANCELLED"]})
        self.assertEqual(body["mode"], "prod")

    def test_get_listener_not_found(self):
        self.api.route("GET", "/listeners", (200, {"listeners": [LISTENER]}))
        with self.assertRaises(APIError) as ctx:
            self.client.get_listener("missing")
        self.assertEqual(ctx.exception.status, 404)


class ClientConfigTests(unittest.TestCase):
    def test_token_and_urls_default_to_environment(self):
        env = {
            "NOTAMIFY_TOKEN": "env-token",
            "NOTAMIFY_API_BASE_URL": "https://api.example.test/v2",
            "NOTAMIFY_CONFIG_FILE": os.devnull + ".missing",
        }
        with mock.patch.dict(os.environ, env):
            client = NotamifyClient()
        self.assertEqual(client.token, "env-token")
        self.assertEqual(client.api_base_url, "https://api.example.test/v2")
        self.assertEqual(client.watcher_base_url, "https://watcher.notamify.com")

    def test_malformed_config_file_does_not_break_explicit_token(self):
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            handle.write("{not json")
        try:
            with mock.patch.dict(os.environ, {"NOTAMIFY_CONFIG_FILE": handle.name, "NOTAMIFY_TOKEN": ""}):
                client = NotamifyClient(token="explicit")
        except BaseException:
            os.unlink(handle.name)
            raise
        self.assertEqual(client.token, "explicit")
        self.assertEqual(client.api_base_url, "https://api.notamify.com/api/v2")
        with mock.patch.dict(os.environ, {"NOTAMIFY_CONFIG_FILE": handle.name, "NOTAMIFY_TOKEN": "env-token"}):
            Path(handle.name).write_text("{not json")
            self.assertEqual(NotamifyClient().token, "env-token")
        os.unlink(handle.name)

    def test_explicit_token_keeps_production_defaults(self):
        with mock.patch.dict(os.environ, {"NOTAMIFY_API_BASE_URL": "https://staging.example.test/v2"}):
            client = NotamifyClient(token="explicit")
        self.assertEqual(client.api_base_url, "https://api.notamify.com/api/v2")

    def test_pager_does_not_stop_on_a_filtered_short_page(self):
        api = FakeAPI().start()
        try:
            def page(req):
                n = int(req.query["page"][0])
                items = [sample_notam(f"00000000-0000-0000-0000-{n:012d}")] if n in (1, 3) else []
                return 200, {"notams": items, "total_count": 75, "page": n, "per_page": 30}

            api.route("GET", "/notams", page)
            client = NotamifyClient(token="t", api_base_url=api.url, watcher_base_url=api.url)
            self.assertEqual(len(list(client.notams.active({"location": ["EGLL"], "category": ["AERODROME"]}, per_page=30))), 2)
        finally:
            api.stop()

    def test_explicit_token_wins(self):
        with mock.patch.dict(os.environ, {"NOTAMIFY_TOKEN": "env-token"}):
            self.assertEqual(NotamifyClient(token="explicit").token, "explicit")


if __name__ == "__main__":
    unittest.main()
