#!/usr/bin/env python3

from __future__ import annotations

import os
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

from notamify_sdk import APIError, CloudflaredError, CloudflaredManager, Listener, ListenerMode, NotamifyClient

DEFAULT_LISTENER_NAME = "notamify-sdk-sandbox-example"


def require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise SystemExit(f"{name} is required")
    return value


def is_quick_tunnel_url(raw_url: str) -> bool:
    host = (urlparse(raw_url).hostname or "").strip().lower()
    return host == "trycloudflare.com" or host.endswith(".trycloudflare.com")


def parse_http_url(raw_url: str, label: str) -> str:
    parsed = urlparse(raw_url.strip())
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise SystemExit(f"{label} must be a valid http(s) URL")
    path = parsed.path or "/"
    normalized = parsed._replace(path=path, fragment="")
    return normalized.geturl()


def url_origin(raw_url: str) -> str:
    parsed = urlparse(raw_url)
    return f"{parsed.scheme}://{parsed.netloc}"


def build_watcher_webhook_url(public_tunnel_url: str, local_app_url: str) -> str:
    public = urlparse(public_tunnel_url)
    local = urlparse(local_app_url)
    path = local.path or "/"
    query = local.query
    out = public._replace(path=path, query=query, fragment="")
    return out.geturl()


def choose_listener(
    listeners: list[Listener],
    preferred_listener_id: str,
    preferred_listener_name: str,
) -> Listener | None:
    if preferred_listener_id:
        for listener in listeners:
            if listener.id == preferred_listener_id:
                if listener.mode != ListenerMode.sandbox:
                    raise SystemExit(
                        f"listener must be sandbox mode when using NOTAMIFY_LISTENER_ID: {preferred_listener_id}"
                    )
                return listener
        raise SystemExit(f"listener not found: {preferred_listener_id}")

    for listener in listeners:
        if listener.name == preferred_listener_name and listener.mode == ListenerMode.sandbox:
            return listener

    for listener in listeners:
        if listener.active and listener.mode == ListenerMode.sandbox and is_quick_tunnel_url(listener.webhook_url):
            return listener

    return None


def upsert_sandbox_listener(
    client: NotamifyClient,
    public_webhook_url: str,
    preferred_listener_id: str,
    preferred_listener_name: str,
) -> Listener:
    listeners = client.list_listeners()
    current = choose_listener(listeners, preferred_listener_id, preferred_listener_name)
    if current is None:
        created = client.create_listener(
            webhook_url=public_webhook_url,
            name=preferred_listener_name,
            filters={},
            active=True,
            mode=ListenerMode.sandbox,
            lifecycle={"enabled": False},
        )
        print(f"created sandbox listener id={created.id}")
        return created

    updated = client.update_listener(
        current.id,
        webhook_url=public_webhook_url,
        email=current.email,
        filters=current.filters,
        name=current.name or preferred_listener_name,
        active=True,
        mode=ListenerMode.sandbox,
        lifecycle=current.lifecycle,
    )
    print(f"updated sandbox listener id={updated.id}")
    return updated


def wait_for_tunnel_reachable(public_url: str, attempts: int = 12, delay_seconds: float = 2.0) -> None:
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(public_url, timeout=5):
                return
        except urllib.error.HTTPError:
            return
        except Exception as exc:
            last_error = exc
            if attempt < attempts:
                time.sleep(delay_seconds)

    raise RuntimeError(f"tunnel did not become reachable: {public_url}") from last_error


def is_retryable_dns_error(exc: APIError) -> bool:
    if exc.status < 500:
        return False
    msg = (exc.message or "").lower()
    return "no such host" in msg or "temporary failure in name resolution" in msg or ("dial tcp" in msg and "lookup" in msg)


def send_sandbox_with_retry(client: NotamifyClient, listener_id: str, max_attempts: int = 15, delay_seconds: float = 4.0):
    attempt = 1
    while attempt <= max_attempts:
        try:
            return client.send_sandbox_message(listener_id)
        except APIError as exc:
            if not is_retryable_dns_error(exc) or attempt >= max_attempts:
                raise
            print(f"Transient tunnel DNS failure from watcher; retrying sandbox send ({attempt}/{max_attempts})...")
            time.sleep(delay_seconds)
            attempt += 1
    raise RuntimeError("sandbox send retry loop exhausted")


def main() -> int:
    token = require_env("NOTAMIFY_TOKEN")
    local_app_url = parse_http_url(
        os.getenv("LOCAL_APP_URL", "http://127.0.0.1:8080/webhooks/notamify").strip(),
        "LOCAL_APP_URL",
    )
    local_origin_url = url_origin(local_app_url)
    preferred_listener_id = os.getenv("NOTAMIFY_LISTENER_ID", "").strip()
    preferred_listener_name = os.getenv("NOTAMIFY_LISTENER_NAME", DEFAULT_LISTENER_NAME).strip() or DEFAULT_LISTENER_NAME

    client = NotamifyClient(token=token)
    cloudflared = CloudflaredManager(local_url=local_origin_url)

    try:
        tunnel = cloudflared.start()
        print(f"Local app origin: {tunnel.local_url}")
        print(f"Local app webhook URL: {local_app_url}")
        print(f"Quick tunnel URL: {tunnel.public_url}")
        watcher_webhook_url = build_watcher_webhook_url(tunnel.public_url, local_app_url)
        print(f"Watcher webhook_url: {watcher_webhook_url}")

        listener = upsert_sandbox_listener(
            client=client,
            public_webhook_url=watcher_webhook_url,
            preferred_listener_id=preferred_listener_id,
            preferred_listener_name=preferred_listener_name,
        )

        wait_for_tunnel_reachable(watcher_webhook_url)
        result = send_sandbox_with_retry(client, listener.id)

        print("sandbox payload dispatched")
        print(f"listener_id={result.listener_id}")
        print(f"mode={result.mode.value}")
        print(f"notam_id={result.notam_id}")
        print(f"sent_at={result.sent_at}")
        input("Press ENTER to stop tunnel.")
    except CloudflaredError as exc:
        print(f"Cloudflared error: {exc}")
        return 1
    except APIError as exc:
        print(f"API error status={exc.status} message={exc.message}")
        return 1
    except RuntimeError as exc:
        print(f"Tunnel readiness error: {exc}")
        return 1
    finally:
        cloudflared.stop()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
