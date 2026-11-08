import os
import unittest
from unittest import mock

from tests._fake_account import FakeAccountServer, ToolFailure, approve_in_background
from tests._fake_api import paged_notams, sample_notam
from tests.test_cli import CLITestCase

BILLING = {"credits_used": 1, "pricing_mode": "promotion"}
JOB = "22222222-2222-2222-2222-222222222222"


def summarized(notam: dict) -> dict:
    """search_* tools return summarised interpretations, like the hosted server."""
    interp = notam["interpretation"]
    return {**notam, "interpretation": {k: interp.get(k) for k in ("category", "excerpt", "affected_elements", "schedule_description", "schedules")}}


class AccountCLITestCase(CLITestCase):
    def setUp(self):
        super().setUp()
        self.account = FakeAccountServer().start()
        self.account_env = mock.patch.dict(os.environ, {"NOTAMIFY_MCP_URL": self.account.mcp_url, "NOTAMIFY_TOKEN": ""})
        self.account_env.start()
        self.browser.stop()
        self.browser = mock.patch("webbrowser.open", side_effect=lambda url: approve_in_background(url) or True)
        self.browser.start()

    def tearDown(self):
        self.account_env.stop()
        self.account.stop()
        super().tearDown()

    def sign_in(self):
        code, data, err = self.run_json("auth", "login", "--login-timeout", "10")
        self.assertEqual(code, 0, err)
        return data


class AccountLoginTests(AccountCLITestCase):
    def test_login_verifies_with_a_free_call(self):
        self.account.tool("search_active_notams", {})
        data = self.sign_in()
        self.assertTrue(data["verified"])
        self.assertEqual(data["tools_available"], 1)
        self.assertEqual(data["scopes"], ["notams:read", "briefings:write"])
        self.assertEqual(self.account.tool_calls, [])  # tools/list only, nothing billed

    def test_status_and_logout(self):
        self.sign_in()
        code, data, _ = self.run_json("auth", "status")
        self.assertEqual(code, 0)
        self.assertEqual(data["notams_and_briefings"], "account")
        self.assertIsNone(data["watcher"])
        code, data, _ = self.run_json("auth", "logout", "--account")
        self.assertTrue(data["account_signed_out"])
        self.assertTrue(self.account.revoked_grants)
        code, _, _ = self.run_json("auth", "status")
        self.assertEqual(code, 3)

    def test_denied(self):
        self.account.deny = True
        code, _, err = self.run_json("auth", "login", "--login-timeout", "10")
        self.assertEqual(code, 1)
        self.assertEqual(err["error"]["code"], "login_denied")


class AccountCommandTests(AccountCLITestCase):
    def setUp(self):
        super().setUp()
        self.sign_in()

    def test_notams_active_uses_the_search_tool(self):
        page = paged_notams(45)

        def search(args):
            status, body = page(_FakeQuery(args))
            return {**body, "notams": [summarized(n) for n in body["notams"]], "mcp_billing": BILLING, "source": "Notamify"}

        self.account.tool("search_active_notams", search)
        code, data, err = self.run_json("notams", "active", "epwa", "--start", "2026-11-09T10:00:00Z", "--hours", "6", "--affected", "CLOSED:RUNWAY")
        self.assertEqual(code, 0, err)
        name, args = self.account.tool_calls[-1]
        self.assertEqual(name, "search_active_notams")
        self.assertEqual(args["locations"], ["EPWA"])
        self.assertEqual(args["starts_at"], "2026-11-09T10:00:00Z")
        self.assertEqual(args["affected_element"], [{"effect": "CLOSED", "type": "RUNWAY"}])
        self.assertEqual((args["page"], args["per_page"]), (1, 30))
        self.assertEqual(data["auth"], "account")
        self.assertEqual(data["credits_charged"], 1)
        self.assertEqual(data["pricing_mode"], "promotion")
        self.assertFalse(data["complete"])
        self.assertEqual(self.api.requests, [])  # the REST API was not used

    def test_all_pages_quote_the_account_rate(self):
        self.account.tool("search_active_notams", {"notams": [], "total_count": 0, "page": 1, "per_page": 30})
        code, _, err = self.run_json("notams", "active", "EPWA", "--all", "--max-pages", "3")
        self.assertEqual(code, 4)
        self.assertEqual(err["error"]["credits_estimate"], "up to 3 credits (1 per page), account rate")

    def test_flight_briefing_retry_reuses_the_idempotency_key(self):
        self.account.tool("create_flight_briefing", {"uuid": JOB, "status_url": "/x", "mcp_billing": BILLING})
        argv = ["briefing", "flight", "create", "--origin", "EPWA@2026-11-09T08:00Z/2026-11-09T10:00Z",
                "--destination", "EGLL@2026-11-09T10:30Z/2026-11-09T13:00Z", "--aircraft", "B738"]
        code, _, err = self.run_json(*argv)
        self.assertEqual(code, 4)
        self.assertEqual(err["error"]["credits_estimate"], "1 credit, account rate")
        self.run_json(*argv, "--yes")
        self.run_json(*argv, "--yes")
        keys = [args["idempotency_key"] for name, args in self.account.tool_calls if name == "create_flight_briefing"]
        self.assertEqual(len(keys), 2)
        self.assertEqual(keys[0], keys[1])
        self.assertTrue(keys[0].startswith("cli-"))

    def test_wait_polls_get_flight_briefing(self):
        states = iter(["processing", "failed"])
        self.account.tool(
            "get_flight_briefing",
            lambda args: {"uuid": args["briefing_id"], "status": next(states), "created_at": "2026-11-08T10:00:00Z", "updated_at": "2026-11-08T10:01:00Z"},
        )
        code, _, err = self.run_json("briefing", "flight", "wait", JOB, "--interval", "0.5")
        self.assertEqual(code, 1)
        self.assertEqual(err["error"]["code"], "briefing_failed")

    def test_job_from_the_other_sign_in_method(self):
        def missing(args):
            raise ToolFailure("Briefing not found for your connected app.")

        self.account.tool("get_flight_briefing", missing)
        code, _, err = self.run_json("briefing", "flight", "status", JOB)
        self.assertEqual(code, 6)
        self.assertIn("sign-in method", err["error"]["remediation"])

    def test_tool_errors_map_to_exit_codes(self):
        cases = [
            ("Insufficient API credits. Review your account at https://notamify.com/account#connections", 5, "insufficient_credits"),
            ("Notamify usage limit reached. Retry in 30 seconds.", 7, "rate_limited"),
            ("Notamify rejected this query. Check its parameters before trying again.", 2, "invalid_request"),
        ]
        for text, exit_code, code_name in cases:
            with self.subTest(code=code_name):
                def fail(args, text=text):
                    raise ToolFailure(text)

                self.account.tool("get_notam", fail)
                code, _, err = self.run_json("notams", "get", "11111111-1111-1111-1111-111111111111")
                self.assertEqual(code, exit_code)
                self.assertEqual(err["error"]["code"], code_name)

    def test_get_notam_strips_server_extras(self):
        notam_id = "11111111-1111-1111-1111-111111111111"
        self.account.tool("get_notam", {**sample_notam(notam_id), "retrieved_at": "x", "source": "Notamify", "mcp_billing": BILLING})
        code, data, _ = self.run_json("notams", "get", notam_id)
        self.assertEqual(code, 0)
        self.assertNotIn("mcp_billing", data["notam"])
        self.assertEqual(data["credits_charged"], 1)

    def test_raw_and_watcher_need_an_api_key(self):
        for argv in (["notams", "raw", "EPWA"], ["watcher", "listeners", "list"]):
            with self.subTest(argv=argv):
                code, _, err = self.run_json(*argv)
                self.assertEqual(code, 3)
                self.assertEqual(err["error"]["code"], "api_key_required")

    def test_auth_flag_and_env_token_choose_the_api_key(self):
        self.api.route("GET", "/notams", paged_notams(1))
        with mock.patch.dict(os.environ, {"NOTAMIFY_TOKEN": "nmf_sk_test"}):
            code, data, _ = self.run_json("notams", "active", "EPWA")
            self.assertEqual((code, data["auth"]), (0, "api_key"))
            self.account.tool("search_active_notams", {"notams": [], "total_count": 0, "page": 1, "per_page": 30, "mcp_billing": BILLING})
            code, data, _ = self.run_json("notams", "active", "EPWA", "--auth", "account")
            self.assertEqual((code, data["auth"]), (0, "account"))

    def test_dry_run_shows_the_tool_call(self):
        code, data, _ = self.run_json("briefing", "airport", "EPWA", "--dry-run")
        self.assertEqual(code, 0)
        self.assertEqual(data["request"]["tool"], "generate_airport_briefing")
        self.assertEqual(data["request"]["arguments"], {"location": "EPWA"})
        self.assertEqual(self.account.tool_calls, [])


class _FakeQuery:
    def __init__(self, args: dict) -> None:
        self.query = {k: [str(v)] for k, v in args.items() if k in ("page", "per_page")}


if __name__ == "__main__":
    unittest.main()
