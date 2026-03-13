#!/usr/bin/env python3

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Callable

from notamify_sdk import APIError, NotamListResult, NotamifyClient

DEFAULT_PER_PAGE = 100
DEFAULT_MAX_PAGES = 200
DEFAULT_LAT = 40.6413
DEFAULT_LON = -73.7781


def require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise SystemExit(f"{name} is required")
    return value


def parse_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def parse_positive_int(value: str | None, fallback: int) -> int:
    raw = (value or "").strip()
    try:
        parsed = int(raw)
    except ValueError:
        return fallback
    if parsed > 0:
        return parsed
    return fallback


def parse_float(value: str | None, fallback: float) -> float:
    raw = (value or "").strip()
    try:
        return float(raw)
    except ValueError:
        return fallback


def today_utc_date_str() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def endpoint_config(
    client: NotamifyClient,
    endpoint: str,
    per_page: int,
) -> tuple[str, Callable[[int], NotamListResult]]:
    locations = parse_csv(os.getenv("NOTAM_LOCATION", os.getenv("NOTAM_LOCATIONS", "")))
    starts_at = os.getenv("NOTAM_STARTS_AT", "").strip()
    ends_at = os.getenv("NOTAM_ENDS_AT", "").strip()

    if endpoint == "active":

        def fetch_page(page: int) -> NotamListResult:
            query: dict[str, Any] = {
                "page": page,
                "per_page": per_page,
            }
            if locations:
                query["location"] = locations
            if starts_at:
                query["starts_at"] = starts_at
            if ends_at:
                query["ends_at"] = ends_at
            return client.get_active_notams(query)

        return "active", fetch_page

    if endpoint == "raw":

        def fetch_page(page: int) -> NotamListResult:
            query: dict[str, Any] = {
                "page": page,
                "per_page": per_page,
            }
            if locations:
                query["location"] = locations
            if starts_at:
                query["starts_at"] = starts_at
            if ends_at:
                query["ends_at"] = ends_at
            return client.get_raw_notams(query)

        return "raw", fetch_page

    if endpoint == "nearby":
        lat = parse_float(os.getenv("NOTAM_LAT"), DEFAULT_LAT)
        lon = parse_float(os.getenv("NOTAM_LON"), DEFAULT_LON)
        radius_nm = parse_float(os.getenv("NOTAM_RADIUS_NM"), 50.0)

        def fetch_page(page: int) -> NotamListResult:
            query: dict[str, Any] = {
                "lat": lat,
                "lon": lon,
                "radius_nm": radius_nm,
                "page": page,
                "per_page": per_page,
            }
            if starts_at:
                query["starts_at"] = starts_at
            if ends_at:
                query["ends_at"] = ends_at
            return client.get_nearby_notams(query)

        return "nearby", fetch_page

    if endpoint == "historical":
        valid_at = os.getenv("NOTAM_VALID_AT", today_utc_date_str()).strip()

        def fetch_page(page: int) -> NotamListResult:
            query: dict[str, Any] = {
                "valid_at": valid_at,
                "page": page,
                "per_page": per_page,
            }
            if locations:
                query["location"] = locations
            return client.get_historical_notams(query)

        return "historical", fetch_page

    raise SystemExit("Unsupported NOTAM_ENDPOINT. Use active, raw, nearby, or historical.")


def fetch_all_pages(
    fetch_page: Callable[[int], NotamListResult],
    max_pages: int,
) -> tuple[list[Any], int, int | None]:
    all_notams: list[Any] = []
    pages_fetched = 0
    expected_total: int | None = None

    for page in range(1, max_pages + 1):
        result = fetch_page(page)
        page_items = list(result.notams)

        if expected_total is None:
            expected_total = result.total_count

        all_notams.extend(page_items)
        pages_fetched = page

        resolved_per_page = result.per_page if result.per_page > 0 else len(page_items)
        total_pages = (
            (expected_total + resolved_per_page - 1) // resolved_per_page
            if expected_total is not None and resolved_per_page > 0
            else None
        )

        print(f"[page {page}] fetched={len(page_items)} total_count={result.total_count}")

        if len(page_items) == 0:
            break
        if total_pages is not None and page >= total_pages:
            break
        if resolved_per_page > 0 and len(page_items) < resolved_per_page:
            break

    return all_notams, pages_fetched, expected_total


def print_summary(endpoint: str, notams: list[Any], pages_fetched: int, expected_total: int | None) -> None:
    print("\nFetch summary")
    print(f"endpoint: {endpoint}")
    print(f"pages fetched: {pages_fetched}")
    print(f"notams collected: {len(notams)}")
    if expected_total is not None:
        print(f"expected total_count: {expected_total}")

    print("\nFirst 5 NOTAM IDs:")
    sample = notams[:5]
    if not sample:
        print("(none)")
        return
    for notam in sample:
        print(f"- {notam.id} | location={notam.location} | starts_at={notam.starts_at}")


def main() -> int:
    token = require_env("NOTAMIFY_TOKEN")
    endpoint = os.getenv("NOTAM_ENDPOINT", "active").strip().lower()
    per_page = parse_positive_int(os.getenv("NOTAM_PER_PAGE"), DEFAULT_PER_PAGE)
    max_pages = parse_positive_int(os.getenv("NOTAM_MAX_PAGES"), DEFAULT_MAX_PAGES)

    client = NotamifyClient(token=token)
    endpoint_label, fetch_page = endpoint_config(client, endpoint, per_page)
    notams, pages_fetched, expected_total = fetch_all_pages(fetch_page, max_pages=max_pages)
    print_summary(endpoint_label, notams, pages_fetched, expected_total)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except APIError as exc:
        print(f"API error ({exc.status}): {exc.message}")
        raise SystemExit(1)
