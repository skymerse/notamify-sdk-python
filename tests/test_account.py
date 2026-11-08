import json
import stat
import tempfile
import threading
import time
import unittest
from pathlib import Path

from notamify_sdk.account import AccountAuth, AccountError, AccountStore, McpClient, tool_error

from tests._fake_account import FakeAccountServer, ToolFailure, approve_in_background


class AccountTestCase(unittest.TestCase):
    def setUp(self):
        self.server = FakeAccountServer().start()
        self.tmp = tempfile.TemporaryDirectory()
        self.store_path = Path(self.tmp.name) / "account.json"

    def tearDown(self):
        self.server.stop()
        self.tmp.cleanup()

    def auth(self) -> AccountAuth:
        return AccountAuth(self.server.mcp_url, AccountStore(self.store_path))

    def login(self, auth: AccountAuth | None = None):
        auth = auth or self.auth()
        return auth.login(open_browser=False, timeout=10, on_url=approve_in_background)


class LoginTests(AccountTestCase):
    def test_login_stores_a_private_session_and_reuses_the_registration(self):
        session = self.login()
        self.assertEqual(session.scopes, ["notams:read", "briefings:write"])
        self.assertEqual(stat.S_IMODE(self.store_path.stat().st_mode), 0o600)
        self.assertEqual(AccountStore(self.store_path).load().access_token, session.access_token)
        self.login()
        self.assertEqual(self.server.registrations, 1)
        registration = next(iter(self.server.clients.values()))
        self.assertEqual(registration["redirect_uris"], ["http://127.0.0.1/callback"])
        self.assertEqual(registration["token_endpoint_auth_method"], "none")
        self.assertEqual(registration["client_name"], "Notamify CLI")

    def test_login_sends_resource_and_both_scopes(self):
        seen = {}

        def capture(url):
            seen["url"] = url
            approve_in_background(url)

        self.auth().login(open_browser=False, timeout=10, on_url=capture)
        self.assertIn("scope=notams%3Aread+briefings%3Awrite", seen["url"])
        self.assertIn("resource=" + self.server.mcp_url.replace(":", "%3A").replace("/", "%2F"), seen["url"])
        self.assertIn("code_challenge_method=S256", seen["url"])

    def test_denied_login(self):
        self.server.deny = True
        with self.assertRaises(AccountError) as ctx:
            self.login()
        self.assertEqual(ctx.exception.code, "login_denied")
        self.assertIsNone(AccountStore(self.store_path).load())

    def test_login_times_out_without_a_browser(self):
        with self.assertRaises(AccountError) as ctx:
            self.auth().login(open_browser=False, timeout=0.3, on_url=lambda url: None)
        self.assertEqual(ctx.exception.code, "login_timeout")

    def test_stale_registration_is_replaced(self):
        self.login()
        data = json.loads(self.store_path.read_text())
        data["client"]["registered_at"] = time.time() - 85 * 86400
        self.store_path.write_text(json.dumps(data))
        self.login()
        self.assertEqual(self.server.registrations, 2)


class TokenTests(AccountTestCase):
    def test_expired_access_token_is_refreshed_and_rotated(self):
        self.server.expires_in = 0
        first = self.login()
        self.server.expires_in = 900
        token = self.auth().access_token()
        self.assertNotEqual(token, first.access_token)
        stored = AccountStore(self.store_path).load()
        self.assertNotEqual(stored.refresh_token, first.refresh_token)
        self.assertEqual(self.server.refresh_count, 1)

    def test_parallel_refreshes_redeem_the_refresh_token_once(self):
        self.server.expires_in = 0
        self.login()
        self.server.expires_in = 900
        tokens, errors = [], []

        def worker():
            try:
                tokens.append(self.auth().access_token())  # a separate instance, like a separate process
            except AccountError as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(set(tokens)), 1)
        self.assertEqual(self.server.refresh_count, 1)
        self.assertEqual(self.server.revoked_grants, set())

    def test_rejected_access_token_is_refreshed_and_the_call_retried(self):
        self.login()
        self.server.tool("search_active_notams", {"notams": [], "total_count": 0, "page": 1, "per_page": 30})
        self.server.expire_access_tokens()
        client = McpClient(self.auth())
        self.assertEqual(client.call_tool("search_active_notams", {"locations": ["EPWA"]})["total_count"], 0)
        self.assertEqual(self.server.refresh_count, 1)

    def test_replayed_refresh_token_ends_the_session(self):
        self.server.expires_in = 0
        self.login()
        stale = self.store_path.read_text()
        self.auth().access_token()  # rotates
        self.store_path.write_text(stale)  # a process that kept the old refresh token
        with self.assertRaises(AccountError) as ctx:
            self.auth().access_token()
        self.assertEqual(ctx.exception.code, "session_expired")
        self.assertIsNone(AccountStore(self.store_path).load())

    def test_not_signed_in(self):
        with self.assertRaises(AccountError) as ctx:
            self.auth().access_token()
        self.assertEqual(ctx.exception.code, "not_signed_in")

    def test_logout_revokes_and_keeps_the_registration(self):
        self.login()
        self.assertEqual(self.auth().logout(), {"signed_out": True, "revoked": True})
        self.assertTrue(self.server.revoked_grants)
        self.assertIsNone(AccountStore(self.store_path).load())
        self.assertIsNotNone(AccountStore(self.store_path).client(self.server.base))
        self.assertEqual(self.auth().logout(), {"signed_out": False, "revoked": False})


class ServerBindingTests(AccountTestCase):
    def test_tokens_are_never_sent_to_another_server(self):
        self.server.expires_in = 0  # force a refresh attempt too
        self.login()
        other = FakeAccountServer().start()
        try:
            other_auth = AccountAuth(other.mcp_url, AccountStore(self.store_path))
            with self.assertRaises(AccountError) as ctx:
                McpClient(other_auth).call_tool("get_notam", {"notam_id": "x"})
            self.assertEqual(ctx.exception.code, "not_signed_in")
            self.assertEqual([r for r in other.api.requests if r.path in ("/mcp", "/oauth/token")], [])
            self.assertIsNotNone(AccountStore(self.store_path).load())  # the real session is untouched
        finally:
            other.stop()

    def test_insecure_server_url_is_refused(self):
        with self.assertRaises(AccountError) as ctx:
            AccountAuth("http://example.com/mcp", AccountStore(self.store_path))
        self.assertEqual(ctx.exception.code, "insecure_server")

    def test_metadata_with_another_issuer_is_refused(self):
        self.server.metadata_issuer = "https://evil.example"
        with self.assertRaises(AccountError) as ctx:
            self.login()
        self.assertEqual(ctx.exception.code, "insecure_server")

    def test_missing_iss_is_refused_when_advertised(self):
        self.server.omit_iss = True
        with self.assertRaises(AccountError) as ctx:
            self.login()
        self.assertEqual(ctx.exception.code, "login_failed")

    def test_callback_with_another_state_does_not_end_the_login(self):
        from urllib import request as urlrequest

        def hijack_then_approve(url):
            port = parse_port(url)
            try:
                urlrequest.urlopen(f"http://127.0.0.1:{port}/callback?code=evil&state=wrong", timeout=5)
            except Exception:
                pass
            approve_in_background(url)

        session = self.auth().login(open_browser=False, timeout=10, on_url=hijack_then_approve)
        self.assertTrue(session.access_token.startswith("at-"))

    def test_temporary_refresh_failure_keeps_the_session(self):
        self.server.expires_in = 0
        self.login()
        self.server.token_status = (429, {"error": "rate_limited"})
        with self.assertRaises(AccountError) as ctx:
            self.auth().access_token()
        self.assertEqual(ctx.exception.code, "unavailable")
        self.assertIsNotNone(AccountStore(self.store_path).load())

    def test_redirects_are_not_followed(self):
        self.login()
        other = FakeAccountServer().start()
        try:
            self.server.api.route("POST", "/mcp", lambda req: (302, None, {"Location": f"{other.base}/mcp"}))
            with self.assertRaises(AccountError):
                McpClient(self.auth()).call_tool("get_notam", {"notam_id": "x"})
            self.assertEqual(other.api.requests, [])
        finally:
            other.stop()


def parse_port(url):
    from urllib.parse import parse_qs, urlsplit

    redirect = parse_qs(urlsplit(url).query)["redirect_uri"][0]
    return urlsplit(redirect).port


class McpTests(AccountTestCase):
    def test_structured_content_and_protocol_headers(self):
        self.login()
        self.server.tool("get_notam", lambda args: {"id": args["notam_id"], "source": "Notamify"})
        client = McpClient(self.auth())
        self.assertEqual(client.call_tool("get_notam", {"notam_id": "abc"})["id"], "abc")
        mcp_requests = [r for r in self.server.api.requests if r.path == "/mcp"]
        self.assertEqual(mcp_requests[0].body["method"], "initialize")
        headers = {k.lower(): v for k, v in mcp_requests[1].headers.items()}
        self.assertEqual(headers.get("mcp-protocol-version"), "2025-06-18")
        self.assertIn("text/event-stream", headers.get("accept", ""))
        self.assertEqual([t["name"] for t in client.list_tools()], ["get_notam"])

    def test_tool_failure_text_maps_to_codes(self):
        self.login()

        def fail(args):
            raise ToolFailure("Insufficient API credits. Review your account at https://notamify.com/account#connections")

        self.server.tool("search_active_notams", fail)
        with self.assertRaises(AccountError) as ctx:
            McpClient(self.auth()).call_tool("search_active_notams", {"locations": ["EPWA"]})
        self.assertEqual(ctx.exception.code, "insufficient_credits")

    def test_missing_briefing_permission(self):
        self.server.grant_scope = "notams:read"
        self.login()
        self.server.tool("create_flight_briefing", {"uuid": "x"})
        with self.assertRaises(AccountError) as ctx:
            McpClient(self.auth()).call_tool("create_flight_briefing", {"locations": []})
        self.assertEqual(ctx.exception.code, "insufficient_scope")

    def test_error_texts(self):
        cases = {
            "Notamify usage limit reached. Retry in 30 seconds.": ("rate_limited", 30),
            "An active Notamify Pro subscription or existing API agreement is required.": ("forbidden", None),
            "Record not found.": ("not_found", None),
            "Briefing not found for your connected app.": ("not_found", None),
            "Notamify rejected this query. Check its parameters before trying again.": ("invalid_request", None),
            "Notamify service is temporarily unavailable.": ("unavailable", None),
            "Reconnect this app and approve briefing generation permission.": ("insufficient_scope", None),
            "This request key was already used with different flight details.": ("idempotency_conflict", None),
            "This briefing request is in progress or its outcome is uncertain.": ("idempotency_conflict", None),
        }
        for text, (code, retry) in cases.items():
            with self.subTest(text=text):
                err = tool_error(text)
                self.assertEqual((err.code, err.retry_after), (code, retry))


if __name__ == "__main__":
    unittest.main()
