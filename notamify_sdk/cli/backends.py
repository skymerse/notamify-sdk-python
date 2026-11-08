"""How CLI commands reach Notamify: with an API key, or with a signed-in Notamify account.

Both backends take the same operation names and payloads and return plain dicts.

- ``ApiKeyBackend`` calls the REST API through ``NotamifyClient``. Regular API pricing.
- ``AccountBackend`` calls the hosted MCP server's tools with the account's OAuth
  session, like the Notamify plugins and connectors. Connected-app pricing; list
  results carry summarised interpretations (``notams get`` has the full record).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Callable, Mapping

from pydantic import ValidationError

from ..account import AccountAuth, AccountError, McpClient, billing
from ..client import SDK_VERSION, APIError, NotamifyClient
from .output import (
    EXIT_AUTH,
    EXIT_ERROR,
    EXIT_FORBIDDEN,
    EXIT_NOT_FOUND,
    EXIT_TEMPORARY,
    EXIT_USAGE,
    CLIError,
    error_from_api,
    to_jsonable,
)

LIST_OPERATIONS = ("active", "raw", "nearby", "archive", "expired")
GENERATION_OPERATIONS = ("airport", "flight_create", "prioritize")

# Regular API credits per call (published in https://mcp.notamify.com/billing as api_rates).
API_CREDITS = {
    "active": 1,
    "raw": 1,
    "nearby": 1,
    "archive": 1,
    "expired": 1,
    "notam": 1,
    "airport": 2,
    "flight_create": 5,
    "flight_status": 0,
    "prioritize": 5,
}

ACCOUNT_TOOLS = {
    "active": "search_active_notams",
    "nearby": "search_nearby_notams",
    "archive": "search_archived_notams",
    "expired": "search_recently_expired_notams",
    "notam": "get_notam",
    "airport": "generate_airport_briefing",
    "flight_create": "create_flight_briefing",
    "flight_status": "get_flight_briefing",
    "prioritize": "prioritize_notam_for_flight",
}

ACCOUNT_RATE_KEYS = {"airport": "airport", "flight_create": "flight", "prioritize": "prioritization"}

RESPONSE_EXTRAS = ("retrieved_at", "source", "mcp_billing")


class Backend:
    mode = ""
    label = ""

    def supports(self, operation: str) -> bool:
        raise NotImplementedError

    def call(self, operation: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    def describe(self, operation: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    def cost(self, operation: str) -> int | None:
        """Credits per call, or None when unknown."""
        raise NotImplementedError


class ApiKeyBackend(Backend):
    mode = "api_key"
    label = "API key"

    def __init__(self, client_factory: Callable[[], NotamifyClient]) -> None:
        self._factory = client_factory
        self._client: NotamifyClient | None = None

    @property
    def client(self) -> NotamifyClient:
        if self._client is None:
            self._client = self._factory()
        return self._client

    def supports(self, operation: str) -> bool:
        return operation in API_CREDITS

    def call(self, operation: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        client = self.client
        calls: dict[str, Callable[[], Any]] = {
            "active": lambda: client.get_active_notams(payload),
            "raw": lambda: client.get_raw_notams(payload),
            "nearby": lambda: client.get_nearby_notams(payload),
            "archive": lambda: client.get_historical_notams(payload),
            "expired": lambda: client.get_recently_expired_notams(payload),
            "notam": lambda: client.get_notam(payload["notam_id"]),
            "airport": lambda: client.generate_airport_briefing(payload),
            "flight_create": lambda: client.create_async_briefing(payload),
            "flight_status": lambda: client.get_async_briefing_status(payload["uuid"]),
            "prioritize": lambda: client.prioritize_notam(payload),
        }
        try:
            result = calls[operation]()
        except APIError as exc:
            err = error_from_api(exc)
            if operation == "flight_status" and err.exit_code == EXIT_NOT_FOUND:
                err.remediation = "Briefings are visible only to the sign-in method that created them (API key or account)."
            raise err from exc
        except ValidationError as exc:
            # Request models are *Query / *Request; anything else is a response the API sent.
            if exc.title.endswith(("Query", "Request")):
                raise CLIError("invalid_arguments", _validation_message(exc), EXIT_USAGE) from exc
            raise CLIError(
                "invalid_response",
                f"unexpected response from Notamify: {_validation_message(exc)}",
                EXIT_ERROR,
                "Update the CLI (uv tool upgrade notamify); if it persists, report it.",
            ) from exc
        return to_jsonable(result)

    def describe(self, operation: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        client = self.client
        method, path, query, body = {
            "active": ("GET", "/notams", payload, None),
            "raw": ("GET", "/notams/raw", payload, None),
            "nearby": ("GET", "/notams/nearby", payload, None),
            "archive": ("GET", "/notams/archive", payload, None),
            "expired": ("GET", "/notams/recently-expired", payload, None),
            "notam": ("GET", f"/notams/{payload.get('notam_id')}", None, None),
            "airport": ("GET", "/notams/briefing/simple", payload, None),
            "flight_create": ("POST", "/notams/briefing", None, payload),
            "flight_status": ("GET", f"/notams/briefing/{payload.get('uuid')}", None, None),
            "prioritize": ("POST", "/notams/prioritisation", None, payload),
        }[operation]
        url = client.api_base_url + path
        if query:
            encoded = client._encode_query({k: v for k, v in query.items() if v is not None})
            url = f"{url}?{encoded}" if encoded else url
        request: dict[str, Any] = {"method": method, "url": url}
        if body is not None:
            request["body"] = to_jsonable(body)
        return {"auth": self.mode, "request": request, "authenticated": bool(client.token)}

    def cost(self, operation: str) -> int | None:
        return API_CREDITS.get(operation)


class AccountBackend(Backend):
    mode = "account"
    label = "Notamify account"

    def __init__(self, auth: AccountAuth | None = None, *, timeout: float = 120.0) -> None:
        self.auth = auth or AccountAuth()
        self._mcp: McpClient | None = None
        self._billing: dict[str, Any] | None = None
        self._billing_loaded = False
        self._timeout = timeout

    @property
    def mcp(self) -> McpClient:
        if self._mcp is None:
            self._mcp = McpClient(self.auth, timeout=self._timeout, client_version=SDK_VERSION)
        return self._mcp

    def supports(self, operation: str) -> bool:
        return operation in ACCOUNT_TOOLS

    def arguments(self, operation: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        args = to_jsonable(dict(payload))
        if operation in LIST_OPERATIONS and "location" in args:
            args["locations"] = args.pop("location")
        if operation == "notam":
            args = {"notam_id": args["notam_id"]}
        if operation == "flight_status":
            args = {"briefing_id": args["uuid"]}
        if operation == "flight_create" and not args.get("idempotency_key"):
            # The same flight request gets the same key, so a retry returns the accepted job instead of a new charge.
            canonical = json.dumps(args, sort_keys=True, separators=(",", ":"))
            args["idempotency_key"] = "cli-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:40]
        return args

    def call(self, operation: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        if not self.supports(operation):
            raise requires_api_key(operation)
        try:
            return self.mcp.call_tool(ACCOUNT_TOOLS[operation], self.arguments(operation, payload))
        except AccountError as exc:
            err = error_from_account(exc)
            if operation == "flight_status" and err.exit_code == EXIT_NOT_FOUND:
                err.remediation = "Briefings are visible only to the sign-in method that created them (API key or account)."
            raise err from exc

    def describe(self, operation: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        if not self.supports(operation):
            raise requires_api_key(operation)
        return {
            "auth": self.mode,
            "request": {"server": self.auth.mcp_url, "tool": ACCOUNT_TOOLS[operation], "arguments": self.arguments(operation, payload)},
            "authenticated": self.auth.session() is not None,
        }

    def cost(self, operation: str) -> int | None:
        if operation == "flight_status":
            return 0
        if not self._billing_loaded:
            self._billing = billing(self.auth.mcp_url)
            self._billing_loaded = True
        policy = self._billing
        if not policy:
            return None
        if not policy.get("promotion_active"):
            return API_CREDITS.get(operation)
        if operation in ACCOUNT_RATE_KEYS:
            rate = (policy.get("generation_rates") or {}).get(ACCOUNT_RATE_KEYS[operation])
            return int(rate) if rate is not None else None
        rate = policy.get("read_credits")
        return int(rate) if rate is not None else None


def credits_charged(response: Mapping[str, Any]) -> tuple[int | None, str | None]:
    receipt = response.get("mcp_billing") if isinstance(response, Mapping) else None
    if isinstance(receipt, Mapping) and isinstance(receipt.get("credits_used"), int):
        return receipt["credits_used"], receipt.get("pricing_mode")
    return None, None


def strip_extras(response: Mapping[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in response.items() if k not in RESPONSE_EXTRAS}


def requires_api_key(operation: str | None = None) -> CLIError:
    what = {"raw": "`notams raw`"}.get(operation or "", "Watcher commands" if operation == "watcher" else "This command")
    return CLIError(
        "api_key_required",
        f"{what} needs an API key; account sign-in covers NOTAMs and briefings only.",
        EXIT_AUTH,
        "Run `notamify auth login --api-key`, or set NOTAMIFY_TOKEN. Create a key at https://notamify.com/api-manager.",
    )


ACCOUNT_URL = "https://notamify.com/account#connections"


def error_from_account(exc: AccountError) -> CLIError:
    code = exc.code
    if code in ("not_signed_in", "session_expired"):
        return CLIError(code, exc.message, EXIT_AUTH, "Run `notamify auth login` to sign in again.")
    if code == "insufficient_scope":
        return CLIError(code, exc.message, EXIT_AUTH, "Run `notamify auth login` and approve briefing generation.")
    if code == "insufficient_credits":
        return CLIError(code, exc.message, EXIT_FORBIDDEN, f"Review credits at {ACCOUNT_URL}.")
    if code == "forbidden":
        return CLIError(code, exc.message, EXIT_FORBIDDEN, f"Review your account at {ACCOUNT_URL}.")
    if code == "not_found":
        return CLIError(code, exc.message, EXIT_NOT_FOUND)
    if code == "invalid_request":
        return CLIError("invalid_request", exc.message, EXIT_USAGE)
    if code in ("rate_limited", "unavailable", "login_timeout"):
        details = {"retry_after": exc.retry_after} if exc.retry_after else {}
        return CLIError(code, exc.message, EXIT_TEMPORARY, "Wait and retry.", details=details)
    return CLIError(code, exc.message, EXIT_ERROR)


def _validation_message(exc: ValidationError) -> str:
    parts = []
    for item in exc.errors()[:5]:
        location = ".".join(str(p) for p in item.get("loc", ()))
        parts.append(f"{location}: {item.get('msg')}" if location else str(item.get("msg")))
    return "; ".join(parts) or str(exc)
