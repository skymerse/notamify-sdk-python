from __future__ import annotations

from typing import Any, Mapping

from notamify_sdk.client import APIError, NotamifyClient, SDK_VERSION
from notamify_sdk.models import Listener, ListenerLifecycleRequest, ListenerMode

_DEFAULT_BASE_URL = "https://watcher.notamify.com"


class WatcherClient(NotamifyClient):
    def __init__(
        self,
        token: str,
        base_url: str = _DEFAULT_BASE_URL,
        timeout: float = 10.0,
        user_agent: str = f"notamify-watcher-sdk-python/{SDK_VERSION}",
    ) -> None:
        super().__init__(
            token=token,
            watcher_base_url=base_url,
            timeout=timeout,
            user_agent=user_agent,
        )

    @property
    def base_url(self) -> str:
        return self.watcher_base_url

    @base_url.setter
    def base_url(self, value: str) -> None:
        self.watcher_base_url = value.rstrip("/")

    def create_listener(
        self,
        webhook_url: str,
        emails: list[str] | None = None,
        filters: Mapping[str, Any] | None = None,
        name: str = "",
        active: bool | None = None,
        mode: ListenerMode | str | None = None,
        lifecycle: ListenerLifecycleRequest | Mapping[str, Any] | None = None,
        lifecycle_enabled: bool | None = None,
    ) -> Listener:
        return super().create_listener(
            webhook_url=webhook_url,
            emails=emails,
            filters=filters,
            name=name,
            active=active,
            mode=mode,
            lifecycle=lifecycle,
            lifecycle_enabled=lifecycle_enabled,
        )

    def update_listener(
        self,
        listener_id: str,
        webhook_url: str,
        emails: list[str] | None = None,
        filters: Mapping[str, Any] | None = None,
        name: str = "",
        active: bool | None = None,
        mode: ListenerMode | str | None = None,
        lifecycle: ListenerLifecycleRequest | Mapping[str, Any] | None = None,
        lifecycle_enabled: bool | None = None,
    ) -> Listener:
        return super().update_listener(
            listener_id=listener_id,
            webhook_url=webhook_url,
            emails=emails,
            filters=filters,
            name=name,
            active=active,
            mode=mode,
            lifecycle=lifecycle,
            lifecycle_enabled=lifecycle_enabled,
        )
