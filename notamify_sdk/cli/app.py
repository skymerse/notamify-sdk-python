"""The ``notamify`` command line interface.

Designed for people and agents alike: text on a terminal, JSON when piped,
stable exit codes, a structured error envelope on stderr, and an explicit
confirmation protocol for anything that spends more than one credit or
changes Watcher state.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import queue
import re
import shlex
import sys
import time
import uuid as uuidlib
from datetime import datetime, timezone
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from pydantic import ValidationError

from ..account import SCOPES, AccountAuth, AccountError, AccountStore, McpClient
from ..client import SDK_VERSION, APIError, NotamifyClient
from ..cloudflared import CloudflaredError, CloudflaredManager
from ..config import ConfigStore, SDKConfig
from ..receiver import ReceivedEvent, ReceiverConfig, WebhookReceiver
from .backends import (
    ACCOUNT_TOOLS,
    AccountBackend,
    ApiKeyBackend,
    Backend,
    credits_charged,
    error_from_account,
    requires_api_key,
    strip_extras,
)
from .output import (
    API_KEYS_URL,
    DISCLAIMER,
    EXIT_AUTH,
    EXIT_CODES,
    EXIT_CONFIRMATION_REQUIRED,
    EXIT_ERROR,
    EXIT_INTERRUPTED,
    EXIT_OK,
    EXIT_TEMPORARY,
    EXIT_USAGE,
    CLIError,
    Output,
    detect_mode,
    error_from_api,
    format_iso,
    format_short,
    interactive,
    mask_secret,
    now_utc,
    parse_time,
    pick_fields,
    to_jsonable,
)

PROG = "notamify"
DEFAULT_PER_PAGE = 30
DEFAULT_MAX_PAGES = 20
CLASSIFICATIONS = ["DOM", "FDC", "INTL", "MIL"]
CATEGORIES = [
    "AERODROME",
    "AIRSPACE",
    "NAVIGATION",
    "COMMUNICATION",
    "OPERATIONS",
    "OBSTACLES",
    "ADMINISTRATIVE",
    "WEATHER",
    "SAFETY",
    "OTHER",
    "ALL",
]


# Parser ------------------------------------------------------------------


class UsageError(Exception):
    def __init__(self, message: str, prog: str) -> None:
        super().__init__(message)
        self.prog = prog


class Parser(argparse.ArgumentParser):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("allow_abbrev", False)
        kwargs.setdefault("formatter_class", argparse.RawDescriptionHelpFormatter)
        super().__init__(*args, **kwargs)

    def error(self, message: str) -> None:  # type: ignore[override]
        raise UsageError(message, self.prog)


@dataclass
class CommandMeta:
    summary: str
    credits: str | None = None
    confirmation: str | None = None
    network: bool = True
    examples: list[str] = field(default_factory=list)


def _output_options() -> argparse.ArgumentParser:
    parent = Parser(add_help=False)
    group = parent.add_argument_group("output")
    group.add_argument(
        "--json",
        dest="output",
        action="store_const",
        const="json",
        default=argparse.SUPPRESS,
        help="JSON output (the default when stdout is not a terminal)",
    )
    group.add_argument(
        "--output",
        choices=["text", "json"],
        default=argparse.SUPPRESS,
        help="output format; also NOTAMIFY_OUTPUT",
    )
    return parent


def _action_options() -> argparse.ArgumentParser:
    parent = Parser(add_help=False)
    group = parent.add_argument_group("execution")
    group.add_argument(
        "--yes",
        "-y",
        action="store_true",
        default=argparse.SUPPRESS,
        help="confirm commands that spend more than one credit or change Watcher state",
    )
    group.add_argument(
        "--dry-run",
        action="store_true",
        default=argparse.SUPPRESS,
        help="print the request and estimated credits without sending it",
    )
    group.add_argument(
        "--timeout",
        type=float,
        default=argparse.SUPPRESS,
        metavar="SECONDS",
        help="HTTP timeout for reads (default 10)",
    )
    group.add_argument(
        "--auth",
        choices=["auto", "account", "api-key"],
        default=argparse.SUPPRESS,
        help="sign-in to use; auto prefers NOTAMIFY_TOKEN, then your account, then a stored key (also NOTAMIFY_AUTH)",
    )
    return parent


OUTPUT_OPTIONS = _output_options()
ACTION_OPTIONS = _action_options()


def _add_command(
    subparsers: Any,
    name: str,
    handler: Callable[["Context"], Any],
    meta: CommandMeta,
    *,
    description: str | None = None,
) -> argparse.ArgumentParser:
    parents = [OUTPUT_OPTIONS, ACTION_OPTIONS] if meta.network else [OUTPUT_OPTIONS]
    epilog_lines = []
    if meta.credits:
        epilog_lines.append(f"credits: {meta.credits}")
    if meta.confirmation:
        epilog_lines.append(f"confirmation: {meta.confirmation}")
    if meta.examples:
        epilog_lines.append("examples:")
        epilog_lines.extend(f"  {example}" for example in meta.examples)
    parser = subparsers.add_parser(
        name,
        help=meta.summary,
        description=description or meta.summary,
        epilog="\n".join(epilog_lines) or None,
        parents=parents,
    )
    parser.set_defaults(handler=handler, meta=meta)
    return parser


def _add_group(subparsers: Any, name: str, summary: str) -> Any:
    parser = subparsers.add_parser(name, help=summary, description=summary, parents=[OUTPUT_OPTIONS])
    return parser.add_subparsers(dest=f"{name}_command", metavar="COMMAND", required=True, parser_class=Parser)


def _add_time_window(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("time window")
    group.add_argument("--start", metavar="TIME", help="window start: ISO 8601, YYYY-MM-DD, now, or +6h/-30m (UTC)")
    group.add_argument("--end", metavar="TIME", help="window end, same formats; relative values count from --start")
    group.add_argument("--hours", type=float, metavar="N", help="window length in hours from --start (instead of --end)")


def _add_notam_filters(
    parser: argparse.ArgumentParser, *, qcode: bool = True, classification: bool = True, interpretation: bool = True
) -> None:
    group = parser.add_argument_group("filters")
    group.add_argument("--id", dest="notam_ids", action="append", metavar="NOTAM_ID", help="limit to Notamify NOTAM IDs")
    if interpretation:
        _add_interpretation_filters(group)
    if qcode:
        group.add_argument("--qcode", action="append", type=str.upper, metavar="QCODE", help="Q-code, e.g. QMRLC")
    if classification:
        group.add_argument(
            "--exclude-classification",
            action="append",
            type=str.upper,
            choices=CLASSIFICATIONS,
            metavar="CLASS",
            help="exclude DOM, FDC, INTL or MIL",
        )
    group.add_argument(
        "--no-est",
        action="store_true",
        help="leave out estimated-end (EST) NOTAMs whose estimated end has passed",
    )


def _add_interpretation_filters(group: Any) -> None:
    group.add_argument(
        "--category",
        action="append",
        type=str.upper,
        metavar="CATEGORY",
        help=f"interpretation category: {', '.join(CATEGORIES)}",
    )
    group.add_argument("--subcategory", action="append", metavar="SUBCATEGORY", help="interpretation subcategory")
    group.add_argument(
        "--affected",
        action="append",
        metavar="EFFECT[:TYPE]",
        help="affected element filter, e.g. CLOSED:RUNWAY, CLOSED, :TAXIWAY (applied after paging)",
    )


def _add_paging(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("pages (1 credit per page fetched)")
    group.add_argument("--page", type=int, default=1, metavar="N", help="first page to fetch (default 1)")
    group.add_argument(
        "--per-page", type=int, default=DEFAULT_PER_PAGE, metavar="N", help="NOTAMs per page, 1-30 (default 30)"
    )
    group.add_argument("--all", action="store_true", help="fetch every page, up to --max-pages (needs --yes)")
    group.add_argument(
        "--max-pages",
        type=int,
        default=DEFAULT_MAX_PAGES,
        metavar="N",
        help=f"page limit for --all (default {DEFAULT_MAX_PAGES})",
    )
    group.add_argument(
        "--fields",
        metavar="LIST",
        help="comma-separated NOTAM fields to keep, dotted for nested: id,notam_number,interpretation.excerpt",
    )


def _add_flight(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("flight")
    location_help = (
        "ICAO@START/END, e.g. EPWA@2026-11-09T08:00Z/2026-11-09T10:00Z or EPWA@now/+2h "
        "(a relative START counts from now, a relative END from START)"
    )
    group.add_argument("--origin", metavar="LOCATION", help=f"departure airport: {location_help}")
    group.add_argument("--destination", metavar="LOCATION", help="arrival airport, same format")
    group.add_argument("--alternate", action="append", metavar="LOCATION", help="alternate airport, repeatable")
    group.add_argument("--fir", action="append", metavar="LOCATION", help="FIR to include, repeatable")
    group.add_argument("--aircraft", metavar="TYPE", help="ICAO aircraft type, e.g. B738")
    group.add_argument("--origin-runway", metavar="RWY", help="departure runway, e.g. RWY11")
    group.add_argument("--destination-runway", metavar="RWY", help="arrival runway, e.g. RWY27L")
    group.add_argument("--destination-procedure", metavar="PROC", help="arrival procedure, e.g. ILS27L")
    group.add_argument(
        "--body",
        metavar="FILE",
        help="full JSON request body from a file, or - for stdin; replaces the flight flags",
    )


def _add_listener_fields(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("listener")
    group.add_argument("--name", help="listener name")
    group.add_argument("--webhook-url", metavar="URL", help="HTTPS endpoint that receives deliveries")
    group.add_argument("--email", dest="emails", action="append", metavar="ADDRESS", help="email recipient, repeatable")
    group.add_argument("--mode", choices=["prod", "sandbox"], help="prod delivers real NOTAMs; sandbox only test sends")
    state = group.add_mutually_exclusive_group()
    state.add_argument("--active", dest="active", action="store_const", const=True, help="deliver NOTAMs")
    state.add_argument("--inactive", dest="active", action="store_const", const=False, help="pause deliveries")
    lifecycle = group.add_mutually_exclusive_group()
    lifecycle.add_argument("--lifecycle", dest="lifecycle_enabled", action="store_const", const=True, help="also deliver cancellations and replacements")
    lifecycle.add_argument("--no-lifecycle", dest="lifecycle_enabled", action="store_const", const=False, help="deliver new NOTAMs only")
    group.add_argument(
        "--lifecycle-type",
        action="append",
        type=str.upper,
        choices=["CANCELLED", "REPLACED"],
        metavar="TYPE",
        help="lifecycle events to deliver: CANCELLED, REPLACED",
    )
    filters = parser.add_argument_group("listener filters (on update, each given filter replaces only that filter)")
    filters.add_argument("--icao", action="append", type=str.upper, metavar="ICAO", help="location, repeatable")
    filters.add_argument("--category", action="append", type=str.upper, metavar="CATEGORY", help="interpretation category")
    filters.add_argument("--subcategory", action="append", metavar="SUBCATEGORY", help="interpretation subcategory")
    filters.add_argument("--qcode", action="append", type=str.upper, metavar="QCODE", help="Q-code")
    filters.add_argument("--notam-type", action="append", type=str.upper, metavar="TYPE", help="NOTAM type: N, R, C")
    filters.add_argument("--airport-type", action="append", metavar="TYPE", help="airport type, e.g. large_airport")
    filters.add_argument("--affected", action="append", metavar="EFFECT[:TYPE]", help="affected element filter")
    filters.add_argument(
        "--filters-json",
        metavar="JSON",
        help='filters as a JSON object, e.g. \'{"time_windows":[...]}\'; on update its keys replace those filters and null removes one',
    )


def build_parser() -> Parser:
    parser = Parser(
        prog=PROG,
        description=(
            "Notamify on the command line: NOTAMs, airport and flight briefings, and NOTAM Watcher.\n\n"
            "Text on a terminal, JSON when piped. Errors go to stderr; in JSON mode as {\"error\": {...}}.\n"
            "Authenticate with NOTAMIFY_TOKEN or `notamify auth login`. Run `notamify commands` for a\n"
            "machine-readable list of every command, option and exit code."
        ),
        parents=[OUTPUT_OPTIONS],
    )
    parser.add_argument("--version", action="version", version=f"{PROG} {SDK_VERSION}")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND", required=True, parser_class=Parser)

    # auth
    auth = _add_group(sub, "auth", "Sign in with your Notamify account or an API key")
    p = _add_command(
        auth,
        "login",
        cmd_auth_login,
        CommandMeta(
            "Sign in with your Notamify account in the browser (default) or save an API key",
            network=False,
            examples=[
                f"{PROG} auth login",
                f"{PROG} auth login --no-browser --port 8765   # then: ssh -L 8765:127.0.0.1:8765 this-host",
                f"{PROG} auth login --api-key",
                f'printf %s "$KEY" | {PROG} auth login --token-stdin',
            ],
        ),
        description=(
            "Sign in with your Notamify account, like the Notamify plugins and connectors: your browser opens\n"
            "notamify.com to approve the CLI, and requests are billed to your account at the connected-app rate.\n"
            "No API key needed. Account sign-in covers NOTAMs and briefings; Watcher commands and `notams raw`\n"
            f"need an API key (--api-key). Create keys at {API_KEYS_URL}."
        ),
    )
    p.add_argument("--api-key", action="store_true", help="save an API key instead (prompts for it)")
    p.add_argument("--token-stdin", action="store_true", help="save an API key read from stdin")
    p.add_argument("--no-browser", action="store_true", help="print the sign-in URL instead of opening a browser")
    p.add_argument("--port", type=int, default=0, help="local port for the sign-in callback (default: any free port)")
    p.add_argument(
        "--login-timeout", type=float, default=300, metavar="SECONDS", help="how long to wait for the browser (default 300)"
    )
    _add_command(
        auth,
        "status",
        cmd_auth_status,
        CommandMeta("Show the account session and API key in use; exits 3 when neither is set", network=False),
    )
    p = _add_command(
        auth,
        "logout",
        cmd_auth_logout,
        CommandMeta("Sign out of the account and remove the stored API key", network=False),
    )
    which = p.add_mutually_exclusive_group()
    which.add_argument("--account", action="store_true", help="only sign out of the account")
    which.add_argument("--api-key", action="store_true", help="only remove the stored API key")

    # notams
    notams = _add_group(sub, "notams", "Search and read NOTAMs")
    for name, handler, summary in (
        ("active", cmd_notams_active, "Active and upcoming NOTAMs with interpretations for up to 5 locations"),
        ("raw", cmd_notams_raw, "Active NOTAMs without interpretations"),
    ):
        p = _add_command(
            notams,
            name,
            handler,
            CommandMeta(
                summary,
                credits="1 per page",
                confirmation="--all",
                examples=[
                    f"{PROG} notams {name} EPWA",
                    f"{PROG} notams {name} KJFK KLAX --start now --hours 24 --affected CLOSED:RUNWAY",
                    f"{PROG} notams {name} EGLL --all --yes --json --fields id,notam_number,interpretation.excerpt",
                ],
            ),
        )
        p.add_argument("locations", nargs="*", metavar="LOCATION", help="ICAO or 3-letter domestic codes, up to 5")
        _add_time_window(p)
        _add_notam_filters(p, interpretation=name != "raw")
        _add_paging(p)

    p = _add_command(
        notams,
        "nearby",
        cmd_notams_nearby,
        CommandMeta(
            "NOTAMs within a radius of a point",
            credits="1 per page",
            confirmation="--all",
            examples=[f"{PROG} notams nearby --lat 52.1657 --lon 20.9671 --radius 10"],
        ),
    )
    p.add_argument("--lat", type=float, required=True, help="latitude, decimal degrees")
    p.add_argument("--lon", type=float, required=True, help="longitude, decimal degrees")
    p.add_argument("--radius", type=float, metavar="NM", help="radius in nautical miles, 0.1-25 (API default 1)")
    _add_time_window(p)
    _add_notam_filters(p)
    _add_paging(p)

    p = _add_command(
        notams,
        "archive",
        cmd_notams_archive,
        CommandMeta(
            "NOTAMs that were valid on a past date",
            credits="1 per page",
            confirmation="--all",
            examples=[f"{PROG} notams archive EDDF --date 2025-09-20"],
        ),
    )
    p.add_argument("locations", nargs="*", metavar="LOCATION", help="ICAO or domestic codes, up to 5")
    p.add_argument("--date", required=True, metavar="YYYY-MM-DD", help="date the NOTAMs were valid on")
    _add_notam_filters(p, qcode=False, classification=False)
    _add_paging(p)

    p = _add_command(
        notams,
        "expired",
        cmd_notams_expired,
        CommandMeta(
            "NOTAMs that became inactive recently (default: last 24 hours, window up to 7 days)",
            credits="1 per page",
            confirmation="--all",
            examples=[f"{PROG} notams expired EPWA", f"{PROG} notams expired EPWA --after -3d"],
        ),
    )
    p.add_argument("locations", nargs="+", metavar="LOCATION", help="ICAO or domestic codes, up to 5")
    p.add_argument("--after", metavar="TIME", help="expired after (default 24 hours ago)")
    p.add_argument("--before", metavar="TIME", help="expired before (default now)")
    p.add_argument(
        "--exclude-classification",
        action="append",
        type=str.upper,
        choices=CLASSIFICATIONS,
        metavar="CLASS",
        help="exclude DOM, FDC, INTL or MIL",
    )
    _add_paging(p)

    p = _add_command(
        notams,
        "get",
        cmd_notams_get,
        CommandMeta(
            "One NOTAM by its Notamify ID, with the full interpretation",
            credits="1",
            examples=[f"{PROG} notams get 3f0c6c4e-8d0b-4d55-9a8e-1f6c3c1d2b7a"],
        ),
    )
    p.add_argument("notam_id", metavar="NOTAM_ID", help="Notamify NOTAM ID (the id field), not the NOTAM number")

    # affected
    p = _add_command(
        sub,
        "affected",
        cmd_affected,
        CommandMeta(
            "Affected runways, taxiways, navaids, airspace and procedures from active NOTAMs",
            credits="1 per page",
            confirmation="--all",
            examples=[f"{PROG} affected EPWA", f"{PROG} affected EGLL --affected CLOSED --all --yes"],
        ),
    )
    p.add_argument("locations", nargs="+", metavar="LOCATION", help="ICAO or domestic codes, up to 5")
    _add_time_window(p)
    _add_notam_filters(p)
    _add_paging(p)

    # briefing
    briefing = _add_group(sub, "briefing", "Generate airport and flight briefings")
    p = _add_command(
        briefing,
        "airport",
        cmd_briefing_airport,
        CommandMeta(
            "Immediate airport briefing for one location",
            credits="2",
            confirmation="always",
            examples=[f"{PROG} briefing airport EPWA --yes", f"{PROG} briefing airport EGLL --start now --end +6h --detailed --yes"],
        ),
    )
    p.add_argument("location", metavar="LOCATION", help="ICAO code")
    p.add_argument("--start", metavar="TIME", help="window start (default: start of the UTC day)")
    p.add_argument("--end", metavar="TIME", help="window end (default: end of the UTC day)")
    p.add_argument("--detailed", action="store_true", help="longer briefing")

    flight = briefing.add_parser(
        "flight", help="Asynchronous flight briefings", description="Asynchronous flight briefings", parents=[OUTPUT_OPTIONS]
    ).add_subparsers(dest="flight_command", metavar="COMMAND", required=True, parser_class=Parser)
    p = _add_command(
        flight,
        "create",
        cmd_flight_create,
        CommandMeta(
            "Start a flight briefing job; add --wait to block until it finishes",
            credits="5",
            confirmation="always",
            examples=[
                f"{PROG} briefing flight create --origin EPWA@now/+2h --destination EGLL@+2h/+3h --aircraft B738 --yes --wait",
                f"{PROG} briefing flight create --body request.json --yes",
            ],
        ),
    )
    _add_flight(p)
    p.add_argument("--wait", action="store_true", help="poll until the job completes or fails")
    p.add_argument(
        "--idempotency-key",
        metavar="KEY",
        help="account sign-in only: retries with the same key return the accepted job without a new charge "
        "(default: derived from the request)",
    )
    p.add_argument("--wait-timeout", type=float, default=300, metavar="SECONDS", help="give up waiting after this long (default 300)")
    p.add_argument("--interval", type=float, default=3, metavar="SECONDS", help="poll interval (default 3)")

    p = _add_command(
        flight,
        "status",
        cmd_flight_status,
        CommandMeta("Show a flight briefing job", credits="free"),
    )
    p.add_argument("uuid", metavar="UUID", help="job UUID from `briefing flight create`")

    p = _add_command(
        flight,
        "wait",
        cmd_flight_wait,
        CommandMeta(
            "Poll a flight briefing job until it completes (exit 0) or fails (exit 1); exit 7 on timeout",
            credits="free",
        ),
    )
    p.add_argument("uuid", metavar="UUID", help="job UUID")
    p.add_argument("--wait-timeout", type=float, default=300, metavar="SECONDS", help="give up after this long (default 300)")
    p.add_argument("--interval", type=float, default=3, metavar="SECONDS", help="poll interval (default 3)")

    # prioritize
    p = _add_command(
        sub,
        "prioritize",
        cmd_prioritize,
        CommandMeta(
            "Rate one NOTAM's priority for a specific flight",
            credits="5",
            confirmation="always",
            examples=[f"{PROG} prioritize NOTAM_ID --origin EPWA@now/+2h --destination EGLL@+2h/+3h --aircraft B738 --yes"],
        ),
    )
    p.add_argument("notam_id", metavar="NOTAM_ID", help="Notamify NOTAM ID")
    _add_flight(p)

    # watcher
    watcher = _add_group(sub, "watcher", "Manage NOTAM Watcher listeners, deliveries and the webhook secret")
    listeners = watcher.add_parser(
        "listeners", help="List, create, change, delete and test listeners", description="NOTAM Watcher listeners", parents=[OUTPUT_OPTIONS]
    ).add_subparsers(dest="listeners_command", metavar="COMMAND", required=True, parser_class=Parser)
    _add_command(listeners, "list", cmd_listeners_list, CommandMeta("List listeners", credits="free"))
    p = _add_command(listeners, "get", cmd_listeners_get, CommandMeta("Show one listener", credits="free"))
    p.add_argument("listener_id", metavar="LISTENER_ID")
    p = _add_command(
        listeners,
        "create",
        cmd_listeners_create,
        CommandMeta(
            "Create a listener; prints its webhook secret once",
            credits="production deliveries are metered",
            confirmation="always",
            examples=[
                f"{PROG} watcher listeners create --name dev --mode sandbox --webhook-url https://example.com/hook --yes",
                f"{PROG} watcher listeners create --name ops --icao EPWA --icao EPKK --affected CLOSED:RUNWAY --email ops@example.com --yes",
            ],
        ),
    )
    _add_listener_fields(p)
    p = _add_command(
        listeners,
        "update",
        cmd_listeners_update,
        CommandMeta(
            "Change only the given fields of a listener",
            credits="free",
            confirmation="always",
            examples=[f"{PROG} watcher listeners update LISTENER_ID --webhook-url https://example.com/hook --yes"],
        ),
    )
    p.add_argument("listener_id", metavar="LISTENER_ID")
    _add_listener_fields(p)
    p = _add_command(listeners, "delete", cmd_listeners_delete, CommandMeta("Delete a listener", credits="free", confirmation="always"))
    p.add_argument("listener_id", metavar="LISTENER_ID")
    p = _add_command(
        listeners,
        "test",
        cmd_listeners_test,
        CommandMeta("Send a test delivery to a sandbox listener", credits="free"),
    )
    p.add_argument("listener_id", metavar="LISTENER_ID")
    p.add_argument("--notam-id", help="NOTAM to send (default: a mock NOTAM)")

    p = _add_command(watcher, "logs", cmd_watcher_logs, CommandMeta("Recent webhook deliveries", credits="free"))
    p.add_argument("--errors-only", action="store_true", help="only failed deliveries")
    secret = watcher.add_parser(
        "secret", help="Show or rotate the webhook signing secret", description="Webhook signing secret", parents=[OUTPUT_OPTIONS]
    ).add_subparsers(dest="secret_command", metavar="COMMAND", required=True, parser_class=Parser)
    _add_command(secret, "show", cmd_secret_show, CommandMeta("Show the masked webhook secret", credits="free"))
    _add_command(
        secret,
        "rotate",
        cmd_secret_rotate,
        CommandMeta(
            "Rotate the webhook secret; the old one stops verifying immediately",
            credits="free",
            confirmation="always",
        ),
    )

    # webhook
    webhook = _add_group(sub, "webhook", "Receive Watcher deliveries locally")
    p = _add_command(
        webhook,
        "listen",
        cmd_webhook_listen,
        CommandMeta(
            "Run a local webhook receiver that verifies signatures and prints each delivery",
            network=False,
            examples=[
                f"NOTAMIFY_WEBHOOK_SECRET=... {PROG} webhook listen --tunnel",
                f"{PROG} webhook listen --port 8080 --count 1 --listen-timeout 120 --json",
            ],
        ),
        description=(
            "Run a local webhook receiver. Verifies X-Notamify-Signature with NOTAMIFY_WEBHOOK_SECRET and prints\n"
            "each delivery (one JSON object per line in JSON mode). --tunnel exposes it through a cloudflared\n"
            "quick tunnel; point a sandbox listener at the printed public URL."
        ),
    )
    p.add_argument("--host", default="127.0.0.1", help="bind address (default 127.0.0.1)")
    p.add_argument("--port", type=int, default=8080, help="port (default 8080)")
    p.add_argument("--path", default="/webhooks/notamify", help="request path (default /webhooks/notamify)")
    p.add_argument("--tunnel", action="store_true", help="start a cloudflared quick tunnel (cloudflared must be installed)")
    p.add_argument("--allow-unsigned", action="store_true", help="accept deliveries without a signature (development only)")
    p.add_argument("--count", type=int, metavar="N", help="exit after N deliveries")
    p.add_argument("--listen-timeout", type=float, metavar="SECONDS", help="stop after this long; exit 7 if --count was not reached")

    # meta
    p = _add_command(
        sub,
        "commands",
        cmd_commands,
        CommandMeta("Describe every command, option, credit cost and exit code (JSON for agents)", network=False),
    )
    skill = _add_group(sub, "skill", "The agent skill that teaches coding agents to use this CLI")
    _add_command(skill, "show", cmd_skill_show, CommandMeta("Print SKILL.md", network=False))
    p = _add_command(
        skill,
        "install",
        cmd_skill_install,
        CommandMeta(
            "Install SKILL.md into an agent's skills directory",
            network=False,
            examples=[f"{PROG} skill install", f"{PROG} skill install --dir ~/.codex/skills"],
        ),
    )
    p.add_argument(
        "--dir",
        default="~/.claude/skills",
        help="skills directory; the skill goes in DIR/notamify/SKILL.md (default ~/.claude/skills)",
    )
    p.add_argument("--force", action="store_true", help="overwrite an existing SKILL.md")
    return parser


# Context -----------------------------------------------------------------


class Context:
    def __init__(self, args: argparse.Namespace, out: Output, argv: Sequence[str]) -> None:
        self.args = args
        self.out = out
        self.argv = list(argv)
        self._client: NotamifyClient | None = None
        self._backends: dict[str, Backend] = {}

    @property
    def yes(self) -> bool:
        return bool(getattr(self.args, "yes", False))

    @property
    def dry_run(self) -> bool:
        return bool(getattr(self.args, "dry_run", False))

    def _new_client(self) -> NotamifyClient:
        timeout = getattr(self.args, "timeout", None)
        kwargs: dict[str, Any] = {"user_agent": f"notamify-cli/{SDK_VERSION}"}
        if timeout:
            kwargs["timeout"] = timeout
        return NotamifyClient(**kwargs)

    @property
    def client(self) -> NotamifyClient:
        """The API-key client, used by Watcher commands. Account sign-in does not cover Watcher."""
        if self._client is None:
            if not _api_key_available() and AccountStore().load() is not None and not self.dry_run:
                raise requires_api_key("watcher")
            self._client = self._new_client()
        return self._client

    def backend(self, operation: str) -> Backend:
        """Pick API key or account for a NOTAM or briefing operation.

        auto: NOTAMIFY_TOKEN wins, then a signed-in account, then a stored API key.
        """
        choice = getattr(self.args, "auth", None) or os.getenv("NOTAMIFY_AUTH", "").strip().lower() or "auto"
        if choice not in ("auto", "account", "api-key"):
            raise CLIError("invalid_arguments", f"invalid auth {choice!r}; use auto, account or api-key", EXIT_USAGE)
        signed_in = AccountStore().load() is not None
        if choice == "auto":
            if _env_token():
                choice = "api-key"
            elif signed_in and operation in ACCOUNT_TOOLS:
                choice = "account"
            elif _api_key_available():
                choice = "api-key"
            elif signed_in:
                raise requires_api_key(operation)
            else:
                raise CLIError(
                    "auth_missing",
                    "Not signed in and no API key is configured.",
                    EXIT_AUTH,
                    f"Run `{PROG} auth login` to sign in with your Notamify account, "
                    f"or `{PROG} auth login --api-key` / set NOTAMIFY_TOKEN to use an API key.",
                )
        if choice == "account":
            if operation not in ACCOUNT_TOOLS:
                raise requires_api_key(operation)
            if not signed_in and not self.dry_run:
                raise CLIError("not_signed_in", "Not signed in to a Notamify account.", EXIT_AUTH, f"Run `{PROG} auth login`.")
        key = "account" if choice == "account" else "api_key"
        if key not in self._backends:
            self._backends[key] = AccountBackend() if key == "account" else ApiKeyBackend(self._new_client)
        return self._backends[key]

    def cost_text(self, backend: Backend, operation: str, calls: int = 1, *, up_to: bool = False) -> str:
        rate = backend.cost(operation)
        if rate is None:
            return "billed at your account's connected-app rate" if backend.mode == "account" else "unknown"
        if rate == 0:
            return "free"
        total = rate * calls
        text = f"{'up to ' if up_to else ''}{total} credit{'s' if total != 1 else ''}"
        if calls > 1:
            text += f" ({rate} per page)"
        if backend.mode == "account":
            text += ", account rate"
        return text

    def confirm_command(self) -> str:
        return shlex.join([PROG, *self.argv, "--yes"])

    def confirm(self, action: str, credits: str | None) -> None:
        """Require --yes, or an interactive yes, before spending credits or changing state."""
        if self.yes:
            return
        cost = f" Estimated cost: {credits}." if credits else ""
        if interactive() and not self.out.json:
            self.out.stderr.write(f"{action}.{cost} Continue? [y/N] ")
            self.out.stderr.flush()
            answer = sys.stdin.readline().strip().lower()
            if answer in {"y", "yes"}:
                return
            raise CLIError("cancelled", "Cancelled.", EXIT_CONFIRMATION_REQUIRED)
        raise CLIError(
            "confirmation_required",
            f"{action} needs confirmation.{cost}",
            EXIT_CONFIRMATION_REQUIRED,
            "Ask the user, then rerun with --yes.",
            details={"action": action, "credits_estimate": credits, "confirm_command": self.confirm_command()},
        )

    def plan_operation(self, backend: Backend, operation: str, payload: Mapping[str, Any], credits: str) -> bool:
        """Print the request instead of sending it under --dry-run. Returns True when it did."""
        if not self.dry_run:
            return False
        described = backend.describe(operation, payload)
        data = {"dry_run": True, **described, "credits_estimate": credits}

        def render(d: Mapping[str, Any]) -> str:
            req = d["request"]
            head = f"{req['method']} {req['url']}" if "url" in req else f"MCP tool {req['tool']} on {req['server']}"
            body = req.get("body") or req.get("arguments")
            return (
                head
                + (f"\n{json.dumps(body, indent=2)}" if body else "")
                + f"\nauth: {backend.label}; credits: {credits} (dry run, nothing sent)"
            )

        self.out.emit(data, render)
        return True

    def plan(self, method: str, base: str, path: str, *, query: Mapping[str, Any] | None = None, body: Any = None, credits: str | None = None) -> bool:
        """Dry run for Watcher requests (API key)."""
        if not self.dry_run:
            return False
        client = self.client
        url = (client.api_base_url if base == "api" else client.watcher_base_url) + path
        encoded = client._encode_query({k: v for k, v in (query or {}).items() if v is not None}) if query else ""
        request: dict[str, Any] = {"method": method, "url": f"{url}?{encoded}" if encoded else url}
        if body is not None:
            request["body"] = to_jsonable(body)
        self.out.emit(
            {"dry_run": True, "auth": "api_key", "request": request, "credits_estimate": credits, "authenticated": bool(client.token)},
            lambda d: f"{d['request']['method']} {d['request']['url']}"
            + (f"\n{json.dumps(d['request']['body'], indent=2)}" if "body" in d["request"] else "")
            + f"\ncredits: {credits or 'free'} (dry run, nothing sent)",
        )
        return True


def _env_token() -> str:
    return os.getenv("NOTAMIFY_TOKEN", "").strip() or os.getenv("NOTAMIFY_WATCHER_TOKEN", "").strip()


def _api_key_available() -> bool:
    if _env_token():
        return True
    store = ConfigStore()
    if not store.path.exists():
        return False
    try:
        return bool(store.load().token)
    except (OSError, ValueError):
        return False


# Helpers -----------------------------------------------------------------


def _time(value: str | None, *, base: Any = None, end_of_day: bool = False) -> Any:
    if value is None:
        return None
    try:
        return parse_time(value, base=base, end_of_day=end_of_day)
    except ValueError as exc:
        raise CLIError("invalid_time", str(exc), EXIT_USAGE) from exc


def _window(args: argparse.Namespace) -> tuple[Any, Any]:
    start = _time(args.start)
    if args.hours is not None and args.end is not None:
        raise CLIError("invalid_arguments", "use --end or --hours, not both", EXIT_USAGE)
    if args.hours is not None:
        if args.hours <= 0:
            raise CLIError("invalid_arguments", "--hours must be positive", EXIT_USAGE)
        start = start or now_utc()
        return start, _time(f"+{args.hours}h", base=start)
    end = _time(args.end, base=start, end_of_day=True)
    if start and end and end <= start:
        raise CLIError("invalid_arguments", "the window end must be after its start", EXIT_USAGE)
    return start, end


def _affected(values: Iterable[str] | None) -> list[dict[str, str]] | None:
    if not values:
        return None
    filters = []
    for value in values:
        effect, _, element_type = value.partition(":")
        entry = {k: v.strip().upper() for k, v in (("effect", effect), ("type", element_type)) if v.strip()}
        if not entry:
            raise CLIError("invalid_arguments", f"invalid --affected {value!r}; use EFFECT[:TYPE]", EXIT_USAGE)
        filters.append(entry)
    return filters


def _locations(values: Sequence[str] | None, *, required: bool = False) -> list[str] | None:
    locations = [v.strip().upper() for v in values or [] if v.strip()]
    if len(locations) > 5:
        raise CLIError("invalid_arguments", "at most 5 locations per request", EXIT_USAGE)
    if required and not locations:
        raise CLIError("invalid_arguments", "give at least one location", EXIT_USAGE)
    return locations or None


def _drop_none(values: Mapping[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in values.items() if v is not None and v != []}


def _call(fn: Callable[[], Any]) -> Any:
    try:
        return fn()
    except APIError as exc:
        raise error_from_api(exc) from exc
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


def _validation_message(exc: ValidationError) -> str:
    parts = []
    for item in exc.errors()[:5]:
        location = ".".join(str(p) for p in item.get("loc", ()))
        parts.append(f"{location}: {item.get('msg')}" if location else str(item.get("msg")))
    return "; ".join(parts) or str(exc)


def _envelope(extra: Mapping[str, Any]) -> dict[str, Any]:
    return {**extra, "source": "Notamify", "retrieved_at": format_iso(now_utc()), "disclaimer": DISCLAIMER}


# NOTAM lists ---------------------------------------------------------------


class _Charges:
    """Credits for one command: charged amounts from account receipts, estimates otherwise."""

    def __init__(self, backend: Backend, operation: str) -> None:
        self.backend = backend
        self.operation = operation
        self.calls = 0
        self.charged: int | None = None
        self.pricing_mode: str | None = None

    def add(self, response: Mapping[str, Any]) -> None:
        self.calls += 1
        charged, mode = credits_charged(response)
        if charged is not None:
            self.charged = (self.charged or 0) + charged
            self.pricing_mode = mode or self.pricing_mode

    def fields(self) -> dict[str, Any]:
        rate = self.backend.cost(self.operation)
        estimate = self.charged if self.charged is not None else (rate * self.calls if rate is not None else None)
        data: dict[str, Any] = {"auth": self.backend.mode, "credits_estimate": estimate}
        if self.charged is not None:
            data["credits_charged"] = self.charged
            data["pricing_mode"] = self.pricing_mode
        return data


def _credits_phrase(data: Mapping[str, Any]) -> str:
    if data.get("credits_charged") is not None:
        return f"{data['credits_charged']} credit(s) charged"
    if data.get("credits_estimate") is not None:
        return f"~{data['credits_estimate']} credit(s)"
    return "credits: see your account"


def _list_notams(
    ctx: Context,
    operation: str,
    query: dict[str, Any],
    *,
    transform: Callable[[list[dict[str, Any]]], dict[str, Any]] | None = None,
    render: Callable[[dict[str, Any]], str] | None = None,
) -> None:
    args = ctx.args
    if args.page < 1:
        raise CLIError("invalid_arguments", "--page must be 1 or more", EXIT_USAGE)
    if not 1 <= args.per_page <= 30:
        raise CLIError("invalid_arguments", "--per-page must be from 1 to 30", EXIT_USAGE)
    if args.max_pages < 1:
        raise CLIError("invalid_arguments", "--max-pages must be 1 or more", EXIT_USAGE)
    backend = ctx.backend(operation)
    max_pages = args.max_pages if args.all else 1
    credits = ctx.cost_text(backend, operation, max_pages, up_to=max_pages > 1)
    first = {**query, "page": args.page, "per_page": args.per_page}
    if ctx.plan_operation(backend, operation, first, credits):
        return
    if max_pages > 1:
        ctx.confirm(f"Fetching up to {max_pages} pages", credits)

    charges = _Charges(backend, operation)
    notams: list[dict[str, Any]] = []
    page = args.page
    total = 0
    complete = False
    while charges.calls < max_pages:
        result = backend.call(operation, {**query, "page": page, "per_page": args.per_page})
        charges.add(result)
        total = int(result.get("total_count") or 0)
        batch = [n for n in result.get("notams") or [] if isinstance(n, dict)]
        notams.extend(batch)
        # total_count counts NOTAMs before category, subcategory and affected-element filters,
        # which the API applies after paging: a short or empty page is not the end.
        if page * args.per_page >= total:
            complete = True
            break
        page += 1

    fields = [f.strip() for f in (args.fields or "").split(",") if f.strip()]
    filtered = any(query.get(k) for k in ("category", "subcategory", "affected_element"))
    body: dict[str, Any] = {
        "total_count": total,
        "filtered_after_paging": filtered,
        "page": args.page,
        "per_page": args.per_page,
        "pages_fetched": charges.calls,
        "complete": complete,
        # On an incomplete result, page already points past the last fetched page.
        "next_page": None if complete else page,
        **charges.fields(),
    }
    if transform is not None:
        body = {**transform(notams), **body}
    else:
        body = {"notams": [pick_fields(n, fields) for n in notams] if fields else notams, **body}
    ctx.out.emit(_envelope(body), render or _render_notam_list)


def _render_notam_list(data: Mapping[str, Any]) -> str:
    lines = [_render_notam_line(notam) for notam in data.get("notams", [])]
    before_filters = " (before filters)" if data.get("filtered_after_paging") else ""
    lines.append(
        f"\n{len(data.get('notams', []))} of {data.get('total_count', 0)}{before_filters} NOTAMs shown; "
        f"{data.get('pages_fetched', 0)} page(s), {_credits_phrase(data)}."
        + ("" if data.get("complete") else f" More: --page {data.get('next_page')} or --all.")
    )
    return "\n".join(lines)


def _render_notam_line(notam: Mapping[str, Any]) -> str:
    interpretation = notam.get("interpretation") or {}
    validity = f"{format_short(notam.get('starts_at'))} → {format_short(notam.get('ends_at'))}"
    if notam.get("is_estimated"):
        validity += " EST"
    if notam.get("is_permanent"):
        validity += " PERM"
    category = "/".join(str(v) for v in (interpretation.get("category"), interpretation.get("subcategory")) if v)
    parts = [
        str(notam.get("notam_number", "")),
        str(notam.get("location") or notam.get("icao_code") or ""),
        validity,
        f"[{category}]" if category else "",
        f"expired {format_short(notam.get('expired_at'))}" if notam.get("expired_at") else "",
    ]
    lines = ["  ".join(part for part in parts if part)]
    summary = interpretation.get("excerpt") or " ".join(str(notam.get("message", "")).split())[:200]
    if summary:
        lines.append(f"  {summary}")
    if notam.get("id"):
        lines.append(f"  id {notam['id']}")
    return "\n".join(lines)


def _active_query(args: argparse.Namespace, *, require_location: bool) -> dict[str, Any]:
    locations = _locations(args.locations)
    if require_location and not locations and not args.notam_ids:
        raise CLIError("invalid_arguments", "give at least one location or --id", EXIT_USAGE)
    start, end = _window(args)
    return _drop_none(
        {
            "location": locations,
            "notam_ids": args.notam_ids,
            "starts_at": start,
            "ends_at": end,
            "category": getattr(args, "category", None),
            "subcategory": getattr(args, "subcategory", None),
            "qcode": args.qcode,
            "affected_element": _affected(getattr(args, "affected", None)),
            "excluded_classifications": args.exclude_classification,
            "always_include_est": False if args.no_est else None,
        }
    )


def cmd_notams_active(ctx: Context) -> None:
    _list_notams(ctx, "active", _active_query(ctx.args, require_location=True))


def cmd_notams_raw(ctx: Context) -> None:
    _list_notams(ctx, "raw", _active_query(ctx.args, require_location=True))


def cmd_notams_nearby(ctx: Context) -> None:
    args = ctx.args
    start, end = _window(args)
    query = _drop_none(
        {
            "lat": args.lat,
            "lon": args.lon,
            "radius_nm": args.radius,
            "notam_ids": args.notam_ids,
            "starts_at": start,
            "ends_at": end,
            "category": args.category,
            "subcategory": args.subcategory,
            "qcode": args.qcode,
            "affected_element": _affected(args.affected),
            "excluded_classifications": args.exclude_classification,
            "always_include_est": False if args.no_est else None,
        }
    )
    _list_notams(ctx, "nearby", query)


def cmd_notams_archive(ctx: Context) -> None:
    args = ctx.args
    locations = _locations(args.locations)
    if not locations and not args.notam_ids:
        raise CLIError("invalid_arguments", "give at least one location or --id", EXIT_USAGE)
    try:
        valid_at = parse_time(args.date).date()
    except ValueError as exc:
        raise CLIError("invalid_time", str(exc), EXIT_USAGE) from exc
    query = _drop_none(
        {
            "valid_at": valid_at,
            "location": locations,
            "notam_ids": args.notam_ids,
            "category": args.category,
            "subcategory": args.subcategory,
            "affected_element": _affected(args.affected),
            "always_include_est": False if args.no_est else None,
        }
    )
    _list_notams(ctx, "archive", query)


def cmd_notams_expired(ctx: Context) -> None:
    args = ctx.args
    before = _time(args.before)
    after = _time(args.after, base=before)
    if after and before and before <= after:
        raise CLIError("invalid_arguments", "--before must be after --after", EXIT_USAGE)
    query = _drop_none(
        {
            "location": _locations(args.locations, required=True),
            "expired_after": after,
            "expired_before": before,
            "excluded_classifications": args.exclude_classification,
        }
    )
    _list_notams(ctx, "expired", query)


def cmd_notams_get(ctx: Context) -> None:
    payload = {"notam_id": ctx.args.notam_id.strip()}
    backend = ctx.backend("notam")
    credits = ctx.cost_text(backend, "notam")
    if ctx.plan_operation(backend, "notam", payload, credits):
        return
    result = backend.call("notam", payload)
    charges = _Charges(backend, "notam")
    charges.add(result)
    ctx.out.emit(_envelope({"notam": strip_extras(result), **charges.fields()}), lambda d: _render_notam_detail(d["notam"]))


def _render_notam_detail(notam: Mapping[str, Any]) -> str:
    lines = [_render_notam_line(notam), ""]
    lines.append(str(notam.get("icao_message") or notam.get("message") or "").strip())
    interpretation = notam.get("interpretation") or {}
    if interpretation:
        lines += ["", "Interpretation (informational):", f"  {interpretation.get('description') or interpretation.get('excerpt') or ''}"]
        for element in interpretation.get("affected_elements") or []:
            detail = f" — {element['details']}" if element.get("details") else ""
            lines.append(f"  • {element.get('effect')} {element.get('type')} {element.get('identifier')}{detail}")
        if interpretation.get("schedule_description"):
            lines.append(f"  Schedule: {interpretation['schedule_description']}")
    return "\n".join(lines)


# Affected elements ---------------------------------------------------------


def cmd_affected(ctx: Context) -> None:
    query = _active_query(ctx.args, require_location=True)

    def transform(notams: list[dict[str, Any]]) -> dict[str, Any]:
        elements = []
        for notam in notams:
            for element in (notam.get("interpretation") or {}).get("affected_elements") or []:
                if not isinstance(element, dict):
                    continue
                elements.append(
                    _drop_none(
                        {
                            "location": notam.get("location") or notam.get("icao_code"),
                            "type": element.get("type"),
                            "subtype": element.get("subtype"),
                            "identifier": element.get("identifier"),
                            "effect": element.get("effect"),
                            "details": element.get("details"),
                            "semantics": element.get("semantics"),
                            "notam_id": notam.get("id"),
                            "notam_number": notam.get("notam_number"),
                            "starts_at": notam.get("starts_at"),
                            "ends_at": notam.get("ends_at"),
                            "is_estimated": notam.get("is_estimated"),
                            "schedule": (notam.get("interpretation") or {}).get("schedule_description"),
                        }
                    )
                )
        fields = [f.strip() for f in (ctx.args.fields or "").split(",") if f.strip()]
        if fields:
            elements = [pick_fields(e, fields) for e in elements]
        return {"elements": elements, "notams_scanned": len(notams)}

    def render(data: Mapping[str, Any]) -> str:
        rows = [("LOCATION", "TYPE", "IDENTIFIER", "EFFECT", "NOTAM", "VALID")]
        for e in data["elements"]:
            rows.append(
                (
                    str(e.get("location", "")),
                    str(e.get("type", "")),
                    str(e.get("identifier", "")),
                    str(e.get("effect", "")),
                    str(e.get("notam_number", "")),
                    f"{format_short(e.get('starts_at'))} → {format_short(e.get('ends_at'))}",
                )
            )
        widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
        table = "\n".join("  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip() for r in rows)
        footer = (
            f"\n{len(data['elements'])} affected elements in {data['notams_scanned']} of {data['total_count']} NOTAMs; "
            f"{_credits_phrase(data)}."
        )
        if not data["complete"]:
            footer += f" Incomplete: more NOTAMs on --page {data['next_page']}, or use --all."
        return table + footer

    _list_notams(ctx, "active", query, transform=transform, render=render)


# Briefings -----------------------------------------------------------------


def cmd_briefing_airport(ctx: Context) -> None:
    args = ctx.args
    start = _time(args.start)
    end = _time(args.end, base=start, end_of_day=True)
    if start and end and end <= start:
        raise CLIError("invalid_arguments", "--end must be after --start", EXIT_USAGE)
    query = _drop_none(
        {
            "location": args.location.strip().upper(),
            "starts_at": start,
            "ends_at": end,
            "type": "detailed" if args.detailed else None,
        }
    )
    backend = ctx.backend("airport")
    credits = ctx.cost_text(backend, "airport")
    if ctx.plan_operation(backend, "airport", query, credits):
        return
    ctx.confirm(f"Generating an airport briefing for {query['location']}", credits)
    result = backend.call("airport", query)
    charges = _Charges(backend, "airport")
    charges.add(result)
    briefing = strip_extras(result)
    ctx.out.emit(
        _envelope({"briefing": briefing, **charges.fields()}),
        lambda d: f"{str(briefing.get('summary', '')).strip()}\n\nNOTAMs: {', '.join(briefing.get('notam_ids') or []) or '-'}"
        f"\n{_credits_phrase(d)}.\n\n{DISCLAIMER}",
    )


def _location_spec(value: str, location_type: str | None) -> dict[str, Any]:
    code, sep, window = value.partition("@")
    code = code.strip().upper()
    if not code or not sep or "/" not in window:
        raise CLIError(
            "invalid_arguments",
            f"invalid location {value!r}; use ICAO@START/END, e.g. EPWA@2026-11-09T08:00Z/2026-11-09T10:00Z or EPWA@now/+2h",
            EXIT_USAGE,
        )
    raw_start, raw_end = window.split("/", 1)
    start = _time(raw_start)
    end = _time(raw_end, base=start, end_of_day=True)
    if end <= start:
        raise CLIError("invalid_arguments", f"{code}: window end must be after its start", EXIT_USAGE)
    return _drop_none({"location": code, "type": location_type, "starts_at": start, "ends_at": end})


def _flight_request(args: argparse.Namespace) -> dict[str, Any]:
    flags = ("origin", "destination", "alternate", "fir", "aircraft", "origin_runway", "destination_runway", "destination_procedure")
    if args.body:
        if any(getattr(args, name) for name in flags):
            raise CLIError("invalid_arguments", "--body replaces the flight flags; use one or the other", EXIT_USAGE)
        try:
            text = sys.stdin.read() if args.body == "-" else Path(args.body).expanduser().read_text(encoding="utf-8")
            body = json.loads(text)
        except (OSError, json.JSONDecodeError) as exc:
            raise CLIError("invalid_body", f"cannot read JSON body: {exc}", EXIT_USAGE) from exc
        if not isinstance(body, dict):
            raise CLIError("invalid_body", "the JSON body must be an object", EXIT_USAGE)
        return body
    locations = []
    if args.origin:
        locations.append(_location_spec(args.origin, "origin"))
    if args.destination:
        locations.append(_location_spec(args.destination, "destination"))
    locations += [_location_spec(v, "alternate") for v in args.alternate or []]
    locations += [_location_spec(v, "fir") for v in args.fir or []]
    if not locations:
        raise CLIError("invalid_arguments", "give --origin and/or --destination (or --body)", EXIT_USAGE)
    return _drop_none(
        {
            "locations": locations,
            "aircraft_type": args.aircraft.upper() if args.aircraft else None,
            "origin_runway": args.origin_runway,
            "destination_runway": args.destination_runway,
            "destination_procedure": args.destination_procedure,
        }
    )


def cmd_flight_create(ctx: Context) -> None:
    body = _flight_request(ctx.args)
    if getattr(ctx.args, "idempotency_key", None):
        body["idempotency_key"] = ctx.args.idempotency_key
    backend = ctx.backend("flight_create")
    if backend.mode != "account":
        body.pop("idempotency_key", None)
    credits = ctx.cost_text(backend, "flight_create")
    if ctx.plan_operation(backend, "flight_create", body, credits):
        return
    route = " → ".join(str(loc.get("location")) for loc in body.get("locations", []))
    ctx.confirm(f"Creating a flight briefing for {route}", credits)
    job = backend.call("flight_create", body)
    charges = _Charges(backend, "flight_create")
    charges.add(job)
    job_id = str(job.get("uuid", ""))
    if ctx.args.wait:
        _announce_job(ctx, job_id)
        _wait_for_briefing(ctx, job_id, backend)
        return
    ctx.out.emit(
        _envelope({"job": strip_extras(job), **charges.fields(), "next": f"{PROG} briefing flight wait {job_id}"}),
        lambda d: f"Briefing job {job_id} is {job.get('status') or 'pending'}.\nWait for it: {PROG} briefing flight wait {job_id}",
    )


def cmd_flight_status(ctx: Context) -> None:
    job_id = _uuid(ctx.args.uuid)
    backend = ctx.backend("flight_status")
    if ctx.plan_operation(backend, "flight_status", {"uuid": job_id}, "free"):
        return
    status = backend.call("flight_status", {"uuid": job_id})
    ctx.out.emit(_envelope({"job": strip_extras(status), "auth": backend.mode}), _render_briefing_status)


def cmd_flight_wait(ctx: Context) -> None:
    job_id = _uuid(ctx.args.uuid)
    backend = ctx.backend("flight_status")
    if ctx.plan_operation(backend, "flight_status", {"uuid": job_id}, "free"):
        return
    _wait_for_briefing(ctx, job_id, backend)


def _uuid(value: str) -> str:
    try:
        return str(uuidlib.UUID(value.strip()))
    except ValueError as exc:
        raise CLIError("invalid_arguments", f"invalid briefing UUID {value!r}", EXIT_USAGE) from exc


def _announce_job(ctx: Context, job_id: str) -> None:
    """Report the accepted job before waiting, so it is never lost if waiting fails."""
    if ctx.out.json:
        ctx.out.stderr.write(json.dumps({"event": "briefing_accepted", "uuid": job_id}) + "\n")
        ctx.out.stderr.flush()
    else:
        ctx.out.note(f"Briefing job {job_id} accepted; waiting… (check later: {PROG} briefing flight wait {job_id})")


def _wait_for_briefing(ctx: Context, job_id: str, backend: Backend) -> None:
    try:
        _poll_briefing(ctx, job_id, backend)
    except CLIError as exc:
        exc.details.setdefault("uuid", job_id)
        if exc.code not in ("briefing_failed",):
            exc.remediation = (exc.remediation + " " if exc.remediation else "") + (
                f"The job exists; do not create it again. Check it with `{PROG} briefing flight wait {job_id}`."
            )
        raise
    except KeyboardInterrupt:
        ctx.out.stderr.write(f"\nBriefing job {job_id} keeps running; check it with `{PROG} briefing flight wait {job_id}`.\n")
        raise


def _poll_briefing(ctx: Context, job_id: str, backend: Backend) -> None:
    args = ctx.args
    deadline = time.monotonic() + max(args.wait_timeout, 0)
    interval = max(args.interval, 0.5)
    while True:
        status = backend.call("flight_status", {"uuid": job_id})
        state = str(status.get("status") or "")
        if state == "completed":
            ctx.out.emit(_envelope({"job": strip_extras(status), "auth": backend.mode}), _render_briefing_status)
            return
        if state == "failed":
            raise CLIError(
                "briefing_failed",
                f"Briefing job {job_id} failed.",
                EXIT_ERROR,
                "The job is terminal; do not keep polling. Create a new briefing if needed.",
                details={"uuid": job_id},
            )
        if time.monotonic() + interval > deadline:
            raise CLIError(
                "timeout",
                f"Briefing job {job_id} is still {state or 'pending'}.",
                EXIT_TEMPORARY,
                f"Polling is free: rerun `{PROG} briefing flight wait {job_id}`.",
                details={"uuid": job_id, "status": state},
            )
        time.sleep(interval)


def _render_briefing_status(data: Mapping[str, Any]) -> str:
    job = to_jsonable(data["job"])
    lines = [f"Briefing {job.get('uuid')}: {job.get('status')} (updated {format_short(job.get('updated_at'))})"]
    response = job.get("response") or {}
    briefing = response.get("briefing") or {}
    if briefing:
        lines += ["", str(briefing.get("text", "")).strip()]
        for group in briefing.get("critical_operational_restrictions") or []:
            lines.append(f"\nCritical restrictions — {group.get('location_code')} ({group.get('location_role') or '-'}):")
            lines += [f"  • {item}" for item in group.get("items") or []]
        lines += ["", DISCLAIMER]
    return "\n".join(lines)


def cmd_prioritize(ctx: Context) -> None:
    body = {**_flight_request(ctx.args), "notam_id": ctx.args.notam_id.strip()}
    backend = ctx.backend("prioritize")
    credits = ctx.cost_text(backend, "prioritize")
    if ctx.plan_operation(backend, "prioritize", body, credits):
        return
    ctx.confirm(f"Prioritizing NOTAM {body['notam_id']} for this flight", credits)
    result = backend.call("prioritize", body)
    charges = _Charges(backend, "prioritize")
    charges.add(result)

    def render(data: Mapping[str, Any]) -> str:
        lines = []
        for item in data["result"].get("notams") or []:
            notam = item.get("notam") or {}
            lines.append(f"{item.get('priority')}  {notam.get('notam_number')}  {notam.get('location')}")
            lines.append(f"  {item.get('explanation', '')}")
            lines += [f"  • {c}" for c in item.get("considerations") or []]
        return "\n".join(lines + ["", DISCLAIMER])

    ctx.out.emit(_envelope({"result": strip_extras(result), **charges.fields()}), render)


# Watcher ------------------------------------------------------------------


def _render_listener(listener: Mapping[str, Any]) -> str:
    targets = listener.get("webhook_url") or ", ".join(listener.get("emails") or []) or "-"
    state = "active" if listener.get("active") else "paused"
    filters = {k: v for k, v in (listener.get("filters") or {}).items() if v}
    lines = [
        f"{listener.get('id')}  {listener.get('name') or '(unnamed)'}  [{listener.get('mode')}, {state}]",
        f"  delivers to {targets}",
        f"  filters {json.dumps(filters) if filters else '(all NOTAMs)'}",
        f"  lifecycle {'on' if (listener.get('lifecycle') or {}).get('enabled') else 'off'}, "
        f"{(listener.get('metadata') or {}).get('notams_shipped', 0)} NOTAMs delivered",
    ]
    if listener.get("webhook_secret"):
        lines.append(f"  webhook secret {listener['webhook_secret']}  (shown once; store it now)")
    return "\n".join(lines)


def cmd_listeners_list(ctx: Context) -> None:
    if ctx.plan("GET", "watcher", "/listeners", credits="free"):
        return
    listeners = _call(lambda: ctx.client.list_listeners())
    ctx.out.emit(
        {"listeners": listeners},
        lambda d: "\n\n".join(_render_listener(to_jsonable(l)) for l in listeners) or "No listeners.",
    )


def cmd_listeners_get(ctx: Context) -> None:
    if ctx.plan("GET", "watcher", "/listeners", credits="free"):
        return
    listener = _call(lambda: ctx.client.get_listener(ctx.args.listener_id))
    ctx.out.emit({"listener": listener}, lambda d: _render_listener(to_jsonable(listener)))


def _listener_changes(args: argparse.Namespace) -> dict[str, Any]:
    filters: dict[str, Any] = {}
    if args.filters_json:
        try:
            given = json.loads(args.filters_json)
        except json.JSONDecodeError as exc:
            raise CLIError("invalid_arguments", f"--filters-json is not valid JSON: {exc}", EXIT_USAGE) from exc
        if not isinstance(given, dict):
            raise CLIError("invalid_arguments", "--filters-json must be a JSON object", EXIT_USAGE)
        filters.update(given)
    for key, value in (
        ("notam_icao", args.icao),
        ("category", args.category),
        ("subcategory", args.subcategory),
        ("qcode", args.qcode),
        ("notam_type", args.notam_type),
        ("airport_type", args.airport_type),
        ("affected_element", _affected(args.affected)),
    ):
        if value:
            filters[key] = value
    changes: dict[str, Any] = _drop_none(
        {
            "name": args.name,
            "webhook_url": args.webhook_url,
            "emails": args.emails,
            "mode": args.mode,
            "active": args.active,
        }
    )
    if args.lifecycle_enabled is not None or args.lifecycle_type:
        lifecycle: dict[str, Any] = {}
        if args.lifecycle_enabled is not None:
            lifecycle["enabled"] = args.lifecycle_enabled
        if args.lifecycle_type:
            lifecycle["types"] = args.lifecycle_type
            lifecycle.setdefault("enabled", True)
        changes["lifecycle"] = lifecycle
    if filters:
        changes["filters"] = filters
    return changes


def cmd_listeners_create(ctx: Context) -> None:
    changes = _listener_changes(ctx.args)
    if not changes.get("webhook_url") and not changes.get("emails"):
        raise CLIError("invalid_arguments", "give --webhook-url or --email", EXIT_USAGE)
    changes.setdefault("filters", {})
    if ctx.plan("POST", "watcher", "/listeners", body=changes, credits="free; production deliveries are metered"):
        return
    mode = changes.get("mode", "prod")
    ctx.confirm(
        f"Creating a {mode} listener" + (" that delivers real NOTAMs" if mode == "prod" else ""),
        "production deliveries are metered" if mode == "prod" else None,
    )
    listener = _call(lambda: ctx.client.create_listener(**changes))
    if listener.webhook_secret:
        ctx.out.note("The webhook secret is shown once. Store it as NOTAMIFY_WEBHOOK_SECRET.")
    ctx.out.emit({"listener": listener}, lambda d: _render_listener(to_jsonable(listener)))


def cmd_listeners_update(ctx: Context) -> None:
    args = ctx.args
    changes = _listener_changes(args)
    if not changes:
        raise CLIError("invalid_arguments", "nothing to change; pass at least one listener option", EXIT_USAGE)
    if ctx.plan("PUT", "watcher", f"/listeners/{args.listener_id}", body={"changes": changes}, credits="free"):
        return
    ctx.confirm(f"Updating listener {args.listener_id} ({', '.join(sorted(changes))})", None)
    listener = _call(lambda: ctx.client.patch_listener(args.listener_id, **changes))
    ctx.out.emit({"listener": listener}, lambda d: _render_listener(to_jsonable(listener)))


def cmd_listeners_delete(ctx: Context) -> None:
    listener_id = ctx.args.listener_id
    if ctx.plan("DELETE", "watcher", f"/listeners/{listener_id}", credits="free"):
        return
    ctx.confirm(f"Deleting listener {listener_id}", None)
    _call(lambda: ctx.client.delete_listener(listener_id))
    ctx.out.emit({"deleted": True, "listener_id": listener_id}, lambda d: f"Deleted listener {listener_id}.")


def cmd_listeners_test(ctx: Context) -> None:
    args = ctx.args
    body = _drop_none({"notam_id": args.notam_id})
    if ctx.plan("POST", "watcher", f"/listeners/{args.listener_id}/sandbox:send", body=body, credits="free"):
        return
    result = _call(lambda: ctx.client.send_sandbox_message(args.listener_id, args.notam_id))
    ctx.out.emit(
        {"delivery": result},
        lambda d: f"Sent test NOTAM {result.notam_id} to listener {result.listener_id} at {result.sent_at}.",
    )


def cmd_watcher_logs(ctx: Context) -> None:
    if ctx.plan("GET", "watcher", "/webhook-logs", credits="free"):
        return
    logs = _call(lambda: ctx.client.get_webhook_logs())
    data = {"errors": logs.errors} if ctx.args.errors_only else {"success": logs.success, "errors": logs.errors}

    def render(_: Any) -> str:
        rows = []
        entries = [("error", e) for e in logs.errors]
        if not ctx.args.errors_only:
            entries = [("ok", e) for e in logs.success] + entries
        for kind, entry in sorted(entries, key=lambda pair: pair[1].created_at, reverse=True):
            rows.append(
                f"{entry.created_at}  {kind:5}  {entry.status}  listener {entry.listener_id}  NOTAM {entry.notam_id}"
                + (f"\n    {entry.error}" if entry.error else "")
            )
        return "\n".join(rows) or "No deliveries."

    ctx.out.emit(data, render)


def cmd_secret_show(ctx: Context) -> None:
    if ctx.plan("GET", "watcher", "/webhook-secret", credits="free"):
        return
    masked = _call(lambda: ctx.client.get_webhook_secret_masked())
    ctx.out.emit({"webhook_secret_masked": masked}, lambda d: masked)


def cmd_secret_rotate(ctx: Context) -> None:
    if ctx.plan("POST", "watcher", "/webhook-secret:rotate", credits="free"):
        return
    ctx.confirm("Rotating the webhook secret (the current secret stops verifying)", None)
    secret = _call(lambda: ctx.client.rotate_webhook_secret())
    ctx.out.note("Shown once. Update NOTAMIFY_WEBHOOK_SECRET wherever deliveries are verified.")
    ctx.out.emit({"webhook_secret": secret}, lambda d: secret)


# Local webhook receiver ----------------------------------------------------


def cmd_webhook_listen(ctx: Context) -> None:
    args = ctx.args
    secret = os.getenv("NOTAMIFY_WEBHOOK_SECRET", "").strip()
    if not secret and not args.allow_unsigned:
        raise CLIError(
            "webhook_secret_missing",
            "NOTAMIFY_WEBHOOK_SECRET is not set.",
            EXIT_USAGE,
            f"Export the secret from `{PROG} watcher listeners create`, or pass --allow-unsigned for local experiments.",
        )
    path = args.path if args.path.startswith("/") else f"/{args.path}"
    events: "queue.Queue[ReceivedEvent]" = queue.Queue()
    receiver = WebhookReceiver(
        ReceiverConfig(host=args.host, port=args.port, path=path, secret=secret, allow_unsigned_dev=args.allow_unsigned),
        on_event=events.put,
    )
    try:
        receiver.start()
    except OSError as exc:
        raise CLIError("listen_failed", f"cannot listen on {args.host}:{args.port}: {exc}", EXIT_ERROR) from exc
    local_url = f"http://{args.host}:{args.port}{path}"
    tunnel: CloudflaredManager | None = None
    public_url = None
    try:
        if args.tunnel:
            tunnel = CloudflaredManager(f"http://{args.host}:{args.port}")
            try:
                public_url = tunnel.start().public_url + path
            except CloudflaredError as exc:
                raise CLIError(
                    "tunnel_failed",
                    str(exc),
                    EXIT_ERROR,
                    "Install cloudflared (https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/) or drop --tunnel.",
                ) from exc
        ready = {"type": "ready", "local_url": local_url, "public_url": public_url, "signature_required": not args.allow_unsigned}
        if ctx.out.json:
            ctx.out.write_json(ready)
        else:
            ctx.out.stdout.write(f"Listening on {local_url}\n")
            if public_url:
                ctx.out.stdout.write(
                    f"Public URL {public_url}\n"
                    f"Point a sandbox listener at it: {PROG} watcher listeners update LISTENER_ID --webhook-url {public_url} --yes\n"
                )
            ctx.out.stdout.write("Press Ctrl-C to stop.\n")
            ctx.out.stdout.flush()

        received = 0
        deadline = time.monotonic() + args.listen_timeout if args.listen_timeout else None
        while args.count is None or received < args.count:
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                if args.count is not None:
                    raise CLIError(
                        "timeout",
                        f"received {received} of {args.count} deliveries before the timeout",
                        EXIT_TEMPORARY,
                        details={"received": received},
                    )
                break
            try:
                event = events.get(timeout=min(remaining, 0.5) if remaining is not None else 0.5)
            except queue.Empty:
                continue
            received += 1
            _emit_delivery(ctx, event)
    finally:
        if tunnel is not None:
            tunnel.stop()
        receiver.stop()


def _emit_delivery(ctx: Context, event: ReceivedEvent) -> None:
    parsed = event.webhook_event
    kind = parsed.kind.value if parsed and parsed.kind else event.body.get("kind")
    if kind is None:
        kind = "error" if event.body.get("error") else "sandbox"
    record = {
        "type": "delivery",
        "signature_verified": event.signature_verified,
        "kind": kind,
        "event_id": parsed.event_id if parsed else event.body.get("event_id"),
        "listener_id": parsed.listener_id if parsed else event.body.get("listener_id"),
        "valid_payload": parsed is not None,
        "body": event.body,
    }
    if ctx.out.json:
        ctx.out.write_json(record)
        return
    notam = event.body.get("notam") or {}
    verified = "verified" if event.signature_verified else "UNSIGNED"
    change = (event.body.get("change") or {}).get("changed_notam_id")
    ctx.out.stdout.write(
        f"{format_short(now_utc())}  {record['kind']}  {notam.get('notam_number', '?')}  {notam.get('location', '')}  "
        f"[{verified}]" + (f"  changes {change}" if change else "")
        + (f"  error: {event.body['error']}" if event.body.get("error") else "") + "\n"
    )
    ctx.out.stdout.flush()


# Auth ---------------------------------------------------------------------


ACCOUNT_CONNECTIONS_URL = "https://notamify.com/account#connections"


def _token_source() -> tuple[str, str | None]:
    for name in ("NOTAMIFY_TOKEN", "NOTAMIFY_WATCHER_TOKEN"):
        value = os.getenv(name, "").strip()
        if value:
            return value, f"env:{name}"
    store = ConfigStore()
    token = store.load().token if store.path.exists() else ""
    return token, ("config" if token else None)


def _read_raw_config(store: ConfigStore) -> dict[str, Any]:
    if not store.path.exists():
        return {}
    raw = json.loads(store.path.read_text(encoding="utf-8"))
    return raw if isinstance(raw, dict) else {}


def _save_api_key(ctx: Context) -> None:
    if ctx.args.token_stdin:
        token = sys.stdin.read().strip()
    elif interactive():
        ctx.out.stderr.write(f"Create an API key at {API_KEYS_URL} (Notamify Pro or an API plan).\n")
        token = getpass.getpass("API key: ").strip()
    else:
        raise CLIError(
            "invalid_arguments",
            "no terminal to prompt for the key",
            EXIT_USAGE,
            f'Pipe it in: printf %s "$KEY" | {PROG} auth login --token-stdin, or set NOTAMIFY_TOKEN.',
        )
    if not token or any(ch.isspace() for ch in token):
        raise CLIError("invalid_arguments", "the API key is empty or contains whitespace", EXIT_USAGE)
    store = ConfigStore()
    raw = _read_raw_config(store)
    cfg = SDKConfig(
        api_base_url=str(raw.get("api_base_url") or SDKConfig().api_base_url),
        watcher_base_url=str(raw.get("watcher_base_url") or raw.get("base_url") or SDKConfig().watcher_base_url),
        token=token,
    )
    store.save(cfg)
    env_token = os.getenv("NOTAMIFY_TOKEN", "").strip()
    if env_token and env_token != token:
        ctx.out.note("Note: NOTAMIFY_TOKEN is set and takes precedence over the saved key.")
    ctx.out.emit(
        {"saved": True, "method": "api_key", "config_path": str(store.path), "token": mask_secret(token)},
        lambda d: f"Saved API key {d['token']} to {d['config_path']}.",
    )


def cmd_auth_login(ctx: Context) -> None:
    args = ctx.args
    if args.token_stdin or args.api_key:
        _save_api_key(ctx)
        return
    auth = AccountAuth()

    def show(url: str) -> None:
        if ctx.out.json:
            ctx.out.stderr.write(
                json.dumps(
                    {
                        "event": "sign_in",
                        "url": url,
                        "browser_opened": not args.no_browser,
                        "waiting_seconds": int(args.login_timeout),
                        "message": "Open the URL in a browser to approve the Notamify CLI.",
                    }
                )
                + "\n"
            )
            ctx.out.stderr.flush()
            return
        ctx.out.stderr.write(
            "Sign in to Notamify in your browser to connect the CLI:\n"
            f"  {url}\n"
            + ("" if args.no_browser else "If no browser opened, open the link above.\n")
            + f"Waiting up to {int(args.login_timeout)} seconds…\n"
        )
        ctx.out.stderr.flush()

    try:
        session = auth.login(open_browser=not args.no_browser, port=args.port, timeout=args.login_timeout, on_url=show)
    except AccountError as exc:
        raise error_from_account(exc) from exc

    # A free call that proves the session works end to end.
    tools: int | None = None
    verify_error = None
    try:
        tools = len(McpClient(auth, timeout=30, client_version=SDK_VERSION).list_tools())
    except AccountError as exc:
        verify_error = exc.message
    missing = [scope for scope in SCOPES if scope not in session.scopes]
    if _env_token():
        ctx.out.note("Note: NOTAMIFY_TOKEN is set, so commands use the API key unless you pass --auth account.")
    data = {
        "signed_in": True,
        "method": "account",
        "scopes": session.scopes,
        "missing_scopes": missing,
        "tools_available": tools,
        "verified": tools is not None,
        "verify_error": verify_error,
        "session_path": str(auth.store.path),
        "manage_url": ACCOUNT_CONNECTIONS_URL,
    }

    def render(d: Mapping[str, Any]) -> str:
        lines = ["Signed in to Notamify."]
        if d["verified"]:
            lines.append(f"Connection verified: {d['tools_available']} tools available.")
        else:
            lines.append(f"Signed in, but the check call failed: {d['verify_error']}")
        if d["missing_scopes"]:
            lines.append("Briefing generation was not approved; run `notamify auth login` again to allow it.")
        lines.append(f"Manage or revoke this connection at {ACCOUNT_CONNECTIONS_URL}.")
        return "\n".join(lines)

    ctx.out.emit(data, render)


def cmd_auth_status(ctx: Context) -> None:
    token, source = _token_source()
    session = AccountStore().load()
    try:
        client_cfg = ConfigStore().load()
    except ValueError:
        client_cfg = SDKConfig()
    if _env_token():
        notams_auth = "api_key"
    elif session is not None:
        notams_auth = "account"
    elif token:
        notams_auth = "api_key"
    else:
        notams_auth = None
    account = None
    if session is not None:
        account = {
            "signed_in": True,
            "scopes": session.scopes,
            "access_token_expires_at": format_iso(datetime.fromtimestamp(session.expires_at, timezone.utc)),
            "signed_in_at": format_iso(datetime.fromtimestamp(session.signed_in_at, timezone.utc)),
            "session_path": str(AccountStore().path),
        }
    data = {
        "authenticated": bool(token or session),
        "notams_and_briefings": notams_auth,
        "watcher": "api_key" if token else None,
        "account": account,
        "api_key": {"source": source, "token": mask_secret(token)} if token else None,
        "config_path": str(ConfigStore().path),
        "api_base_url": client_cfg.api_base_url,
        "watcher_base_url": client_cfg.watcher_base_url,
        "mcp_url": AccountAuth().mcp_url,
        "verified": False,
    }

    def render(d: Mapping[str, Any]) -> str:
        lines = []
        if d["account"]:
            lines.append(f"Account: signed in ({', '.join(d['account']['scopes'])}).")
        else:
            lines.append(f"Account: not signed in. Run `{PROG} auth login`.")
        if d["api_key"]:
            lines.append(f"API key: {d['api_key']['token']} from {d['api_key']['source']}.")
        else:
            lines.append(f"API key: none. Run `{PROG} auth login --api-key` or set NOTAMIFY_TOKEN.")
        names = {"account": "your account", "api_key": "the API key", None: "nothing (sign in first)"}
        lines.append(f"NOTAMs and briefings use {names[d['notams_and_briefings']]}; Watcher uses {names[d['watcher']]}.")
        lines.append("Not verified against the server; data requests spend credits.")
        return "\n".join(lines)

    ctx.out.emit(data, render)
    if not data["authenticated"]:
        raise SilentExit(EXIT_AUTH)


def cmd_auth_logout(ctx: Context) -> None:
    args = ctx.args
    both = not args.account and not args.api_key
    signed_out = False
    if both or args.account:
        signed_out = AccountAuth().logout()
    removed_key = False
    store = ConfigStore()
    if (both or args.api_key) and store.path.exists():
        raw = _read_raw_config(store)
        removed_key = bool(raw.get("token"))
        if removed_key:
            store.save(
                SDKConfig(
                    api_base_url=str(raw.get("api_base_url") or SDKConfig().api_base_url),
                    watcher_base_url=str(raw.get("watcher_base_url") or raw.get("base_url") or SDKConfig().watcher_base_url),
                    token="",
                )
            )
    if os.getenv("NOTAMIFY_TOKEN", "").strip():
        ctx.out.note("NOTAMIFY_TOKEN is still set in this environment.")
    data = {"account_signed_out": signed_out, "api_key_removed": removed_key, "config_path": str(store.path)}

    def render(d: Mapping[str, Any]) -> str:
        lines = []
        if both or args.account:
            lines.append(
                f"Signed out of your Notamify account (connection revoked; it is listed at {ACCOUNT_CONNECTIONS_URL})."
                if signed_out
                else "Not signed in to an account."
            )
        if both or args.api_key:
            lines.append("Removed the stored API key." if removed_key else "No stored API key.")
        return "\n".join(lines)

    ctx.out.emit(data, render)


# Meta ---------------------------------------------------------------------


class SilentExit(Exception):
    def __init__(self, code: int) -> None:
        super().__init__(code)
        self.code = code


def _iter_commands(parser: argparse.ArgumentParser, path: list[str]) -> Iterable[tuple[list[str], argparse.ArgumentParser]]:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, child in action.choices.items():
                yield from _iter_commands(child, [*path, name])
    if parser.get_default("handler") is not None:
        yield path, parser


def _describe_argument(action: argparse.Action) -> dict[str, Any] | None:
    if isinstance(action, (argparse._HelpAction, argparse._VersionAction)):
        return None
    entry: dict[str, Any] = {
        "name": action.option_strings[0] if action.option_strings else action.dest,
        "help": action.help,
    }
    if len(action.option_strings) > 1:
        entry["aliases"] = action.option_strings[1:]
    if not action.option_strings:
        entry["positional"] = True
        entry["required"] = action.nargs not in ("*", "?")
    elif action.required:
        entry["required"] = True
    if isinstance(action, argparse._AppendAction) or action.nargs in ("*", "+"):
        entry["repeatable"] = True
    if action.nargs == 0 or isinstance(action, (argparse._StoreTrueAction, argparse._StoreConstAction)):
        entry["flag"] = True
    if action.choices:
        entry["choices"] = list(action.choices)
    if action.metavar and action.option_strings:
        entry["value"] = action.metavar
    if action.default not in (None, argparse.SUPPRESS, False) and not isinstance(action.default, list):
        entry["default"] = action.default
    return entry


def describe_commands(parser: argparse.ArgumentParser) -> dict[str, Any]:
    commands = []
    for path, sub in _iter_commands(parser, []):
        meta: CommandMeta = sub.get_default("meta")
        commands.append(
            _drop_none(
                {
                    "command": " ".join(path),
                    "summary": meta.summary,
                    "credits": meta.credits,
                    "needs_yes": meta.confirmation,
                    "examples": meta.examples or None,
                    "arguments": [a for a in (_describe_argument(x) for x in sub._actions) if a],
                }
            )
        )
    return {
        "name": PROG,
        "version": SDK_VERSION,
        "output": "Text on a terminal, JSON when stdout is not a terminal; force with --json, --output or NOTAMIFY_OUTPUT.",
        "errors": (
            'JSON mode writes JSON lines to stderr; on failure the last line is {"error": {"code", "message", '
            '"exit_code", "remediation", ...}}. `auth login` first writes {"event": "sign_in", "url": ...}.'
        ),
        "confirmation": (
            "Commands that may spend more than one credit or change Watcher state exit 4 unless --yes is given. "
            "The error has credits_estimate and confirm_command; ask the user, then run confirm_command."
        ),
        "pagination": "List results report complete and next_page. complete=false means more NOTAMs exist.",
        "authentication": (
            "Two ways: `notamify auth login` signs in with a Notamify account in the browser (connected-app pricing; "
            "NOTAMs and briefings), or an API key via NOTAMIFY_TOKEN / `auth login --api-key` (all commands, including "
            "Watcher and `notams raw`). --auth auto prefers NOTAMIFY_TOKEN, then the account, then a stored key. "
            "Results report auth, and credits_charged when the server reports the charge."
        ),
        "environment": {
            "NOTAMIFY_TOKEN": "API key; takes precedence over the config file",
            "NOTAMIFY_CONFIG_FILE": "config file path (default ~/.config/notamify/config.json)",
            "NOTAMIFY_API_BASE_URL": "API base URL",
            "NOTAMIFY_WATCHER_BASE_URL": "Watcher API base URL",
            "NOTAMIFY_WEBHOOK_SECRET": "secret for `webhook listen` signature checks",
            "NOTAMIFY_OUTPUT": "text or json",
            "NOTAMIFY_AUTH": "auto, account or api-key (same as --auth)",
            "NOTAMIFY_ACCOUNT_FILE": "account session file (default next to the config file, account.json)",
            "NOTAMIFY_MCP_URL": "Notamify connected-app server for account sign-in (default https://mcp.notamify.com/mcp)",
        },
        "exit_codes": {str(code): text for code, text in EXIT_CODES.items()},
        "commands": commands,
    }


def cmd_commands(ctx: Context) -> None:
    manifest = describe_commands(build_parser())

    def render(data: Mapping[str, Any]) -> str:
        width = max(len(c["command"]) for c in data["commands"])
        lines = [f"{PROG} {data['version']}", ""]
        for c in data["commands"]:
            cost = f"  [{c['credits']}]" if c.get("credits") else ""
            lines.append(f"  {c['command'].ljust(width)}  {c['summary']}{cost}")
        lines += ["", "Exit codes:"] + [f"  {k}  {v}" for k, v in data["exit_codes"].items()]
        lines += ["", f"Details: {PROG} COMMAND --help, or {PROG} commands --json"]
        return "\n".join(lines)

    ctx.out.emit(manifest, render)


def skill_text() -> str:
    return resources.files("notamify_sdk.cli").joinpath("SKILL.md").read_text(encoding="utf-8")


def cmd_skill_show(ctx: Context) -> None:
    text = skill_text()
    if ctx.out.json:
        ctx.out.write_json({"name": "notamify", "content": text})
    else:
        ctx.out.stdout.write(text)


def cmd_skill_install(ctx: Context) -> None:
    target = Path(ctx.args.dir).expanduser() / "notamify" / "SKILL.md"
    if target.exists() and not ctx.args.force:
        if target.read_text(encoding="utf-8") == skill_text():
            ctx.out.emit({"installed": True, "path": str(target), "changed": False}, lambda d: f"Up to date: {target}")
            return
        raise CLIError("exists", f"{target} exists and differs", EXIT_USAGE, "Pass --force to overwrite it.")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(skill_text(), encoding="utf-8")
    ctx.out.emit({"installed": True, "path": str(target), "changed": True}, lambda d: f"Installed {target}")


# Entry point --------------------------------------------------------------


TIME_OPTIONS = {"--start", "--end", "--after", "--before"}
_NEGATIVE_OFFSET = re.compile(r"^-\d+(?:\.\d+)?[mhd]$", re.I)


def _join_negative_times(argv: list[str]) -> list[str]:
    """Let `--after -3d` work: argparse would read -3d as an option."""
    joined: list[str] = []
    skip = False
    for index, arg in enumerate(argv):
        if skip:
            skip = False
            continue
        nxt = argv[index + 1] if index + 1 < len(argv) else None
        if arg in TIME_OPTIONS and nxt is not None and _NEGATIVE_OFFSET.match(nxt):
            joined.append(f"{arg}={nxt}")
            skip = True
        else:
            joined.append(arg)
    return joined


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    out = Output(detect_mode(argv))
    parser = build_parser()
    try:
        args = parser.parse_args(_join_negative_times(argv))
        out.mode = getattr(args, "output", None) or out.mode
        ctx = Context(args, out, argv)
        args.handler(ctx)
        return EXIT_OK
    except UsageError as exc:
        out.error(CLIError("usage", str(exc), EXIT_USAGE, f"Run `{exc.prog} --help`."))
        return EXIT_USAGE
    except CLIError as exc:
        out.error(exc)
        return exc.exit_code
    except SilentExit as exc:
        return exc.code
    except KeyboardInterrupt:
        return EXIT_INTERRUPTED
    except BrokenPipeError:
        try:
            sys.stdout.close()
        except OSError:
            pass
        return EXIT_OK
    except json.JSONDecodeError as exc:
        out.error(
            CLIError(
                "invalid_config",
                f"cannot read the config file {ConfigStore().path}: {exc}",
                EXIT_ERROR,
                f"Fix or delete it, then run `{PROG} auth login`.",
            )
        )
        return EXIT_ERROR
    except Exception as exc:  # keep agents on the error envelope instead of a traceback
        out.error(CLIError("internal_error", f"{type(exc).__name__}: {exc}", EXIT_ERROR, "Please report this bug."))
        return EXIT_ERROR
