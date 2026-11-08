import contextlib
import io
import json
import os
import stat
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock
from urllib import request

from notamify_sdk.cli import build_parser, describe_commands, main
from notamify_sdk.cli.output import parse_time
from notamify_sdk.signature import compute_signature

from tests._fake_api import FakeAPI, paged_notams, sample_notam

JOB = "22222222-2222-2222-2222-222222222222"
LISTENER = {
    "id": "l1",
    "name": "ops",
    "webhook_url": "https://example.com/hook",
    "emails": [],
    "filters": {"notam_icao": ["EPWA"], "category": ["AERODROME"]},
    "lifecycle": {"enabled": False, "types": []},
    "metadata": {"notams_shipped": 3},
    "active": True,
    "mode": "prod",
}


class CLITestCase(unittest.TestCase):
    def setUp(self):
        self.api = FakeAPI().start()
        self.tmp = tempfile.TemporaryDirectory()
        self.config = Path(self.tmp.name) / "config.json"
        self.env = mock.patch.dict(
            os.environ,
            {
                "NOTAMIFY_TOKEN": "test-api-key",
                "NOTAMIFY_API_BASE_URL": self.api.url,
                "NOTAMIFY_WATCHER_BASE_URL": self.api.url,
                "NOTAMIFY_CONFIG_FILE": str(self.config),
                "NOTAMIFY_OUTPUT": "",
            },
        )
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.api.stop()
        self.tmp.cleanup()

    def run_cli(self, *argv: str, stdin: str = ""):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), mock.patch("sys.stdin", io.StringIO(stdin)):
            code = main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def run_json(self, *argv: str, stdin: str = ""):
        code, out, err = self.run_cli(*argv, stdin=stdin)
        return code, (json.loads(out) if out.strip() else None), (json.loads(err) if err.strip() else None)


class NotamCommandTests(CLITestCase):
    def test_piped_output_is_json_with_completeness(self):
        self.api.route("GET", "/notams", paged_notams(45))
        code, data, err = self.run_json("notams", "active", "epwa")
        self.assertEqual(code, 0, err)
        self.assertEqual(len(data["notams"]), 30)
        self.assertFalse(data["complete"])
        self.assertEqual(data["next_page"], 2)
        self.assertEqual(data["credits_estimate"], 1)
        self.assertEqual(self.api.last("GET", "/notams").query["location"], ["EPWA"])
        self.assertEqual(self.api.last("GET", "/notams").headers["User-Agent"].split("/")[0], "notamify-cli")

    def test_all_requires_confirmation_then_fetches_every_page(self):
        self.api.route("GET", "/notams", paged_notams(45))
        code, _, err = self.run_json("notams", "active", "EPWA", "--all")
        self.assertEqual(code, 4)
        self.assertEqual(err["error"]["code"], "confirmation_required")
        self.assertEqual(err["error"]["confirm_command"], "notamify notams active EPWA --all --yes")
        self.assertEqual(self.api.requests, [])

        code, data, _ = self.run_json("notams", "active", "EPWA", "--all", "--yes")
        self.assertEqual(code, 0)
        self.assertEqual(len(data["notams"]), 45)
        self.assertTrue(data["complete"])
        self.assertIsNone(data["next_page"])
        self.assertEqual(data["pages_fetched"], 2)

    def test_fields_keep_dotted_paths(self):
        self.api.route("GET", "/notams", paged_notams(1))
        _, data, _ = self.run_json("notams", "active", "EPWA", "--fields", "notam_number,interpretation.excerpt")
        self.assertEqual(data["notams"][0], {"notam_number": "A0000/26", "interpretation": {"excerpt": "RWY 11/29 closed"}})

    def test_filters_and_window_reach_the_api(self):
        self.api.route("GET", "/notams", paged_notams(0))
        self.run_json(
            "notams", "active", "EPWA", "--start", "2026-11-09T10:00:00Z", "--hours", "6",
            "--affected", "closed:runway", "--affected", ":taxiway", "--category", "aerodrome", "--no-est",
        )
        query = self.api.last("GET", "/notams").query
        self.assertEqual(query["starts_at"], ["2026-11-09T10:00:00Z"])
        self.assertEqual(query["ends_at"], ["2026-11-09T16:00:00Z"])
        self.assertEqual(query["affected_element"], ["effect:CLOSED,type:RUNWAY", "type:TAXIWAY"])
        self.assertEqual(query["category"], ["AERODROME"])
        self.assertEqual(query["always_include_est"], ["false"])

    def test_dry_run_sends_nothing(self):
        code, data, _ = self.run_json("notams", "nearby", "--lat", "52.1", "--lon", "20.9", "--radius", "5", "--dry-run")
        self.assertEqual(code, 0)
        self.assertTrue(data["dry_run"])
        self.assertIn("/notams/nearby?lat=52.1&lon=20.9&radius_nm=5.0", data["request"]["url"])
        self.assertEqual(self.api.requests, [])

    def test_text_output(self):
        self.api.route("GET", "/notams", paged_notams(1))
        code, out, _ = self.run_cli("notams", "active", "EPWA", "--output", "text")
        self.assertEqual(code, 0)
        self.assertIn("A0000/26  EPWA  2026-11-08 10:00Z → 2026-11-09 10:00Z  [AERODROME/RUNWAY]", out)
        self.assertIn("1 of 1 NOTAMs shown", out)

    def test_expired_archive_and_get(self):
        self.api.route("GET", "/notams/recently-expired", (200, {"notams": [sample_notam(expired_at="2026-11-08T08:00:00Z")], "total_count": 1, "page": 1, "per_page": 30}))
        code, data, _ = self.run_json("notams", "expired", "EPWA", "--after", "2026-11-05", "--before", "2026-11-08T12:00Z")
        self.assertEqual(code, 0)
        self.assertEqual(data["notams"][0]["expired_at"], "2026-11-08T08:00:00Z")
        self.assertEqual(self.api.last("GET", "/notams/recently-expired").query["expired_before"], ["2026-11-08T12:00:00Z"])

        self.api.route("GET", "/notams/archive", paged_notams(2))
        code, data, _ = self.run_json("notams", "archive", "EDDF", "--date", "2025-09-20")
        self.assertEqual(code, 0)
        self.assertEqual(self.api.last("GET", "/notams/archive").query["valid_at"], ["2025-09-20"])

        notam_id = "11111111-1111-1111-1111-111111111111"
        self.api.route("GET", f"/notams/{notam_id}", (200, sample_notam(notam_id)))
        code, data, _ = self.run_json("notams", "get", notam_id)
        self.assertEqual(data["notam"]["id"], notam_id)
        self.assertIn("disclaimer", data)

    def test_affected_flattens_elements(self):
        self.api.route("GET", "/notams", paged_notams(2))
        code, data, _ = self.run_json("affected", "EPWA")
        self.assertEqual(code, 0)
        self.assertEqual(len(data["elements"]), 2)
        element = data["elements"][0]
        self.assertEqual((element["type"], element["identifier"], element["effect"]), ("RUNWAY", "11/29", "CLOSED"))
        self.assertEqual(element["notam_number"], "A0000/26")
        self.assertTrue(data["complete"])
        _, data, _ = self.run_json("affected", "EPWA", "--fields", "identifier,effect")
        self.assertEqual(data["elements"][0], {"identifier": "11/29", "effect": "CLOSED"})


class ReviewRegressionTests(CLITestCase):
    def test_filtered_short_page_is_not_complete(self):
        # The API filters by category and affected element after paging; total_count is before filters.
        def page(req):
            n = int(req.query["page"][0])
            items = [sample_notam(f"00000000-0000-0000-0000-{n:012d}")] if n == 1 else []
            return 200, {"notams": items, "total_count": 90, "page": n, "per_page": 30}

        self.api.route("GET", "/notams", page)
        code, data, _ = self.run_json("notams", "active", "EGLL", "--affected", "CLOSED:RUNWAY")
        self.assertEqual(code, 0)
        self.assertFalse(data["complete"])
        self.assertEqual(data["next_page"], 2)
        self.assertTrue(data["filtered_after_paging"])
        code, data, _ = self.run_json("notams", "active", "EGLL", "--category", "aerodrome", "--all", "--yes")
        self.assertEqual((data["pages_fetched"], data["complete"]), (3, True))

    def test_wait_failure_keeps_the_job_uuid(self):
        self.api.route("POST", "/notams/briefing", (201, {"uuid": JOB, "status": "pending", "status_url": "/x"}))
        self.api.route("GET", f"/notams/briefing/{JOB}", (503, {"error": "unavailable"}))
        code, _, err = self.run_cli(
            "briefing", "flight", "create", "--origin", "EPWA@2026-11-09T08:00Z/2026-11-09T10:00Z", "--yes", "--wait"
        )
        self.assertEqual(code, 7)
        lines = [json.loads(line) for line in err.splitlines()]
        self.assertEqual(lines[0], {"event": "briefing_accepted", "uuid": JOB})
        self.assertEqual(lines[-1]["error"]["uuid"], JOB)
        self.assertIn("do not create it again", lines[-1]["error"]["remediation"])

    def test_negative_relative_times_and_now(self):
        self.api.route("GET", "/notams/recently-expired", (200, {"notams": [], "total_count": 0, "page": 1, "per_page": 30}))
        code, _, err = self.run_json("notams", "expired", "EPWA", "--after", "-3d")
        self.assertEqual(code, 0, err)
        self.api.route("GET", "/notams", paged_notams(0))
        code, _, err = self.run_json("notams", "active", "EPWA", "--start", "-2h", "--end", "now")
        self.assertEqual(code, 0, err)

    def test_location_end_counts_from_its_start(self):
        code, data, err = self.run_json(
            "briefing", "flight", "create", "--origin", "EPWA@2026-12-20T08:00Z/+2h", "--dry-run"
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(data["request"]["body"]["locations"][0]["ends_at"], "2026-12-20T10:00:00Z")

    def test_unexpected_response_is_not_a_usage_error(self):
        self.api.route("GET", "/listeners", (200, {"listeners": [{"id": "l1", "mode": "shadow"}]}))
        code, _, err = self.run_json("watcher", "listeners", "list")
        self.assertEqual((code, err["error"]["code"]), (1, "invalid_response"))
        self.api.route("GET", "/notams", (200, "<html>proxy</html>"))
        code, _, err = self.run_json("notams", "active", "EPWA")
        self.assertEqual((code, err["error"]["code"]), (7, "invalid_response"))

    def test_raw_has_no_interpretation_filters(self):
        code, _, err = self.run_json("notams", "raw", "EPWA", "--category", "AERODROME")
        self.assertEqual(code, 2)


class ErrorTests(CLITestCase):
    def test_http_errors_map_to_exit_codes(self):
        cases = [
            (401, {"error": "Invalid API key"}, 3, "auth_invalid"),
            (403, "<p>API call quota exceeded. To continue using the API, please purchase additional credits</p>", 5, "insufficient_credits"),
            (403, {"error": "Access to this endpoint is restricted."}, 5, "forbidden"),
            (404, {"error": "No NOTAMs"}, 6, "not_found"),
            (400, {"error": "bad location"}, 2, "invalid_request"),
            (503, {"error": "unavailable"}, 7, "server_error"),
        ]
        for status, body, exit_code, error_code in cases:
            with self.subTest(status=status, code=error_code):
                self.api.route("GET", "/notams", (status, body))
                code, _, err = self.run_json("notams", "active", "EPWA")
                self.assertEqual(code, exit_code)
                self.assertEqual(err["error"]["code"], error_code)
        self.assertIn("quota exceeded", err_message(self, 403))

    def test_missing_token(self):
        with mock.patch.dict(os.environ, {"NOTAMIFY_TOKEN": ""}):
            code, _, err = self.run_json("notams", "active", "EPWA")
        self.assertEqual(code, 3)
        self.assertEqual(err["error"]["code"], "auth_missing")

    def test_usage_errors_are_json_and_exit_2(self):
        for argv in (["notams", "activ", "EPWA"], ["notams", "active"], ["notams", "active", "EPWA", "--start", "tomorrow"], ["notams", "act", "EPWA"]):
            with self.subTest(argv=argv):
                code, _, err = self.run_json(*argv)
                self.assertEqual(code, 2)
                self.assertIn("error", err)

    def test_abbreviated_options_are_rejected(self):
        code, _, err = self.run_json("notams", "active", "EPWA", "--per", "5")
        self.assertEqual(code, 2)


def err_message(case: CLITestCase, status: int) -> str:
    case.api.route("GET", "/notams", (status, "<p>API call quota exceeded.</p>"))
    _, _, err = case.run_json("notams", "active", "EPWA")
    return err["error"]["message"]


class BriefingTests(CLITestCase):
    def job(self, status: str) -> dict:
        body = {"uuid": JOB, "status": status, "created_at": "2026-11-08T10:00:00Z", "updated_at": "2026-11-08T10:01:00Z"}
        if status == "completed":
            body["response"] = {
                "locations": [],
                "briefing": {"text": "All clear except RWY 11/29.", "critical_operational_restrictions": []},
            }
        return body

    def test_flight_create_needs_yes_then_waits(self):
        argv = ["briefing", "flight", "create", "--origin", "EPWA@2026-11-09T08:00Z/2026-11-09T10:00Z",
                "--destination", "EGLL@2026-11-09T10:30Z/2026-11-09T13:00Z", "--aircraft", "b738"]
        code, _, err = self.run_json(*argv)
        self.assertEqual(code, 4)
        self.assertEqual(err["error"]["credits_estimate"], "5 credits")

        self.api.route("POST", "/notams/briefing", (201, {"uuid": JOB, "status": "pending", "status_url": "/x"}))
        polls = iter(["processing", "completed"])
        self.api.route("GET", f"/notams/briefing/{JOB}", lambda req: (200, self.job(next(polls))))
        code, data, _ = self.run_json(*argv, "--yes", "--wait", "--interval", "0.5")
        self.assertEqual(code, 0)
        self.assertEqual(data["job"]["status"], "completed")
        body = self.api.last("POST", "/notams/briefing").body
        self.assertEqual(body["aircraft_type"], "B738")
        self.assertEqual(body["locations"][0], {"location": "EPWA", "type": "origin", "starts_at": "2026-11-09T08:00:00Z", "ends_at": "2026-11-09T10:00:00Z", "always_include_est": True})

    def test_wait_reports_failed_job_as_terminal(self):
        self.api.route("GET", f"/notams/briefing/{JOB}", (500, self.job("failed")))
        code, _, err = self.run_json("briefing", "flight", "wait", JOB)
        self.assertEqual(code, 1)
        self.assertEqual(err["error"]["code"], "briefing_failed")

    def test_wait_times_out_with_exit_7(self):
        self.api.route("GET", f"/notams/briefing/{JOB}", (202, self.job("processing")))
        code, _, err = self.run_json("briefing", "flight", "wait", JOB, "--wait-timeout", "0", "--interval", "0.5")
        self.assertEqual(code, 7)
        self.assertEqual(err["error"]["status"], "processing")

    def test_airport_briefing(self):
        self.api.route("GET", "/notams/briefing/simple", (200, {"location": "EPWA", "summary": "RWY closed", "notam_ids": ["A1/26"]}))
        code, _, err = self.run_json("briefing", "airport", "EPWA")
        self.assertEqual(code, 4)
        code, data, _ = self.run_json("briefing", "airport", "epwa", "--detailed", "--yes")
        self.assertEqual(code, 0)
        self.assertEqual(data["briefing"]["notam_ids"], ["A1/26"])
        self.assertEqual(self.api.last("GET", "/notams/briefing/simple").query["type"], ["detailed"])

    def test_prioritize_body_from_stdin(self):
        self.api.route("POST", "/notams/prioritisation", (200, {"notams": [], "total_count": 0, "page": 1, "per_page": 10}))
        body = {"locations": [{"location": "EPWA", "type": "origin", "starts_at": "2026-11-09T08:00:00Z", "ends_at": "2026-11-09T10:00:00Z"}]}
        code, _, err = self.run_json("prioritize", "N1", "--body", "-", "--yes", stdin=json.dumps(body))
        self.assertEqual(code, 0, err)
        self.assertEqual(self.api.last("POST", "/notams/prioritisation").body["notam_id"], "N1")


class WatcherTests(CLITestCase):
    def test_update_changes_only_given_fields(self):
        self.api.route("GET", "/listeners", (200, {"listeners": [LISTENER]}))
        self.api.route("PUT", "/listeners/l1", lambda req: (200, {**LISTENER, **req.body}))
        code, _, err = self.run_json("watcher", "listeners", "update", "l1", "--icao", "epkk")
        self.assertEqual(code, 4)
        code, data, err = self.run_json("watcher", "listeners", "update", "l1", "--icao", "epkk", "--yes")
        self.assertEqual(code, 0, err)
        body = self.api.last("PUT", "/listeners/l1").body
        self.assertEqual(body["name"], "ops")
        self.assertEqual(body["webhook_url"], "https://example.com/hook")
        self.assertEqual(body["filters"], {"notam_icao": ["EPKK"], "category": ["AERODROME"]})

    def test_create_and_delete(self):
        self.api.route("POST", "/listeners", lambda req: (201, {**req.body, "id": "l2", "webhook_secret": "nmf_wh_secret"}))
        code, data, err = self.run_json(
            "watcher", "listeners", "create", "--name", "dev", "--mode", "sandbox",
            "--webhook-url", "https://example.com/dev", "--affected", "CLOSED:RUNWAY", "--yes",
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(data["listener"]["webhook_secret"], "nmf_wh_secret")
        self.assertEqual(self.api.last("POST", "/listeners").body["filters"], {"affected_element": [{"effect": "CLOSED", "type": "RUNWAY"}]})

        self.api.route("DELETE", "/listeners/l2", (204, None))
        code, _, err = self.run_json("watcher", "listeners", "delete", "l2")
        self.assertEqual(code, 4)
        code, data, _ = self.run_json("watcher", "listeners", "delete", "l2", "--yes")
        self.assertEqual(data, {"deleted": True, "listener_id": "l2"})

    def test_logs_and_secret(self):
        self.api.route("GET", "/webhook-logs", (200, {"success": [{"listener_id": "l1", "status": "200"}], "errors": []}))
        _, data, _ = self.run_json("watcher", "logs", "--errors-only")
        self.assertEqual(data, {"errors": []})
        self.api.route("POST", "/webhook-secret:rotate", (200, {"webhook_secret": "nmf_wh_new"}))
        code, _, _ = self.run_json("watcher", "secret", "rotate")
        self.assertEqual(code, 4)
        _, data, _ = self.run_json("watcher", "secret", "rotate", "--yes")
        self.assertEqual(data["webhook_secret"], "nmf_wh_new")


class LocalCommandTests(CLITestCase):
    def test_auth_login_status_logout(self):
        with mock.patch.dict(os.environ, {"NOTAMIFY_TOKEN": ""}):
            code, data, _ = self.run_json("auth", "status")
            self.assertEqual(code, 3)
            self.assertFalse(data["authenticated"])

            code, data, _ = self.run_json("auth", "login", "--token-stdin", stdin="test-api-key-abcdefghijklmnop\n")
            self.assertEqual(code, 0)
            self.assertEqual(stat.S_IMODE(self.config.stat().st_mode), 0o600)
            self.assertNotIn("abcdefghijklmnop", json.dumps(data))

            code, data, _ = self.run_json("auth", "status")
            self.assertEqual((code, data["source"]), (0, "config"))

            code, data, _ = self.run_json("auth", "logout")
            self.assertTrue(data["removed"])
            code, _, _ = self.run_json("auth", "status")
            self.assertEqual(code, 3)

    def test_login_without_terminal_or_stdin_flag(self):
        code, _, err = self.run_json("auth", "login")
        self.assertEqual(code, 2)
        self.assertIn("--token-stdin", err["error"]["remediation"])

    def test_commands_manifest(self):
        code, data, _ = self.run_json("commands")
        self.assertEqual(code, 0)
        names = {c["command"] for c in data["commands"]}
        self.assertTrue({"notams active", "briefing flight create", "watcher listeners update", "webhook listen"} <= names)
        self.assertEqual(set(data["exit_codes"]), {"0", "1", "2", "3", "4", "5", "6", "7", "130"})
        for command in data["commands"]:
            self.assertTrue(command["summary"], command["command"])

    def test_every_command_has_help(self):
        manifest = describe_commands(build_parser())
        for command in manifest["commands"]:
            with self.subTest(command=command["command"]):
                out = io.StringIO()
                with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as ctx:
                    build_parser().parse_args([*command["command"].split(), "--help"])
                self.assertEqual(ctx.exception.code, 0)
                self.assertIn("usage: notamify", out.getvalue())

    def test_skill_install(self):
        target_dir = Path(self.tmp.name) / "skills"
        code, data, _ = self.run_json("skill", "install", "--dir", str(target_dir))
        self.assertEqual(code, 0)
        skill = (target_dir / "notamify" / "SKILL.md").read_text()
        self.assertTrue(skill.startswith("---\nname: notamify"))
        code, data, _ = self.run_json("skill", "install", "--dir", str(target_dir))
        self.assertFalse(data["changed"])


class WebhookListenTests(CLITestCase):
    def test_receives_one_signed_delivery(self):
        secret = "whsec_test"
        port = _free_port()
        out = io.StringIO()
        result = {}

        def run():
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                result["code"] = main(["webhook", "listen", "--port", str(port), "--count", "1", "--listen-timeout", "10", "--json"])

        with mock.patch.dict(os.environ, {"NOTAMIFY_WEBHOOK_SECRET": secret}):
            thread = threading.Thread(target=run)
            thread.start()
            _wait_for(lambda: '"ready"' in out.getvalue())
            payload = {
                "listener_id": "l1",
                "kind": "interpretation",
                "event_id": "e1",
                "notam": sample_notam(),
                "sent_at": "2026-11-08T10:00:00Z",
            }
            raw = json.dumps(payload).encode()
            ts = int(time.time())
            req = request.Request(
                f"http://127.0.0.1:{port}/webhooks/notamify",
                data=raw,
                method="POST",
                headers={"Content-Type": "application/json", "X-Notamify-Signature": f"t={ts},v1={compute_signature(secret, ts, raw)}"},
            )
            with request.urlopen(req, timeout=5) as resp:
                self.assertEqual(resp.status, 202)
            thread.join(timeout=10)

        self.assertEqual(result.get("code"), 0)
        lines = [json.loads(line) for line in out.getvalue().splitlines()]
        self.assertEqual(lines[0]["type"], "ready")
        self.assertEqual(lines[1]["type"], "delivery")
        self.assertTrue(lines[1]["signature_verified"])
        self.assertEqual(lines[1]["event_id"], "e1")

    def test_requires_secret(self):
        with mock.patch.dict(os.environ, {"NOTAMIFY_WEBHOOK_SECRET": ""}):
            code, _, err = self.run_json("webhook", "listen")
        self.assertEqual(code, 2)
        self.assertEqual(err["error"]["code"], "webhook_secret_missing")


class ParseTimeTests(unittest.TestCase):
    base = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)

    def test_formats(self):
        self.assertEqual(parse_time("2026-11-09T10:00:00Z"), datetime(2026, 11, 9, 10, tzinfo=timezone.utc))
        self.assertEqual(parse_time("2026-11-09T12:00:00+02:00"), datetime(2026, 11, 9, 10, tzinfo=timezone.utc))
        self.assertEqual(parse_time("2026-11-09T10:00"), datetime(2026, 11, 9, 10, tzinfo=timezone.utc))
        self.assertEqual(parse_time("2026-11-09"), datetime(2026, 11, 9, tzinfo=timezone.utc))
        self.assertEqual(parse_time("2026-11-09", end_of_day=True), datetime(2026, 11, 9, 23, 59, 59, tzinfo=timezone.utc))
        # "now" is always the current time; only offsets count from base.
        self.assertLess(abs((parse_time("now", base=self.base) - datetime.now(timezone.utc)).total_seconds()), 5)
        self.assertEqual(parse_time("+6h", base=self.base), datetime(2026, 11, 8, 18, tzinfo=timezone.utc))
        self.assertEqual(parse_time("-30m", base=self.base), datetime(2026, 11, 8, 11, 30, tzinfo=timezone.utc))
        self.assertEqual(parse_time("+2d", base=self.base), datetime(2026, 11, 10, 12, tzinfo=timezone.utc))
        with self.assertRaises(ValueError):
            parse_time("tomorrow")


def _free_port() -> int:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_for(condition, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.05)
    raise AssertionError("timed out")


if __name__ == "__main__":
    unittest.main()
