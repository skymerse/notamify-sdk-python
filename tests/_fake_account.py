"""A local fake of an OAuth 2.1 authorization server and MCP endpoint for tests.

It implements what a careful client must handle: PKCE S256, dynamic client
registration with loopback redirects on any port, iss on the redirect, single-use
refresh tokens with replay detection (RFC 9700), stateless JSON-RPC over POST /mcp,
structuredContent results and text-only tool errors.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import threading
from typing import Any, Callable
from urllib.parse import urlencode, urlsplit

from tests._fake_api import FakeAPI, Request

ToolHandler = Callable[[dict[str, Any]], dict[str, Any]]


class FakeAccountServer:
    def __init__(self) -> None:
        self.api = FakeAPI()
        self.lock = threading.Lock()
        self.clients: dict[str, dict[str, Any]] = {}
        self.codes: dict[str, dict[str, Any]] = {}
        self.access: dict[str, dict[str, Any]] = {}
        self.refresh: dict[str, dict[str, Any]] = {}
        self.redeemed: set[str] = set()
        self.revoked_grants: set[str] = set()
        self.refresh_count = 0
        self.registrations = 0
        self.tools: dict[str, ToolHandler] = {}
        self.tool_calls: list[tuple[str, dict[str, Any]]] = []
        self.deny = False
        self.omit_iss = False
        self.token_status: tuple[int, Any] | None = None
        self.metadata_issuer: str | None = None
        self.grant_scope: str | None = None
        self.expires_in = 900
        self.billing = {
            "promotion_active": True,
            "read_credits": 1,
            "read_unit": "operation",
            "generation_rates": {"airport": 1, "flight": 1, "prioritization": 1},
        }
        self._routes()

    @property
    def base(self) -> str:
        return self.api.url

    @property
    def mcp_url(self) -> str:
        return f"{self.base}/mcp"

    def start(self) -> "FakeAccountServer":
        self.api.start()
        return self

    def stop(self) -> None:
        self.api.stop()

    def tool(self, name: str, handler: ToolHandler | dict[str, Any]) -> None:
        self.tools[name] = handler if callable(handler) else (lambda _args, value=handler: value)

    def expire_access_tokens(self) -> None:
        with self.lock:
            self.access.clear()

    # OAuth -------------------------------------------------------------------

    def _routes(self) -> None:
        api = self.api
        api.route("GET", "/.well-known/oauth-protected-resource/mcp", lambda req: (200, {"resource": self.mcp_url, "authorization_servers": [self.base]}))
        api.route(
            "GET",
            "/.well-known/oauth-authorization-server",
            lambda req: (
                200,
                {
                    "issuer": self.metadata_issuer or self.base,
                    "authorization_endpoint": f"{self.base}/authorize",
                    "token_endpoint": f"{self.base}/oauth/token",
                    "registration_endpoint": f"{self.base}/oauth/register",
                    "revocation_endpoint": f"{self.base}/oauth/token",
                    "code_challenge_methods_supported": ["S256"],
                    "authorization_response_iss_parameter_supported": True,
                    "scopes_supported": ["notams:read", "briefings:write"],
                },
            ),
        )
        api.route("POST", "/oauth/register", self._register)
        api.route("GET", "/authorize", self._authorize)
        api.route("POST", "/oauth/token", self._token)
        api.route("POST", "/mcp", self._mcp)
        api.route("GET", "/billing", lambda req: (200, self.billing))

    def _register(self, req: Request) -> tuple[int, Any]:
        body = req.body or {}
        if not body.get("client_name") or not body.get("redirect_uris"):
            return 400, {"error": "invalid_client_metadata"}
        client_id = "client-" + secrets.token_hex(4)
        with self.lock:
            self.clients[client_id] = body
            self.registrations += 1
        return 201, {"client_id": client_id, **body}

    def _authorize(self, req: Request) -> tuple[int, Any, dict[str, str]]:
        q = {k: v[0] for k, v in req.query.items()}
        client = self.clients.get(q.get("client_id", ""))
        if client is None:
            return 400, {"error": "invalid_client"}, {}
        registered = urlsplit(client["redirect_uris"][0])
        redirect = urlsplit(q.get("redirect_uri", ""))
        # Loopback: any port matches (RFC 8252).
        if (redirect.scheme, redirect.hostname, redirect.path) != (registered.scheme, registered.hostname, registered.path):
            return 400, {"error": "invalid redirect"}, {}
        if q.get("code_challenge_method") != "S256" or not q.get("code_challenge"):
            return 400, {"error": "PKCE S256 required"}, {}
        if self.deny:
            params = {"error": "access_denied", "state": q.get("state", ""), "iss": self.base}
        else:
            code = secrets.token_urlsafe(16)
            with self.lock:
                self.codes[code] = {
                    "client_id": q["client_id"],
                    "challenge": q["code_challenge"],
                    "redirect_uri": q["redirect_uri"],
                    "scope": self.grant_scope or q.get("scope", "notams:read"),
                    "resource": q.get("resource"),
                }
            params = {"code": code, "state": q.get("state", ""), "iss": self.base}
        if self.omit_iss:
            params.pop("iss", None)
        return 302, None, {"Location": f"{q['redirect_uri']}?{urlencode(params)}"}

    def _issue(self, client_id: str, scope: str, grant: str) -> dict[str, Any]:
        access = "at-" + secrets.token_hex(8)
        refresh = "rt-" + secrets.token_hex(8)
        self.access[access] = {"scope": scope, "grant": grant}
        self.refresh[refresh] = {"client_id": client_id, "scope": scope, "grant": grant}
        return {"access_token": access, "refresh_token": refresh, "token_type": "bearer", "expires_in": self.expires_in, "scope": scope}

    def _token(self, req: Request) -> tuple[int, Any]:
        body = req.body or {}
        if self.token_status is not None and body.get("grant_type") == "refresh_token":
            return self.token_status
        with self.lock:
            if "token" in body and "grant_type" not in body:  # RFC 7009 revocation
                record = self.refresh.pop(body["token"], None)
                if record:
                    self.revoked_grants.add(record["grant"])
                return 200, {}
            if body.get("client_id") not in self.clients:
                return 401, {"error": "invalid_client"}
            if body.get("grant_type") == "authorization_code":
                record = self.codes.pop(body.get("code", ""), None)
                if record is None or record["client_id"] != body["client_id"] or record["redirect_uri"] != body.get("redirect_uri"):
                    return 400, {"error": "invalid_grant"}
                verifier = body.get("code_verifier", "")
                challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
                if challenge != record["challenge"]:
                    return 400, {"error": "invalid_grant", "error_description": "PKCE verification failed"}
                return 200, self._issue(body["client_id"], record["scope"], "grant-" + secrets.token_hex(4))
            if body.get("grant_type") == "refresh_token":
                token = body.get("refresh_token", "")
                if token in self.redeemed:
                    # Replay detection: revoke the grant (RFC 9700 section 4.14.2).
                    self.revoked_grants.update(r["grant"] for r in self.refresh.values())
                    return 400, {"error": "invalid_grant", "error_description": "refresh token reused"}
                record = self.refresh.pop(token, None)
                if record is None or record["grant"] in self.revoked_grants:
                    return 400, {"error": "invalid_grant"}
                self.redeemed.add(token)
                self.refresh_count += 1
                return 200, self._issue(record["client_id"], record["scope"], record["grant"])
        return 400, {"error": "unsupported_grant_type"}

    # MCP ---------------------------------------------------------------------

    def _mcp(self, req: Request) -> tuple[int, Any, dict[str, str]]:
        auth = req.headers.get("Authorization", "")
        token = auth[7:] if auth.startswith("Bearer ") else ""
        record = self.access.get(token)
        if record is None or record["grant"] in self.revoked_grants:
            return 401, {"error": "invalid_token"}, {"WWW-Authenticate": 'Bearer error="invalid_token"'}
        accept = req.headers.get("Accept", "")
        if "application/json" not in accept or "text/event-stream" not in accept:
            return 406, {"error": "Not Acceptable"}, {}
        message = req.body or {}
        method = message.get("method")
        rid = message.get("id")
        if method == "initialize":
            return 200, {"jsonrpc": "2.0", "id": rid, "result": {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "Notamify"}}}, {}
        if method == "tools/list":
            tools = [{"name": name, "inputSchema": {"type": "object"}} for name in self.tools]
            return 200, {"jsonrpc": "2.0", "id": rid, "result": {"tools": tools}}, {}
        if method == "tools/call":
            name = message["params"]["name"]
            args = message["params"].get("arguments", {})
            generation = name in ("generate_airport_briefing", "create_flight_briefing", "prioritize_notam_for_flight")
            if generation and "briefings:write" not in record["scope"].split():
                return 403, {"error": "insufficient_scope"}, {"WWW-Authenticate": 'Bearer error="insufficient_scope", scope="notams:read briefings:write"'}
            self.tool_calls.append((name, args))
            handler = self.tools.get(name)
            if handler is None:
                return 200, {"jsonrpc": "2.0", "id": rid, "error": {"code": -32602, "message": f"Tool {name} not found"}}, {}
            try:
                data = handler(args)
            except ToolFailure as failure:
                return 200, {"jsonrpc": "2.0", "id": rid, "result": {"isError": True, "content": [{"type": "text", "text": failure.text}]}}, {}
            return 200, {"jsonrpc": "2.0", "id": rid, "result": {"content": [{"type": "text", "text": "{}"}], "structuredContent": data}}, {}
        return 200, {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "Method not found"}}, {}


class ToolFailure(Exception):
    def __init__(self, text: str) -> None:
        super().__init__(text)
        self.text = text


def approve_in_background(url: str) -> None:
    """Stand in for the browser: follow the authorize redirect to the CLI's loopback callback."""
    from urllib import request

    def run() -> None:
        try:
            request.urlopen(url, timeout=10).read()
        except Exception:
            pass

    threading.Thread(target=run, daemon=True).start()
