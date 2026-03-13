# Notamify Examples (Python)

These examples cover watcher webhook integration and NOTAM data fetching.
Run them from a source checkout or source distribution after installing dependencies with `uv sync`.

## 1) Copy-paste receiver into your service

Use this file as your base:

- `examples/service_receiver.py`

Run it locally:

```bash
export NOTAMIFY_WEBHOOK_SECRET="your_webhook_secret"
export NOTAMIFY_WEBHOOK_PATH="/webhooks/notamify"
uv run python ./examples/service_receiver.py
```

Replace `handle_notamify_event(event)` with your business logic.

## 2) End-to-end local test against your service

This script:

- starts cloudflared tunnel to your local app origin
- derives watcher `webhook_url` from tunnel host + your local path
- creates/updates one listener in test mode (`mode=sandbox`)
- calls `POST /listeners/{id}/sandbox:send` to trigger a webhook
- retries transient DNS propagation errors

Prerequisite: `cloudflared` must be installed and available in `PATH`.

```bash
export NOTAMIFY_TOKEN="your_notamify_token"
export LOCAL_APP_URL="http://127.0.0.1:8080/webhooks/notamify"
uv run python ./examples/local_service_run.py
```

Optional:

- `NOTAMIFY_LISTENER_ID`
- `NOTAMIFY_LISTENER_NAME` (default `notamify-sdk-sandbox-example`)

Create a token in the [Notamify API Manager](https://notamify.com/api-manager).

## 3) Fetch NOTAMs across all pages

This script paginates until all NOTAM pages are fetched for one endpoint.

- file: `examples/notams_fetch.py`
- supported endpoints via `NOTAM_ENDPOINT`: `active`, `raw`, `nearby`, `historical`

```bash
export NOTAMIFY_TOKEN="your_notamify_token"
export NOTAM_ENDPOINT="active"
export NOTAM_LOCATION="KJFK,KLAX"
uv run python ./examples/notams_fetch.py
```

Optional variables:

- `NOTAM_PER_PAGE` (default `100`)
- `NOTAM_MAX_PAGES` (default `200`)
- `NOTAM_STARTS_AT`, `NOTAM_ENDS_AT` (ISO datetime)
- `NOTAM_LAT`, `NOTAM_LON`, `NOTAM_RADIUS_NM` (for `nearby`)
- `NOTAM_VALID_AT` (YYYY-MM-DD, for `historical`)

## Webhook payload DTO

Sandbox test sends and production sends use the same webhook payload DTO. Watcher can send standard `interpretation` messages and optional `lifecycle` messages for later NOTAMC/NOTAMR changes when the listener has `lifecycle_enabled = true`.

```json
{
  "listener_id": "listener-123",
  "kind": "interpretation",
  "event_id": "event-123",
  "notam": {
    "id": "A1234/26",
    "notam_number": "A1234/26",
    "notam_type": "N",
    "location": "EPWA",
    "icao_code": "EPWA",
    "qcode": "QMRLC",
    "classification": "INTL",
    "starts_at": "2026-02-25T12:00:00Z",
    "ends_at": "2026-02-25T13:00:00Z",
    "issued_at": "2026-02-25T11:55:00Z",
    "is_estimated": false,
    "is_permanent": false,
    "message": "NOTAM text",
    "icao_message": "NOTAM text",
    "interpretation": {
      "description": "Human summary",
      "excerpt": "Short summary",
      "category": "AERODROME",
      "subcategory": "RUNWAY_OPERATIONS",
      "map_elements": [],
      "affected_elements": [],
      "schedules": []
    }
  },
  "context": {
    "location": {
      "ident": "EPWA",
      "icao": "EPWA",
      "name": "Warsaw Chopin Airport",
      "iso_country": "PL",
      "iso_country_name": "Poland",
      "coordinates": {
        "lat": 52.1657,
        "lon": 20.9671
      },
      "fir_icaos": ["EPWW"]
    }
  },
  "sent_at": "2026-02-25T13:09:51Z"
}
```

Lifecycle payloads use the same top-level structure, but set `kind` to `"lifecycle"` and add:

```json
{
  "change": {
    "changed_notam_id": "old-notam-uuid",
    "notam_type": "R"
  }
}
```
