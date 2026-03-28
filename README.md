# notamify-sdk (Python)

Python SDK for Notamify public APIs:

- Notamify API v2 (`https://api.notamify.com/api/v2`)
- Watcher API (`https://watcher.notamify.com`)

## Features

- Typed API client with one auth token
- Pydantic response/request models
- Pager-style NOTAM listing via `client.notams.*` with item iteration and `pager.pages`
- Supported NOTAM endpoints:
  - `GET /notams`
  - `GET /notams/raw`
  - `GET /notams/nearby`
  - `GET /notams/archive`
  - `POST /notams/briefing`
  - `GET /notams/briefing/{uuid}`
  - `POST /notams/prioritisation`
- Watcher listener management + webhook logs/secrets
- Listener `mode` support (`prod` default, `sandbox` for test-only listeners)
- Listener lifecycle support via `lifecycle.enabled` and `lifecycle.types`
- Sandbox test delivery endpoint (`POST /listeners/{id}/sandbox:send`)
- Webhook signature verification (`X-Notamify-Signature`)
- Typed webhook event models for `interpretation` and `lifecycle` payloads
- Embedded receiver + cloudflared helper for local webhook testing
- Strict receiver mode fails fast when webhook secret is missing

## Install

```bash
pip install notamify-sdk
```

Or with `uv`:

```bash
uv add notamify-sdk
```

For contributors working from a source checkout:

```bash
uv sync
```

## Configuration

Environment variables (highest priority):

- `NOTAMIFY_TOKEN`
- `NOTAMIFY_API_BASE_URL`
- `NOTAMIFY_WATCHER_BASE_URL`
- `NOTAMIFY_WEBHOOK_SECRET`
- `NOTAMIFY_CONFIG_FILE`

Default config file: `~/.config/notamify/config.json`

## Usage

```python
from notamify_sdk import NotamifyClient

client = NotamifyClient(token="YOUR_TOKEN")

# The API accepts at most 30 items per page.
active_notams = list(client.notams.active({
    "location": ["KJFK", "KLAX"],
    "per_page": 30,
}))
print(len(active_notams))

first_page = next(iter(client.notams.active({
    "location": ["KJFK", "KLAX"],
    "per_page": 30,
}).pages))
print(first_page.total_count)

job = client.create_async_briefing({
    "locations": [{
        "location": "KJFK",
        "type": "origin",
        "starts_at": "2026-02-25T10:00:00Z",
        "ends_at": "2026-02-25T12:00:00Z",
    }],
})
print(job.uuid)

# Watcher sandbox flow
listener = client.create_listener(
    "https://example.trycloudflare.com/webhooks/notamify",
    mode="sandbox",
    lifecycle={"enabled": False},
)
print(listener.webhook_secret)
sandbox_result = client.send_sandbox_message(listener.id, "SANDBOX-NOTAM-1")
print(sandbox_result.notam_id)
```

## Pagination

The SDK exposes two NOTAM access styles:

- `client.get_active_notams(...)`, `client.get_raw_notams(...)`, `client.get_nearby_notams(...)`, and `client.get_historical_notams(...)` return a single `NotamListResult` page.
- `client.notams.active(...)`, `client.notams.raw(...)`, `client.notams.nearby(...)`, and `client.notams.historical(...)` return a pager that fetches all pages lazily as you iterate.

Use the pager when you want all NOTAMs across pages:

```python
pager = client.notams.active(
    {"location": ["KJFK", "KLAX"]},
    per_page=30,
)

for notam in pager:
    print(notam.id)
```

If you want everything in memory at once, materialize the pager with `list(...)`:

```python
all_notams = list(
    client.notams.active(
        {"location": ["KJFK", "KLAX"]},
        per_page=30,
    )
)
```

If you need page metadata such as `page`, `per_page`, or `total_count`, iterate over `pager.pages`:

```python
pager = client.notams.active(
    {"location": ["KJFK", "KLAX"]},
    per_page=30,
)

for page in pager.pages:
    print(page.page, page.total_count, len(page.notams))
```

The API allows at most `30` items per page. The SDK validates this with Pydantic and rejects larger `per_page` values.

## Local Webhook Testing

The example scripts live in the repository and source distribution under `examples/`.
They are intended to be run from a source checkout or source distribution, not from an installed wheel alone.

- Production: implement the webhook endpoint in your own application code.
- Verify `X-Notamify-Signature` using `NOTAMIFY_WEBHOOK_SECRET`.
- Production watcher deliveries include `kind`, `event_id`, and `notam`.
- Lifecycle watcher deliveries also include `change.changed_notam_id` for the original NOTAM that was cancelled or replaced.
- Local development: expose your local app endpoint with `cloudflared`, then set watcher `webhook_url` to that tunnel URL.
- Sandbox test sends use the same webhook payload DTO as production sends, with mock NOTAM content.

Prerequisite: `cloudflared` must be installed and available in `PATH`.

```bash
export NOTAMIFY_TOKEN="your_notamify_token"
export LOCAL_APP_URL="http://127.0.0.1:8080/webhooks/notamify"
uv run python ./examples/local_service_run.py
```

## Examples

- `examples/service_receiver.py`: minimal production-style webhook receiver
- `examples/local_service_run.py`: cloudflared tunnel + sandbox delivery flow
- `examples/notams_fetch.py`: straightforward NOTAM query examples
- `examples/README.md`: setup notes and sample payloads

## Development

```bash
uv run python -m unittest discover -s tests -p 'test_*.py'
```

## License

MIT
