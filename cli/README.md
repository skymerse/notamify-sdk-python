# notamify

**OpenClaw and agents ready.**

The Notamify command line: NOTAMs, affected infrastructure, airport and flight briefings, and NOTAM Watcher webhooks. It is built for people at a terminal and for AI agents (Claude Code, Codex, OpenClaw and others) alike.

## Install

```bash
uv tool install notamify      # or: pipx install notamify
notamify --version
```

Both tools install the `notamify` command into `~/.local/bin`. If your shell says `command not found: notamify`, that directory is not on your `PATH` yet:

```bash
uv tool update-shell          # or: pipx ensurepath
```

Then open a new terminal. To check without changing anything, run `~/.local/bin/notamify --version`, or run it without installing: `uvx notamify --version`.

With plain pip, install into a virtual environment (`pip install notamify`); the command is then in that environment's `bin/` directory and is on `PATH` while the environment is active.

To update: `uv tool upgrade notamify` (or `pipx upgrade notamify`).

## Sign in

Create an API key at [notamify.com/api-manager](https://notamify.com/api-manager) (Notamify Pro or an API plan), then:

```bash
notamify auth login           # prompts for the key; saved to ~/.config/notamify/config.json (mode 0600)
notamify auth status
```

`NOTAMIFY_TOKEN` takes precedence over the saved key, which suits CI, containers and agents.

## Use

```bash
notamify notams active EPWA EGLL --start now --hours 24
notamify affected EPWA --affected CLOSED:RUNWAY
notamify notams get NOTAM_ID
notamify briefing airport EPWA
notamify briefing flight create --origin EPWA@now/+2h --destination EGLL@+2h/+3h --aircraft B738 --wait
notamify watcher listeners list
notamify webhook listen --tunnel
notamify commands             # every command, its credit cost and the exit codes
```

On a terminal you get readable text. When the output is piped, as it is for agents, you get JSON; force either with `--json` or `--output text`.

Commands that may spend more than one credit (`--all`, briefings, prioritize) or change Watcher listeners ask for confirmation. Without a terminal they exit with code 4 and print the exact command to rerun with `--yes`. `--dry-run` shows the request and its estimated cost without sending it.

## For AI agents

```bash
notamify skill install                       # ~/.claude/skills/notamify/SKILL.md
notamify skill install --dir ~/.codex/skills # or any other skills directory
```

The skill tells an agent which command to use, how to read pagination and exit codes, and how to ask before spending credits. Chat apps without a shell (ChatGPT, Claude web and desktop) should use the hosted Notamify MCP server at `https://mcp.notamify.com/mcp` instead.

NOTAM interpretations and briefings are informational. Verify official aviation sources before making operational decisions.

The CLI is part of [notamify-sdk](https://github.com/skymerse/notamify-sdk-python); `python -m notamify_sdk.cli` runs the same command from the SDK alone.
