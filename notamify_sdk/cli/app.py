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
import shlex
import sys
import time
import uuid as uuidlib
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from pydantic import ValidationError

from ..client import SDK_VERSION, APIError, NotamifyClient
from ..cloudflared import CloudflaredError, CloudflaredManager
from ..config import ConfigStore, SDKConfig
from ..receiver import ReceivedEvent, ReceiverConfig, WebhookReceiver
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


def _add_notam_filters(parser: argparse.ArgumentParser, *, qcode: bool = True, classification: bool = True) -> None:
    group = parser.add_argument_group("filters")
    group.add_argument("--id", dest="notam_ids", action="append", metavar="NOTAM_ID", help="limit to Notamify NOTAM IDs")
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
        help="affected element filter, e.g. CLOSED:RUNWAY, CLOSED, :TAXIWAY",
    )
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
    location_help = "ICAO@START/END, e.g. EPWA@2026-11-09T08:00Z/2026-11-09T10:00Z or EPWA@now/+2h"
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
    auth = _add_group(sub, "auth", "Store, inspect or remove the API key")
    p = _add_command(
        auth,
        "login",
        cmd_auth_login,
        CommandMeta(
            "Save an API key to the config file",
            network=False,
            examples=[f"{PROG} auth login", f'printf %s "$KEY" | {PROG} auth login --token-stdin'],
        ),
        description=f"Save an API key to the config file (mode 0600). Create a key at {API_KEYS_URL}.",
    )
    p.add_argument("--token-stdin", action="store_true", help="read the key from stdin instead of prompting")
    _add_command(
        auth,
        "status",
        cmd_auth_status,
        CommandMeta("Show where the API key comes from; exits 3 when none is set", network=False),
    )
    _add_command(auth, "logout", cmd_auth_logout, CommandMeta("Remove the stored API key", network=False))

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
        _add_notam_filters(p)
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
                f"{PROG} briefing flight create --origin EPWA@now/+2h --destination EGLL@+2h/+5h --aircraft B738 --yes --wait",
                f"{PROG} briefing flight create --body request.json --yes",
            ],
        ),
    )
    _add_flight(p)
    p.add_argument("--wait", action="store_true", help="poll until the job completes or fails")
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
            examples=[f"{PROG} prioritize NOTAM_ID --origin EPWA@now/+2h --destination EGLL@+2h/+5h --aircraft B738 --yes"],
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

    @property
    def yes(self) -> bool:
        return bool(getattr(self.args, "yes", False))

    @property
    def dry_run(self) -> bool:
        return bool(getattr(self.args, "dry_run", False))

    @property
    def client(self) -> NotamifyClient:
        if self._client is None:
            timeout = getattr(self.args, "timeout", None)
            kwargs: dict[str, Any] = {"user_agent": f"notamify-cli/{SDK_VERSION}"}
            if timeout:
                kwargs["timeout"] = timeout
            self._client = NotamifyClient(**kwargs)
        return self._client

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

    def plan(self, method: str, base: str, path: str, *, query: Mapping[str, Any] | None = None, body: Any = None, credits: str | None = None) -> bool:
        """Print the request instead of sending it under --dry-run. Returns True when it did."""
        if not self.dry_run:
            return False
        client = self.client
        url = (client.api_base_url if base == "api" else client.watcher_base_url) + path
        encoded = client._encode_query({k: v for k, v in (query or {}).items() if v is not None}) if query else ""
        request: dict[str, Any] = {"method": method, "url": f"{url}?{encoded}" if encoded else url}
        if body is not None:
            request["body"] = to_jsonable(body)
        self.out.emit(
            {"dry_run": True, "request": request, "credits_estimate": credits, "authenticated": bool(client.token)},
            lambda d: f"{d['request']['method']} {d['request']['url']}"
            + (f"\n{json.dumps(d['request']['body'], indent=2)}" if "body" in d["request"] else "")
            + f"\ncredits: {credits or 'free'} (dry run, nothing sent)",
        )
        return True


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
        raise CLIError("invalid_arguments", _validation_message(exc), EXIT_USAGE) from exc


def _validation_message(exc: ValidationError) -> str:
    parts = []
    for item in exc.errors()[:5]:
        location = ".".join(str(p) for p in item.get("loc", ()))
        parts.append(f"{location}: {item.get('msg')}" if location else str(item.get("msg")))
    return "; ".join(parts) or str(exc)


def _envelope(extra: Mapping[str, Any]) -> dict[str, Any]:
    return {**extra, "source": "Notamify", "retrieved_at": format_iso(now_utc()), "disclaimer": DISCLAIMER}


# NOTAM lists ---------------------------------------------------------------


def _list_notams(
    ctx: Context,
    *,
    path: str,
    fetch: Callable[[dict[str, Any]], Any],
    query: dict[str, Any],
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
    max_pages = args.max_pages if args.all else 1
    credits = f"up to {max_pages} credits (1 per page)" if max_pages > 1 else "1 credit"
    first = {**query, "page": args.page, "per_page": args.per_page}
    if ctx.plan("GET", "api", path, query=first, credits=credits):
        return
    if max_pages > 1:
        ctx.confirm(f"Fetching up to {max_pages} pages", credits)

    notams: list[dict[str, Any]] = []
    page = args.page
    pages_fetched = 0
    total = 0
    complete = False
    while pages_fetched < max_pages:
        result = _call(lambda: fetch({**query, "page": page, "per_page": args.per_page}))
        pages_fetched += 1
        total = result.total_count
        batch = [n.model_dump(mode="json", by_alias=True) for n in result.notams]
        notams.extend(batch)
        seen = (page - 1) * args.per_page + len(batch)
        if not batch or seen >= total or len(batch) < args.per_page:
            complete = True
            break
        page += 1

    fields = [f.strip() for f in (args.fields or "").split(",") if f.strip()]
    body: dict[str, Any] = {
        "total_count": total,
        "page": args.page,
        "per_page": args.per_page,
        "pages_fetched": pages_fetched,
        "complete": complete,
        # On an incomplete result, page already points past the last fetched page.
        "next_page": None if complete else page,
        "credits_estimate": pages_fetched,
    }
    if transform is not None:
        body = {**transform(notams), **body}
    else:
        body = {"notams": [pick_fields(n, fields) for n in notams] if fields else notams, **body}
    ctx.out.emit(_envelope(body), render or _render_notam_list)


def _render_notam_list(data: Mapping[str, Any]) -> str:
    lines = [_render_notam_line(notam) for notam in data.get("notams", [])]
    lines.append(
        f"\n{len(data.get('notams', []))} of {data.get('total_count', 0)} NOTAMs shown; "
        f"{data.get('pages_fetched', 0)} page(s), ~{data.get('credits_estimate', 0)} credit(s)."
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
    parts = [
        str(notam.get("notam_number", "")),
        str(notam.get("location") or notam.get("icao_code") or ""),
        validity,
        f"[{interpretation.get('category')}/{interpretation.get('subcategory')}]" if interpretation else "",
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
            "category": args.category,
            "subcategory": args.subcategory,
            "qcode": args.qcode,
            "affected_element": _affected(args.affected),
            "excluded_classifications": args.exclude_classification,
            "always_include_est": False if args.no_est else None,
        }
    )


def cmd_notams_active(ctx: Context) -> None:
    query = _active_query(ctx.args, require_location=True)
    _list_notams(ctx, path="/notams", fetch=lambda q: ctx.client.get_active_notams(q), query=query)


def cmd_notams_raw(ctx: Context) -> None:
    query = _active_query(ctx.args, require_location=True)
    _list_notams(ctx, path="/notams/raw", fetch=lambda q: ctx.client.get_raw_notams(q), query=query)


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
    _list_notams(ctx, path="/notams/nearby", fetch=lambda q: ctx.client.get_nearby_notams(q), query=query)


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
    _list_notams(ctx, path="/notams/archive", fetch=lambda q: ctx.client.get_historical_notams(q), query=query)


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
    _list_notams(
        ctx,
        path="/notams/recently-expired",
        fetch=lambda q: ctx.client.get_recently_expired_notams(q),
        query=query,
    )


def cmd_notams_get(ctx: Context) -> None:
    notam_id = ctx.args.notam_id.strip()
    if ctx.plan("GET", "api", f"/notams/{notam_id}", query={"add_popularity": False}, credits="1 credit"):
        return
    notam = _call(lambda: ctx.client.get_notam(notam_id))
    ctx.out.emit(_envelope({"notam": notam}), lambda d: _render_notam_detail(to_jsonable(d["notam"])))


def _render_notam_detail(notam: Mapping[str, Any]) -> str:
    lines = [_render_notam_line(notam), ""]
    lines.append(str(notam.get("icao_message") or notam.get("message") or "").strip())
    interpretation = notam.get("interpretation") or {}
    if interpretation:
        lines += ["", "Interpretation (informational):", f"  {interpretation.get('description', '')}"]
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
            f"~{data['credits_estimate']} credit(s)."
        )
        if not data["complete"]:
            footer += f" Incomplete: more NOTAMs on --page {data['next_page']}, or use --all."
        return table + footer

    _list_notams(
        ctx,
        path="/notams",
        fetch=lambda q: ctx.client.get_active_notams(q),
        query=query,
        transform=transform,
        render=render,
    )


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
    if ctx.plan("GET", "api", "/notams/briefing/simple", query=query, credits="2 credits"):
        return
    ctx.confirm(f"Generating an airport briefing for {query['location']}", "2 credits")
    result = _call(lambda: ctx.client.generate_airport_briefing(query))
    ctx.out.emit(
        _envelope({"briefing": result, "credits_estimate": 2}),
        lambda d: f"{result.summary.strip()}\n\nNOTAMs: {', '.join(result.notam_ids) or '-'}\n\n{DISCLAIMER}",
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
    end = _time(raw_end, end_of_day=True)
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
    if ctx.plan("POST", "api", "/notams/briefing", body=body, credits="5 credits"):
        return
    route = " → ".join(str(loc.get("location")) for loc in body.get("locations", []))
    ctx.confirm(f"Creating a flight briefing for {route}", "5 credits")
    job = _call(lambda: ctx.client.create_async_briefing(body))
    if ctx.args.wait:
        ctx.out.note(f"Briefing job {job.uuid} accepted; waiting…")
        _wait_for_briefing(ctx, job.uuid)
        return
    ctx.out.emit(
        _envelope({"job": job, "credits_estimate": 5, "next": f"{PROG} briefing flight wait {job.uuid}"}),
        lambda d: f"Briefing job {job.uuid} is {job.status.value if job.status else 'pending'}.\nWait for it: {PROG} briefing flight wait {job.uuid}",
    )


def cmd_flight_status(ctx: Context) -> None:
    job_id = _uuid(ctx.args.uuid)
    if ctx.plan("GET", "api", f"/notams/briefing/{job_id}", credits="free"):
        return
    status = _call(lambda: ctx.client.get_async_briefing_status(job_id))
    ctx.out.emit(_envelope({"job": status}), _render_briefing_status)


def cmd_flight_wait(ctx: Context) -> None:
    job_id = _uuid(ctx.args.uuid)
    if ctx.plan("GET", "api", f"/notams/briefing/{job_id}", credits="free"):
        return
    _wait_for_briefing(ctx, job_id)


def _uuid(value: str) -> str:
    try:
        return str(uuidlib.UUID(value.strip()))
    except ValueError as exc:
        raise CLIError("invalid_arguments", f"invalid briefing UUID {value!r}", EXIT_USAGE) from exc


def _wait_for_briefing(ctx: Context, job_id: str) -> None:
    args = ctx.args
    deadline = time.monotonic() + max(args.wait_timeout, 0)
    interval = max(args.interval, 0.5)
    while True:
        status = _call(lambda: ctx.client.get_async_briefing_status(job_id))
        state = status.status.value
        if state == "completed":
            ctx.out.emit(_envelope({"job": status}), _render_briefing_status)
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
                f"Briefing job {job_id} is still {state}.",
                EXIT_TEMPORARY,
                f"Polling is free: rerun `{PROG} briefing flight wait {job_id}`.",
                details={"uuid": job_id, "status": state},
            )
        time.sleep(interval)


def _render_briefing_status(data: Mapping[str, Any]) -> str:
    job = to_jsonable(data["job"])
    lines = [f"Briefing {job['uuid']}: {job['status']} (updated {format_short(job.get('updated_at'))})"]
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
    if ctx.plan("POST", "api", "/notams/prioritisation", body=body, credits="5 credits"):
        return
    ctx.confirm(f"Prioritizing NOTAM {body['notam_id']} for this flight", "5 credits")
    result = _call(lambda: ctx.client.prioritize_notam(body))

    def render(data: Mapping[str, Any]) -> str:
        lines = []
        for item in to_jsonable(data["result"])["notams"]:
            notam = item["notam"]
            lines.append(f"{item['priority']}  {notam.get('notam_number')}  {notam.get('location')}")
            lines.append(f"  {item.get('explanation', '')}")
            lines += [f"  • {c}" for c in item.get("considerations") or []]
        return "\n".join(lines + ["", DISCLAIMER])

    ctx.out.emit(_envelope({"result": result, "credits_estimate": 5}), render)


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
    record = {
        "type": "delivery",
        "signature_verified": event.signature_verified,
        "kind": parsed.kind.value if parsed else event.body.get("kind"),
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
        f"[{verified}]" + (f"  changes {change}" if change else "") + "\n"
    )
    ctx.out.stdout.flush()


# Auth ---------------------------------------------------------------------


def _token_source() -> tuple[str, str | None]:
    for name in ("NOTAMIFY_TOKEN", "NOTAMIFY_WATCHER_TOKEN"):
        value = os.getenv(name, "").strip()
        if value:
            return value, f"env:{name}"
    store = ConfigStore()
    token = store.load().token if store.path.exists() else ""
    return token, ("config" if token else None)


def cmd_auth_login(ctx: Context) -> None:
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
    cfg = store.load() if store.path.exists() else SDKConfig()
    raw: dict[str, Any] = {}
    if store.path.exists():
        try:
            raw = json.loads(store.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raw = {}
    cfg.api_base_url = str(raw.get("api_base_url") or SDKConfig().api_base_url)
    cfg.watcher_base_url = str(raw.get("watcher_base_url") or raw.get("base_url") or SDKConfig().watcher_base_url)
    cfg.token = token
    store.save(cfg)
    env_token = os.getenv("NOTAMIFY_TOKEN", "").strip()
    if env_token and env_token != token:
        ctx.out.note("Note: NOTAMIFY_TOKEN is set and takes precedence over the saved key.")
    ctx.out.emit(
        {"saved": True, "config_path": str(store.path), "token": mask_secret(token)},
        lambda d: f"Saved API key {d['token']} to {d['config_path']}.",
    )


def cmd_auth_status(ctx: Context) -> None:
    token, source = _token_source()
    client_cfg = ConfigStore().load()
    data = {
        "authenticated": bool(token),
        "source": source,
        "token": mask_secret(token) if token else None,
        "config_path": str(ConfigStore().path),
        "api_base_url": client_cfg.api_base_url,
        "watcher_base_url": client_cfg.watcher_base_url,
        "verified": False,
    }
    ctx.out.emit(
        data,
        lambda d: (
            f"API key {d['token']} from {d['source']} (not verified; checking would spend a credit)."
            if d["authenticated"]
            else f"No API key. Set NOTAMIFY_TOKEN or run `{PROG} auth login`."
        )
        + f"\nAPI {d['api_base_url']}\nWatcher {d['watcher_base_url']}",
    )
    if not token:
        raise SilentExit(EXIT_AUTH)


def cmd_auth_logout(ctx: Context) -> None:
    store = ConfigStore()
    removed = False
    if store.path.exists():
        cfg = store.load()
        raw = json.loads(store.path.read_text(encoding="utf-8"))
        removed = bool(raw.get("token"))
        cfg.token = ""
        cfg.api_base_url = str(raw.get("api_base_url") or SDKConfig().api_base_url)
        cfg.watcher_base_url = str(raw.get("watcher_base_url") or raw.get("base_url") or SDKConfig().watcher_base_url)
        store.save(cfg)
    if os.getenv("NOTAMIFY_TOKEN", "").strip():
        ctx.out.note("NOTAMIFY_TOKEN is still set in this environment.")
    ctx.out.emit(
        {"removed": removed, "config_path": str(store.path)},
        lambda d: "Removed the stored API key." if removed else "No stored API key.",
    )


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
        "errors": 'JSON mode writes {"error": {"code", "message", "exit_code", "remediation", ...}} to stderr.',
        "confirmation": (
            "Commands that may spend more than one credit or change Watcher state exit 4 unless --yes is given. "
            "The error has credits_estimate and confirm_command; ask the user, then run confirm_command."
        ),
        "pagination": "List results report complete and next_page. complete=false means more NOTAMs exist.",
        "environment": {
            "NOTAMIFY_TOKEN": "API key; takes precedence over the config file",
            "NOTAMIFY_CONFIG_FILE": "config file path (default ~/.config/notamify/config.json)",
            "NOTAMIFY_API_BASE_URL": "API base URL",
            "NOTAMIFY_WATCHER_BASE_URL": "Watcher API base URL",
            "NOTAMIFY_WEBHOOK_SECRET": "secret for `webhook listen` signature checks",
            "NOTAMIFY_OUTPUT": "text or json",
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


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    out = Output(detect_mode(argv))
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
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
