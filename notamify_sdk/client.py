from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from importlib.metadata import PackageNotFoundError, version
from typing import Any, Mapping, TypeVar
from urllib import error, request
from urllib.parse import urlencode

from pydantic import BaseModel

from .models import (
    ActiveNotamsQuery,
    BriefingJobCreated,
    BriefingJobStatusDTO,
    GenerateFlightBriefingRequest,
    HistoricalNotamsQuery,
    Listener,
    ListenerFilters,
    ListenerMode,
    ListenerRequest,
    NearbyNotamsQuery,
    NotamListResult,
    NotamPrioritisationRequest,
    NotamPrioritisationResult,
    SandboxDeliveryResult,
    WebhookLogsResponse,
)

_DEFAULT_API_BASE_URL = "https://api.notamify.com/api/v2"
_DEFAULT_WATCHER_BASE_URL = "https://watcher.notamify.com"


def _package_version() -> str:
    try:
        return version("notamify-sdk")
    except PackageNotFoundError:
        return "0.1.0"


SDK_VERSION = _package_version()

ModelT = TypeVar("ModelT", bound=BaseModel)


@dataclass
class APIError(Exception):
    status: int
    message: str
    payload: Any = None

    def __str__(self) -> str:
        return f"APIError(status={self.status}, message={self.message})"


class NotamifyClient:
    def __init__(
        self,
        token: str,
        api_base_url: str = _DEFAULT_API_BASE_URL,
        watcher_base_url: str = _DEFAULT_WATCHER_BASE_URL,
        timeout: float = 10.0,
        user_agent: str = f"notamify-sdk-python/{SDK_VERSION}",
    ) -> None:
        self.token = token.strip()
        self.api_base_url = api_base_url.rstrip("/")
        self.watcher_base_url = watcher_base_url.rstrip("/")
        self.timeout = timeout
        self.user_agent = user_agent

    # Watcher API
    def list_listeners(self) -> list[Listener]:
        payload = self._request("GET", self.watcher_base_url, "/listeners")
        return [Listener.model_validate(x) for x in payload.get("listeners", [])]

    def create_listener(
        self,
        webhook_url: str | None = None,
        email: str = "",
        filters: ListenerFilters | Mapping[str, Any] | None = None,
        name: str = "",
        active: bool | None = None,
        mode: ListenerMode | str | None = None,
        lifecycle_enabled: bool | None = None,
    ) -> Listener:
        request_body: dict[str, Any] = {
            "webhook_url": self._normalize_listener_text(webhook_url),
            "email": self._normalize_listener_text(email),
            "filters": filters or {},
            "name": self._normalize_listener_text(name),
            "active": active,
            "mode": self._to_listener_mode(mode) if mode is not None else None,
            "lifecycle_enabled": lifecycle_enabled,
        }
        body = self._prepare_body(request_body, ListenerRequest)
        payload = self._request("POST", self.watcher_base_url, "/listeners", body=body)
        return Listener.model_validate(payload)

    def update_listener(
        self,
        listener_id: str,
        webhook_url: str | None = None,
        email: str = "",
        filters: ListenerFilters | Mapping[str, Any] | None = None,
        name: str = "",
        active: bool | None = None,
        mode: ListenerMode | str | None = None,
        lifecycle_enabled: bool | None = None,
    ) -> Listener:
        request_body: dict[str, Any] = {
            "webhook_url": self._normalize_listener_text(webhook_url),
            "email": self._normalize_listener_text(email),
            "filters": filters or {},
            "name": self._normalize_listener_text(name),
            "active": active,
            "mode": self._to_listener_mode(mode) if mode is not None else None,
            "lifecycle_enabled": lifecycle_enabled,
        }
        body = self._prepare_body(request_body, ListenerRequest)
        payload = self._request("PUT", self.watcher_base_url, f"/listeners/{listener_id}", body=body)
        return Listener.model_validate(payload)

    def delete_listener(self, listener_id: str) -> None:
        self._request("DELETE", self.watcher_base_url, f"/listeners/{listener_id}")

    def get_webhook_secret_masked(self) -> str:
        payload = self._request("GET", self.watcher_base_url, "/webhook-secret")
        return str(payload.get("webhook_secret_masked", ""))

    def rotate_webhook_secret(self) -> str:
        payload = self._request("POST", self.watcher_base_url, "/webhook-secret:rotate")
        return str(payload.get("webhook_secret", ""))

    def get_webhook_logs(self) -> WebhookLogsResponse:
        payload = self._request("GET", self.watcher_base_url, "/webhook-logs")
        return WebhookLogsResponse.model_validate(payload)

    def send_sandbox_message(self, listener_id: str, notam_id: str | None = None) -> SandboxDeliveryResult:
        body: dict[str, Any] = {}
        if notam_id:
            body["notam_id"] = notam_id
        payload = self._request("POST", self.watcher_base_url, f"/listeners/{listener_id}/sandbox:send", body=body)
        return SandboxDeliveryResult.model_validate(payload)

    # NOTAM API v2
    def get_active_notams(self, query: ActiveNotamsQuery | Mapping[str, Any] | None = None) -> NotamListResult:
        query_payload = self._prepare_query(query, ActiveNotamsQuery)
        payload = self._request("GET", self.api_base_url, "/notams", query=query_payload)
        return NotamListResult.model_validate(payload)

    def get_raw_notams(self, query: ActiveNotamsQuery | Mapping[str, Any] | None = None) -> NotamListResult:
        query_payload = self._prepare_query(query, ActiveNotamsQuery)
        payload = self._request("GET", self.api_base_url, "/notams/raw", query=query_payload)
        return NotamListResult.model_validate(payload)

    def get_nearby_notams(self, query: NearbyNotamsQuery | Mapping[str, Any]) -> NotamListResult:
        query_payload = self._prepare_query(query, NearbyNotamsQuery)
        payload = self._request("GET", self.api_base_url, "/notams/nearby", query=query_payload)
        return NotamListResult.model_validate(payload)

    def get_historical_notams(self, query: HistoricalNotamsQuery | Mapping[str, Any]) -> NotamListResult:
        query_payload = self._prepare_query(query, HistoricalNotamsQuery)
        payload = self._request("GET", self.api_base_url, "/notams/archive", query=query_payload)
        return NotamListResult.model_validate(payload)

    def create_async_briefing(
        self,
        payload: GenerateFlightBriefingRequest | Mapping[str, Any],
    ) -> BriefingJobCreated:
        body = self._prepare_body(payload, GenerateFlightBriefingRequest)
        response = self._request("POST", self.api_base_url, "/notams/briefing", body=body)
        return BriefingJobCreated.model_validate(response)

    def create_briefing(
        self,
        payload: GenerateFlightBriefingRequest | Mapping[str, Any],
    ) -> BriefingJobCreated:
        return self.create_async_briefing(payload)

    def get_async_briefing_status(self, uuid: str) -> BriefingJobStatusDTO:
        payload = self._request("GET", self.api_base_url, f"/notams/briefing/{uuid}")
        return BriefingJobStatusDTO.model_validate(payload)

    def get_briefing_status(self, uuid: str) -> BriefingJobStatusDTO:
        return self.get_async_briefing_status(uuid)

    def prioritize_notam(
        self,
        payload: NotamPrioritisationRequest | Mapping[str, Any],
    ) -> NotamPrioritisationResult:
        body = self._prepare_body(payload, NotamPrioritisationRequest)
        response = self._request("POST", self.api_base_url, "/notams/prioritisation", body=body)
        return NotamPrioritisationResult.model_validate(response)

    def prioritise_notam(
        self,
        payload: NotamPrioritisationRequest | Mapping[str, Any],
    ) -> NotamPrioritisationResult:
        return self.prioritize_notam(payload)

    def _request(
        self,
        method: str,
        base_url: str,
        path: str,
        body: Mapping[str, Any] | None = None,
        query: Mapping[str, Any] | None = None,
    ) -> Any:
        if not self.token:
            raise APIError(0, "token is empty")

        headers = {
            "Authorization": f"Bearer {self.token}",
            "User-Agent": self.user_agent,
        }

        target_url = base_url + path
        if query:
            encoded = self._encode_query(query)
            if encoded:
                target_url = f"{target_url}?{encoded}"

        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode("utf-8")

        req = request.Request(target_url, method=method, headers=headers, data=data)
        try:
            with request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
                if not raw:
                    return {}
                return json.loads(raw.decode("utf-8"))
        except error.HTTPError as exc:
            raw = exc.read().decode("utf-8") if exc.fp else ""
            message = raw or exc.reason
            payload = None
            if raw:
                try:
                    payload = json.loads(raw)
                    if isinstance(payload, dict) and "error" in payload:
                        message = str(payload["error"])
                except json.JSONDecodeError:
                    payload = raw
            raise APIError(exc.code, str(message), payload)
        except error.URLError as exc:
            raise APIError(0, str(exc.reason), None)

    def _prepare_query(self, payload: Mapping[str, Any] | BaseModel | None, schema: type[ModelT]) -> dict[str, Any]:
        if payload is None:
            return {}
        model = payload if isinstance(payload, schema) else schema.model_validate(payload)
        return model.model_dump(mode="python", exclude_none=True)

    def _prepare_body(self, payload: Mapping[str, Any] | BaseModel, schema: type[ModelT]) -> dict[str, Any]:
        model = payload if isinstance(payload, schema) else schema.model_validate(payload)
        return model.model_dump(mode="json", exclude_none=True)

    def _encode_query(self, payload: Mapping[str, Any]) -> str:
        flat: list[tuple[str, str]] = []
        for key, value in payload.items():
            if value is None:
                continue
            if isinstance(value, (list, tuple)):
                for item in value:
                    if item is None:
                        continue
                    flat.append((key, self._stringify_query_value(item)))
                continue
            flat.append((key, self._stringify_query_value(value)))
        return urlencode(flat, doseq=True)

    def _stringify_query_value(self, value: Any) -> str:
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, datetime):
            return value.isoformat().replace("+00:00", "Z")
        if isinstance(value, date):
            return value.isoformat()
        if isinstance(value, Enum):
            return str(value.value)
        return str(value)

    def _to_listener_mode(self, value: ListenerMode | str) -> str:
        if isinstance(value, ListenerMode):
            return value.value
        normalized = str(value).strip().lower()
        if normalized in {ListenerMode.prod.value, ListenerMode.sandbox.value}:
            return normalized
        raise ValueError("listener mode must be 'prod' or 'sandbox'")

    def _normalize_listener_text(self, value: str | None) -> str | None:
        if value is None:
            return None
        return str(value).strip()
