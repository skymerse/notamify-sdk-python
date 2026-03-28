#!/usr/bin/env python3

from __future__ import annotations

import os
from datetime import date, timedelta

from notamify_sdk import APIError, NotamifyClient


def main() -> int:
    token = os.getenv("NOTAMIFY_TOKEN", "").strip()
    if not token:
        raise SystemExit("NOTAMIFY_TOKEN is required")

    client = NotamifyClient(token=token)
    sample_size = 5
    icaos = ["KJFK", "KLAX", "EGLL", "EPWA"]

    active = list(
        client.notams.active(
            {
                "location": icaos,
                "per_page": 30,
            }
        )
    )
    print("Active NOTAMs")
    print(f"locations: {', '.join(icaos)}")
    print(f"returned: {len(active)} across all pages")
    for notam in active[:sample_size]:
        print(f"- {notam.id} | {notam.location} | {notam.starts_at.isoformat()}")

    raw = list(
        client.notams.raw(
            {
                "location": ["KJFK", "KLAX"],
                "per_page": 30,
            }
        )
    )
    print("\nRaw NOTAMs")
    print("locations: KJFK, KLAX")
    print(f"returned: {len(raw)} across all pages")
    for notam in raw[:sample_size]:
        print(f"- {notam.id} | {notam.location} | {notam.notam_number}")

    nearby = list(
        client.notams.nearby(
            {
                "lat": 40.6413,
                "lon": -73.7781,
                "radius_nm": 50,
                "per_page": 30,
            }
        )
    )
    print("\nNearby NOTAMs")
    print("center: KJFK (40.6413, -73.7781), radius: 50nm")
    print(f"returned: {len(nearby)} across all pages")
    for notam in nearby[:sample_size]:
        print(f"- {notam.id} | {notam.location} | {notam.starts_at.isoformat()}")

    historical_date = date.today() - timedelta(days=7)
    historical = list(
        client.notams.historical(
            {
                "valid_at": historical_date,
                "location": ["KJFK", "KLAX"],
                "per_page": 30,
            }
        )
    )
    print("\nHistorical NOTAMs")
    print(f"valid_at: {historical_date.isoformat()} | locations: KJFK, KLAX")
    print(f"returned: {len(historical)} across all pages")
    for notam in historical[:sample_size]:
        print(f"- {notam.id} | {notam.location} | {notam.starts_at.isoformat()}")

    first_active_page = next(
        iter(
            client.notams.active(
                {
                    "location": icaos,
                    "per_page": 30,
                }
            ).pages
        ),
        None,
    )
    if first_active_page is not None:
        print("\nFirst active page metadata")
        print(f"page: {first_active_page.page}")
        print(f"per_page: {first_active_page.per_page}")
        print(f"total_count: {first_active_page.total_count}")

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except APIError as exc:
        print(f"API error ({exc.status}): {exc.message}")
        raise SystemExit(1)
