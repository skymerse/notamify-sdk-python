"""Sign in with a Notamify account and call Notamify through the hosted MCP server.

This is the same sign-in the Notamify plugins and connectors use: OAuth 2.1 with
PKCE against https://mcp.notamify.com, consent in the browser on notamify.com,
and no API key. Requests are billed to the account at the connected-app rate.

Standard library only. The session lives in its own file (mode 0600). Refresh
tokens rotate and can be redeemed once; redeeming one twice revokes the whole
connection, so refreshing is serialised across processes with a file lock.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import re
import secrets
import threading
import time
import webbrowser
from dataclasses import asdict, dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence
from urllib import error, request
from urllib.parse import parse_qs, urlencode, urlsplit

from .config import ConfigStore, write_private_json

DEFAULT_MCP_URL = "https://mcp.notamify.com/mcp"
SCOPES: tuple[str, ...] = ("notams:read", "briefings:write")
CLIENT_NAME = "Notamify CLI"
CALLBACK_PATH = "/callback"
# Loopback redirect URIs match on any port (RFC 8252 section 7.3), so one registration serves every login.
REGISTERED_REDIRECT_URI = f"http://127.0.0.1{CALLBACK_PATH}"
MCP_PROTOCOL_VERSION = "2025-06-18"
# Registrations expire after 90 days on the server; register again before that.
CLIENT_REREGISTER_AFTER = 80 * 86400
REFRESH_MARGIN = 60


class AccountError(Exception):
    """A sign-in, session or connected-app request failed.

    ``code`` is one of: not_signed_in, session_expired, insufficient_scope, login_failed,
    login_denied, login_timeout, insufficient_credits, forbidden, not_found,
    invalid_request, rate_limited, unavailable, tool_error.
    """

    def __init__(self, code: str, message: str, *, status: int | None = None, retry_after: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.retry_after = retry_after

    def __str__(self) -> str:
        return self.message


@dataclass
class AccountSession:
    issuer: str
    resource: str
    client_id: str
    access_token: str
    refresh_token: str
    expires_at: float
    scope: str
    signed_in_at: float = field(default_factory=time.time)
    refreshed_at: float = field(default_factory=time.time)

    @property
    def scopes(self) -> list[str]:
        return [s for s in self.scope.split() if s]

    def expired(self, margin: float = REFRESH_MARGIN) -> bool:
        return time.time() >= self.expires_at - margin


def default_mcp_url() -> str:
    return os.getenv("NOTAMIFY_MCP_URL", "").strip() or DEFAULT_MCP_URL


class AccountStore:
    """The signed-in session and the CLI's client registration, in one private file."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or self.default_path()

    @staticmethod
    def default_path() -> Path:
        custom = os.getenv("NOTAMIFY_ACCOUNT_FILE", "").strip()
        if custom:
            return Path(custom)
        return ConfigStore.default_path().parent / "account.json"

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return raw if isinstance(raw, dict) else {}

    def _write(self, data: Mapping[str, Any]) -> None:
        write_private_json(self.path, dict(data))

    def load(self) -> AccountSession | None:
        raw = self._read().get("session")
        if not isinstance(raw, dict):
            return None
        try:
            return AccountSession(**{k: raw[k] for k in AccountSession.__dataclass_fields__ if k in raw})
        except TypeError:
            return None

    def save(self, session: AccountSession) -> None:
        data = self._read()
        data["session"] = asdict(session)
        self._write(data)

    def clear_session(self) -> None:
        data = self._read()
        if data.pop("session", None) is not None:
            self._write(data)

    def client(self, issuer: str) -> str | None:
        raw = self._read().get("client")
        if not isinstance(raw, dict) or raw.get("issuer") != issuer or not raw.get("client_id"):
            return None
        if time.time() - float(raw.get("registered_at") or 0) > CLIENT_REREGISTER_AFTER:
            return None
        return str(raw["client_id"])

    def save_client(self, issuer: str, client_id: str) -> None:
        data = self._read()
        data["client"] = {"issuer": issuer, "client_id": client_id, "registered_at": time.time()}
        self._write(data)

    def clear_client(self) -> None:
        data = self._read()
        if data.pop("client", None) is not None:
            self._write(data)

    @contextlib.contextmanager
    def lock(self) -> Iterator[None]:
        """Exclusive lock across processes, held while a refresh token is redeemed."""
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock_path = self.path.with_name(self.path.name + ".lock")
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            try:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX)
            except ImportError:  # Windows (not covered by CI)
                import msvcrt

                deadline = time.monotonic() + 30
                while True:
                    try:
                        msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
                        break
                    except OSError:
                        if time.monotonic() > deadline:
                            raise AccountError("unavailable", f"timed out waiting for {lock_path}")
                        time.sleep(0.1)
            yield
        finally:
            os.close(fd)  # closing the descriptor releases the lock


# HTTP -----------------------------------------------------------------------


@dataclass
class _Response:
    status: int
    headers: Mapping[str, str]
    body: bytes

    def json(self) -> Any:
        try:
            return json.loads(self.body.decode("utf-8")) if self.body else {}
        except ValueError:
            return None


def _http(
    method: str,
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    form: Mapping[str, str] | None = None,
    body: Any = None,
    timeout: float = 30.0,
) -> _Response:
    data = None
    sent_headers = {"User-Agent": _user_agent(), **dict(headers or {})}
    if form is not None:
        data = urlencode(form).encode("utf-8")
        sent_headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif body is not None:
        data = json.dumps(body).encode("utf-8")
        sent_headers["Content-Type"] = "application/json"
    req = request.Request(url, data=data, method=method, headers=sent_headers)
    try:
        with request.urlopen(req, timeout=timeout) as resp:
            return _Response(resp.status, dict(resp.headers), resp.read())
    except error.HTTPError as exc:
        return _Response(exc.code, dict(exc.headers or {}), exc.read() if exc.fp else b"")
    except (error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise AccountError("unavailable", f"cannot reach Notamify: {reason}") from exc


def _user_agent() -> str:
    from .client import SDK_VERSION

    return f"notamify-sdk-python/{SDK_VERSION}"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


# OAuth ----------------------------------------------------------------------


class AccountAuth:
    """Browser sign-in, token refresh and sign-out for a Notamify account."""

    def __init__(self, mcp_url: str | None = None, store: AccountStore | None = None, timeout: float = 30.0) -> None:
        self.mcp_url = (mcp_url or default_mcp_url()).rstrip("/")
        self.store = store or AccountStore()
        self.timeout = timeout
        self._metadata: dict[str, Any] | None = None

    # Discovery
    def metadata(self) -> dict[str, Any]:
        if self._metadata is None:
            parts = urlsplit(self.mcp_url)
            resource_meta = _http(
                "GET", f"{_origin(self.mcp_url)}/.well-known/oauth-protected-resource{parts.path}", timeout=self.timeout
            ).json() or {}
            servers = resource_meta.get("authorization_servers") or [_origin(self.mcp_url)]
            issuer = str(servers[0]).rstrip("/")
            response = _http("GET", f"{issuer}/.well-known/oauth-authorization-server", timeout=self.timeout)
            meta = response.json()
            if response.status != 200 or not isinstance(meta, dict) or not meta.get("token_endpoint"):
                raise AccountError("unavailable", "Notamify sign-in is unavailable (no authorization server metadata).")
            meta.setdefault("issuer", issuer)
            meta["resource"] = resource_meta.get("resource") or self.mcp_url
            self._metadata = meta
        return self._metadata

    def _client_id(self, *, force_new: bool = False) -> str:
        meta = self.metadata()
        issuer = str(meta["issuer"])
        if not force_new:
            existing = self.store.client(issuer)
            if existing:
                return existing
        endpoint = meta.get("registration_endpoint")
        if not endpoint:
            raise AccountError("login_failed", "Notamify sign-in does not offer client registration.")
        response = _http(
            "POST",
            str(endpoint),
            body={
                "client_name": CLIENT_NAME,
                "redirect_uris": [REGISTERED_REDIRECT_URI],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "none",
                "scope": " ".join(SCOPES),
            },
            timeout=self.timeout,
        )
        payload = response.json() or {}
        if response.status not in (200, 201) or not payload.get("client_id"):
            raise AccountError("login_failed", f"client registration failed: {_oauth_message(payload, response)}")
        self.store.save_client(issuer, str(payload["client_id"]))
        return str(payload["client_id"])

    # Sign-in
    def login(
        self,
        *,
        scopes: Sequence[str] = SCOPES,
        open_browser: bool = True,
        port: int = 0,
        timeout: float = 300.0,
        on_url: Callable[[str], None] | None = None,
    ) -> AccountSession:
        """Sign in through the browser and store the session. Blocks until the browser returns or ``timeout``."""
        meta = self.metadata()
        client_id = self._client_id()
        verifier = _b64url(secrets.token_bytes(48))
        challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
        state = _b64url(secrets.token_bytes(24))

        callback = _LoopbackCallback(port)
        try:
            redirect_uri = f"http://127.0.0.1:{callback.port}{CALLBACK_PATH}"
            url = f"{meta['authorization_endpoint']}?" + urlencode(
                {
                    "response_type": "code",
                    "client_id": client_id,
                    "redirect_uri": redirect_uri,
                    "scope": " ".join(scopes),
                    "state": state,
                    "code_challenge": challenge,
                    "code_challenge_method": "S256",
                    "resource": meta["resource"],
                }
            )
            if on_url is not None:
                on_url(url)
            if open_browser:
                try:
                    webbrowser.open(url)
                except webbrowser.Error:
                    pass
            params = callback.wait(timeout)
        finally:
            callback.close()

        if params is None:
            raise AccountError("login_timeout", f"no response from the browser within {int(timeout)} seconds")
        if params.get("state") != state:
            raise AccountError("login_failed", "the sign-in response did not match this request (state mismatch)")
        if params.get("iss") and params["iss"].rstrip("/") != str(meta["issuer"]).rstrip("/"):
            raise AccountError("login_failed", "the sign-in response came from an unexpected issuer")
        if params.get("error"):
            if params["error"] == "access_denied":
                raise AccountError("login_denied", "Sign-in was cancelled.")
            raise AccountError("login_failed", params.get("error_description") or params["error"])
        if not params.get("code"):
            raise AccountError("login_failed", "the sign-in response had no authorization code")

        response = _http(
            "POST",
            str(meta["token_endpoint"]),
            form={
                "grant_type": "authorization_code",
                "code": params["code"],
                "redirect_uri": redirect_uri,
                "client_id": client_id,
                "code_verifier": verifier,
                "resource": str(meta["resource"]),
            },
            timeout=self.timeout,
        )
        tokens = response.json() or {}
        if response.status != 200 or not tokens.get("access_token"):
            if tokens.get("error") == "invalid_client":
                self.store.clear_client()
            raise AccountError("login_failed", f"token exchange failed: {_oauth_message(tokens, response)}")
        session = AccountSession(
            issuer=str(meta["issuer"]),
            resource=str(meta["resource"]),
            client_id=client_id,
            access_token=str(tokens["access_token"]),
            refresh_token=str(tokens.get("refresh_token") or ""),
            expires_at=time.time() + _expires_in(tokens),
            scope=str(tokens.get("scope") or " ".join(scopes)),
        )
        self.store.save(session)
        return session

    # Tokens
    def session(self) -> AccountSession | None:
        return self.store.load()

    def access_token(self) -> str:
        """A valid access token, refreshing it first if it is about to expire."""
        session = self.store.load()
        if session is None:
            raise AccountError("not_signed_in", "Not signed in to a Notamify account.")
        if not session.expired():
            return session.access_token
        return self._refresh_locked(stale_token=session.access_token)

    def refresh_after_rejection(self, rejected_token: str) -> str:
        """Called after the server rejected ``rejected_token``; refreshes unless another process already did."""
        return self._refresh_locked(stale_token=rejected_token, force=True)

    def _refresh_locked(self, *, stale_token: str, force: bool = False) -> str:
        with self.store.lock():
            session = self.store.load()
            if session is None:
                raise AccountError("not_signed_in", "Not signed in to a Notamify account.")
            # Another process refreshed while this one waited for the lock.
            if session.access_token != stale_token and not session.expired():
                return session.access_token
            if not force and not session.expired():
                return session.access_token
            return self._refresh(session).access_token

    def _refresh(self, session: AccountSession) -> AccountSession:
        if not session.refresh_token:
            self.store.clear_session()
            raise AccountError("session_expired", "The Notamify session expired. Sign in again.")
        meta = self.metadata()
        response = _http(
            "POST",
            str(meta["token_endpoint"]),
            form={
                "grant_type": "refresh_token",
                "refresh_token": session.refresh_token,
                "client_id": session.client_id,
                "resource": session.resource,
            },
            timeout=self.timeout,
        )
        tokens = response.json() or {}
        if response.status == 200 and tokens.get("access_token"):
            session.access_token = str(tokens["access_token"])
            session.refresh_token = str(tokens.get("refresh_token") or session.refresh_token)
            session.expires_at = time.time() + _expires_in(tokens)
            session.scope = str(tokens.get("scope") or session.scope)
            session.refreshed_at = time.time()
            self.store.save(session)
            return session
        if response.status >= 500 or tokens.get("error") == "temporarily_unavailable":
            raise AccountError("unavailable", "Notamify sign-in is temporarily unavailable.", status=response.status)
        if tokens.get("error") == "invalid_client":
            self.store.clear_client()
        self.store.clear_session()
        raise AccountError(
            "session_expired",
            f"The Notamify session ended ({_oauth_message(tokens, response)}). Sign in again.",
            status=response.status,
        )

    def logout(self) -> bool:
        """Revoke the refresh token (best effort) and forget the session. Returns whether one existed."""
        with self.store.lock():
            session = self.store.load()
            if session is None:
                return False
            try:
                meta = self.metadata()
                endpoint = meta.get("revocation_endpoint")
                if endpoint and session.refresh_token:
                    _http(
                        "POST",
                        str(endpoint),
                        form={"token": session.refresh_token, "token_type_hint": "refresh_token", "client_id": session.client_id},
                        timeout=self.timeout,
                    )
            except AccountError:
                pass
            self.store.clear_session()
            return True


def _expires_in(tokens: Mapping[str, Any]) -> float:
    value = tokens.get("expires_in")
    try:
        return float(value) if value is not None else 900.0
    except (TypeError, ValueError):
        return 900.0


def _oauth_message(payload: Any, response: _Response) -> str:
    if isinstance(payload, dict):
        text = payload.get("error_description") or payload.get("error") or payload.get("message")
        if text:
            return str(text)
    return f"HTTP {response.status}"


class _LoopbackCallback:
    """One-shot HTTP server on 127.0.0.1 that receives the OAuth redirect."""

    def __init__(self, port: int = 0) -> None:
        self._result: dict[str, str] | None = None
        self._done = threading.Event()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                url = urlsplit(self.path)
                if url.path != CALLBACK_PATH:
                    self.send_response(HTTPStatus.NOT_FOUND)
                    self.end_headers()
                    return
                params = {k: v[0] for k, v in parse_qs(url.query).items() if v}
                ok = "code" in params and "error" not in params
                page = (
                    "<h1>Signed in to Notamify</h1><p>You can close this tab and return to the terminal.</p>"
                    if ok
                    else "<h1>Sign-in did not complete</h1><p>Return to the terminal for details.</p>"
                ).encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(page)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Referrer-Policy", "no-referrer")
                self.end_headers()
                self.wfile.write(page)
                if not owner._done.is_set():
                    owner._result = params
                    owner._done.set()

            def log_message(self, *args: Any) -> None:
                return

        try:
            self._server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        except OSError as exc:
            raise AccountError("login_failed", f"cannot listen on 127.0.0.1:{port} for the sign-in callback: {exc}") from exc
        self.port = int(self._server.server_address[1])
        self._thread = threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
        self._thread.start()

    def wait(self, timeout: float) -> dict[str, str] | None:
        return self._result if self._done.wait(timeout) else None

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


# MCP --------------------------------------------------------------------------

_RETRY_AFTER = re.compile(r"retry in (\d+) seconds", re.I)


def tool_error(message: str) -> AccountError:
    """Map a connected-app tool failure, which carries only text, to an error code."""
    text = message or "Notamify could not complete this request."
    lowered = text.lower()
    if "insufficient api credits" in lowered:
        return AccountError("insufficient_credits", text)
    if "pro subscription" in lowered or "cannot use this api operation" in lowered:
        return AccountError("forbidden", text)
    if "reconnect this app" in lowered or "approve briefing generation" in lowered:
        return AccountError("insufficient_scope", text)
    if "not found" in lowered:
        return AccountError("not_found", text)
    match = _RETRY_AFTER.search(text)
    if "usage limit reached" in lowered:
        return AccountError("rate_limited", text, retry_after=int(match.group(1)) if match else None)
    if "temporarily unavailable" in lowered or "please try again" in lowered:
        return AccountError("unavailable", text)
    if (
        "rejected this query" in lowered
        or "must" in lowered
        or "specify" in lowered
        or "invalid" in lowered
        or "too large" in lowered
        or "already used" in lowered
    ):
        return AccountError("invalid_request", text)
    return AccountError("tool_error", text)


class McpClient:
    """Minimal client for the hosted Notamify MCP server (stateless Streamable HTTP, JSON responses)."""

    def __init__(self, auth: AccountAuth, *, timeout: float = 120.0, client_version: str | None = None) -> None:
        self.auth = auth
        self.url = auth.mcp_url
        self.timeout = timeout
        self.client_version = client_version
        self._protocol_version: str | None = None
        self._next_id = 0

    def list_tools(self) -> list[dict[str, Any]]:
        result = self._rpc("tools/list", {})
        tools = result.get("tools") if isinstance(result, dict) else None
        return tools if isinstance(tools, list) else []

    def call_tool(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        result = self._rpc("tools/call", {"name": name, "arguments": dict(arguments)})
        if not isinstance(result, dict):
            raise AccountError("tool_error", f"{name} returned an unexpected result")
        text = " ".join(
            str(item.get("text", ""))
            for item in result.get("content") or []
            if isinstance(item, dict) and item.get("type") == "text"
        ).strip()
        if result.get("isError"):
            raise tool_error(text)
        data = result.get("structuredContent")
        if isinstance(data, dict):
            return data
        try:
            parsed = json.loads(text)
        except ValueError as exc:
            raise AccountError("tool_error", f"{name} returned an unexpected result") from exc
        if not isinstance(parsed, dict):
            raise AccountError("tool_error", f"{name} returned an unexpected result")
        return parsed

    def _initialize(self) -> None:
        if self._protocol_version is not None:
            return
        result = self._post(
            "initialize",
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "notamify-sdk-python", "version": self.client_version or _user_agent().split("/")[-1]},
            },
        )
        version = result.get("protocolVersion") if isinstance(result, dict) else None
        self._protocol_version = str(version or MCP_PROTOCOL_VERSION)

    def _rpc(self, method: str, params: Mapping[str, Any]) -> Any:
        self._initialize()
        return self._post(method, params)

    def _post(self, method: str, params: Mapping[str, Any]) -> Any:
        self._next_id += 1
        message = {"jsonrpc": "2.0", "id": self._next_id, "method": method, "params": dict(params)}
        token = self.auth.access_token()
        for attempt in (1, 2):
            headers = {
                "Authorization": f"Bearer {token}",
                "Accept": "application/json, text/event-stream",
            }
            if self._protocol_version:
                headers["MCP-Protocol-Version"] = self._protocol_version
            response = _http("POST", self.url, headers=headers, body=message, timeout=self.timeout)
            if response.status == 401 and attempt == 1:
                token = self.auth.refresh_after_rejection(token)
                continue
            break
        return self._result(response, message["id"])

    def _result(self, response: _Response, request_id: int) -> Any:
        challenge = next((v for k, v in response.headers.items() if k.lower() == "www-authenticate"), "")
        if response.status == 401:
            raise AccountError("session_expired", "The Notamify session was rejected. Sign in again.", status=401)
        if response.status == 403 and "insufficient_scope" in challenge:
            raise AccountError(
                "insufficient_scope",
                "This needs briefing permission. Sign in again and approve briefing generation.",
                status=403,
            )
        payload = self._decode(response, request_id)
        if response.status >= 400 or payload is None:
            message = payload.get("error") if isinstance(payload, dict) and isinstance(payload.get("error"), str) else None
            message = message or f"HTTP {response.status}"
            code = {400: "invalid_request", 403: "forbidden", 404: "not_found", 413: "invalid_request", 429: "rate_limited"}.get(
                response.status, "unavailable" if response.status >= 500 or response.status == 0 else "tool_error"
            )
            raise AccountError(code, message, status=response.status)
        if isinstance(payload.get("error"), dict):
            err = payload["error"]
            code = "invalid_request" if err.get("code") in (-32600, -32602) else "tool_error"
            raise AccountError(code, str(err.get("message") or "request failed"))
        return payload.get("result")

    @staticmethod
    def _decode(response: _Response, request_id: int) -> dict[str, Any] | None:
        content_type = next((v for k, v in response.headers.items() if k.lower() == "content-type"), "")
        text = response.body.decode("utf-8", errors="replace")
        if "text/event-stream" in content_type:
            for block in text.split("\n\n"):
                data = "\n".join(line[5:].lstrip() for line in block.splitlines() if line.startswith("data:"))
                if not data:
                    continue
                try:
                    message = json.loads(data)
                except ValueError:
                    continue
                if isinstance(message, dict) and message.get("id") == request_id:
                    return message
            return None
        try:
            payload = json.loads(text) if text else None
        except ValueError:
            return None
        return payload if isinstance(payload, dict) else None


def billing(mcp_url: str | None = None, timeout: float = 10.0) -> dict[str, Any] | None:
    """The public connected-app pricing policy, or None when it is unavailable."""
    url = f"{_origin(mcp_url or default_mcp_url())}/billing"
    try:
        response = _http("GET", url, timeout=timeout)
    except AccountError:
        return None
    payload = response.json()
    return payload if response.status == 200 and isinstance(payload, dict) else None
