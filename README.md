# notamify-sdk (Python)

Python SDK and command line for Notamify public APIs:

- Notamify API v2 (`https://api.notamify.com/api/v2`)
- Watcher API (`https://watcher.notamify.com`)

Which Notamify surface to use:

| You are | Use |
|---|---|
| Writing Python that calls Notamify | this SDK: `pip install notamify-sdk` |
| At a terminal, in scripts or CI, or a coding agent with a shell (Claude Code, Codex) | the `notamify` CLI: `uv tool install notamify` |
| A chat app without a shell (ChatGPT, Claude web and desktop) | the hosted MCP server: `https://mcp.notamify.com/mcp` |

## Command line

```bash
uv tool install notamify      # or: pipx install notamify
notamify --version
```

`uv tool install` and `pipx install` put the `notamify` command in `~/.local/bin`. If your shell then reports `command not found: notamify`, add that directory to your `PATH` with `uv tool update-shell` (or `pipx ensurepath`) and open a new terminal. Without installing anything, `uvx notamify ...` runs the latest release.

```bash
notamify auth login                                    # API key from https://notamify.com/api-manager
notamify notams active EPWA EGLL --start now --hours 24
notamify affected EPWA --affected CLOSED:RUNWAY
notamify briefing flight create --origin EPWA@now/+2h --destination EGLL@+2h/+5h --aircraft B738 --wait
notamify watcher listeners list
notamify webhook listen --tunnel
notamify commands                                      # every command, credit cost and exit code
notamify skill install                                 # agent skill for Claude Code (--dir for others)
```

Output is text on a terminal and JSON when piped. Errors are written to stderr, with stable exit codes. Commands that may spend more than one credit, or that change Watcher listeners, need `--yes`; `--dry-run` previews a request. See [cli/README.md](cli/README.md).

The CLI code lives in this package (`notamify_sdk.cli`, also runnable as `python -m notamify_sdk.cli`); the `notamify` distribution in `cli/` only installs the command.

## Features

- Typed API client with one auth token
- Pydantic response/request models
- Pager-style NOTAM listing via `client.notams.*` with item iteration and `pager.pages`
- Supported NOTAM endpoints:
  - `GET /notams`
  - `GET /notams/raw`
  - `GET /notams/nearby`
  - `GET /notams/archive`
  - `GET /notams/recently-expired`
  - `GET /notams/{id}`
  - `GET /notams/briefing/simple`
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

`NotamifyClient()` without arguments reads the token and base URLs from these sources.

## Quick start

### Get active NOTAMs
```python
from datetime import datetime, timedelta, timezone
from notamify_sdk import NotamifyClient, ActiveNotamsQuery

client = NotamifyClient()  # or NotamifyClient(token="YOUR_API_KEY")

query = ActiveNotamsQuery(
    location=["KJFK", "KLAX"],
    starts_at=datetime.now(timezone.utc),
    ends_at=datetime.now(timezone.utc) + timedelta(days=1),
)
for notam in client.notams.active(query, per_page=30):
    print(notam.notam_number, notam.interpretation.excerpt if notam.interpretation else notam.message)
```

### One NOTAM, recently expired NOTAMs, airport briefing
```python
notam = client.get_notam("NOTAM_ID")
expired = client.get_recently_expired_notams({"location": ["EPWA"]})
briefing = client.generate_airport_briefing({"location": "EPWA"})
print(briefing.summary)
```

### Flight briefing
A failed job is returned with `status == "failed"`, so a polling loop can stop on it.
```python
import time

job = client.create_briefing({
    "locations": [
        {"location": "EPWA", "type": "origin", "starts_at": "2026-11-09T08:00:00Z", "ends_at": "2026-11-09T10:00:00Z"},
        {"location": "EGLL", "type": "destination", "starts_at": "2026-11-09T10:30:00Z", "ends_at": "2026-11-09T13:00:00Z"},
    ],
    "aircraft_type": "B738",
})
while (status := client.get_briefing_status(job.uuid)).status.value in ("pending", "processing"):
    time.sleep(3)
print(status.status.value, status.response.briefing.text if status.response else "")
```

### Watcher flow
```python
# Watcher sandbox flow
listener = client.create_listener(
    "https://example.trycloudflare.com/webhooks/notamify",
    mode="sandbox",
    lifecycle={"enabled": False},
)
print(listener.webhook_secret)
sandbox_result = client.send_sandbox_message(listener.id, "SANDBOX-NOTAM-1")
print(sandbox_result.notam_id)

# Change some fields and keep the rest. update_listener replaces the whole listener.
client.patch_listener(listener.id, filters={"notam_icao": ["EPWA"]})
```

The `notamify_watcher_sdk` package is deprecated and will be removed in 1.0; import from `notamify_sdk`.

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
make test                        # unit tests, CLI tests and the API spec coverage check
make spec                        # refresh tests/fixtures/openapi.json from the live API
uv tool install --editable ./cli # put this checkout's `notamify` command on PATH
```

`tests/test_spec_coverage.py` checks that the SDK covers every operation, parameter and field in the public OpenAPI document. The public document intentionally omits some detail, so the SDK may model more than it shows; the check only fails when the SDK is missing something the document has.

### Releasing

`notamify-sdk` and `notamify` are released together from one `vX.Y.Z` tag. Bump `version` in `pyproject.toml` and `cli/pyproject.toml`, and the `notamify-sdk==` pin in `cli/pyproject.toml`; the release workflow checks that all three match the tag.

## License

MIT
