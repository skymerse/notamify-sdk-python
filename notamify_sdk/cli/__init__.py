"""The ``notamify`` command line interface. Install it with ``uv tool install notamify``."""

from .app import build_parser, describe_commands, main

__all__ = ["build_parser", "describe_commands", "main"]
