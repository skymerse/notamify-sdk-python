# notamify-sdk (Python)

Python SDK for Notamify public APIs:

- Notamify API v2 (`https://api.notamify.com/api/v2`)
- Watcher API (`https://watcher.notamify.com`)

## Features

- Typed API client with one auth token
- Pydantic response/request models
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
- Listener `lifecycle_enabled` support for NOTAMC/NOTAMR follow-up events
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

active = client.get_active_notams({
    "location": ["KJFK", "KLAX"],
    "page": 1,
    "per_page": 30,
})
print(active.total_count)

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
    lifecycle_enabled=False,
)
print(listener.webhook_secret)
sandbox_result = client.send_sandbox_message(listener.id, "SANDBOX-NOTAM-1")
print(sandbox_result.notam_id)
```

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
- `examples/notams_fetch.py`: paginated NOTAM fetch example
- `examples/README.md`: setup notes and sample payloads

## Development

```bash
uv run python -m unittest discover -s tests -p 'test_*.py'
```

## License

MIT
