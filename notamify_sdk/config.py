from __future__ import annotations

import json
import os
import secrets
from dataclasses import dataclass
from pathlib import Path

_DEFAULT_API_BASE_URL = "https://api.notamify.com/api/v2"
_DEFAULT_WATCHER_BASE_URL = "https://watcher.notamify.com"
_DEFAULT_CONFIG_PATH = Path.home() / ".config" / "notamify" / "config.json"
_LEGACY_DEFAULT_CONFIG_PATH = Path.home() / ".config" / "notamify-watcher" / "config.json"


@dataclass
class SDKConfig:
    api_base_url: str = _DEFAULT_API_BASE_URL
    watcher_base_url: str = _DEFAULT_WATCHER_BASE_URL
    token: str = ""

    @property
    def base_url(self) -> str:
        return self.watcher_base_url

    @base_url.setter
    def base_url(self, value: str) -> None:
        self.watcher_base_url = value


def write_private_json(path: Path, payload: object) -> None:
    """Write JSON readable only by the current user (0600) from the moment it exists.

    Writes a new temporary file with an unpredictable name (created exclusively, never
    through a symlink) and renames it over ``path``, so a failed write never leaves a
    truncated file and an existing file with looser permissions is replaced.
    """
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    data = (json.dumps(payload, indent=2) + "\n").encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    for _ in range(5):
        tmp = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
        try:
            fd = os.open(tmp, flags, 0o600)
            break
        except FileExistsError:
            continue
    else:
        raise OSError(f"cannot create a temporary file next to {path}")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def apply_environment(cfg: SDKConfig) -> SDKConfig:
    """Override ``cfg`` with NOTAMIFY_* environment variables, which take precedence over the config file."""
    env_api_base = os.getenv("NOTAMIFY_API_BASE_URL", "").strip()
    env_watcher_base = os.getenv("NOTAMIFY_WATCHER_BASE_URL", "").strip()
    env_token = os.getenv("NOTAMIFY_TOKEN", "").strip() or os.getenv("NOTAMIFY_WATCHER_TOKEN", "").strip()
    if env_api_base:
        cfg.api_base_url = env_api_base
    if env_watcher_base:
        cfg.watcher_base_url = env_watcher_base
    if env_token:
        cfg.token = env_token
    return cfg


class ConfigStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or self.default_path()

    @staticmethod
    def default_path() -> Path:
        custom = os.getenv("NOTAMIFY_CONFIG_FILE", "").strip()
        if custom:
            return Path(custom)
        legacy_custom = os.getenv("NOTAMIFY_WATCHER_CONFIG_FILE", "").strip()
        if legacy_custom:
            return Path(legacy_custom)
        if _DEFAULT_CONFIG_PATH.exists():
            return _DEFAULT_CONFIG_PATH
        if _LEGACY_DEFAULT_CONFIG_PATH.exists():
            return _LEGACY_DEFAULT_CONFIG_PATH
        return _DEFAULT_CONFIG_PATH

    def load(self) -> SDKConfig:
        cfg = SDKConfig()
        if self.path.exists():
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            cfg.api_base_url = str(raw.get("api_base_url", cfg.api_base_url)).strip() or cfg.api_base_url
            cfg.watcher_base_url = str(raw.get("watcher_base_url", cfg.watcher_base_url)).strip() or cfg.watcher_base_url
            cfg.watcher_base_url = str(raw.get("base_url", cfg.watcher_base_url)).strip() or cfg.watcher_base_url
            cfg.token = str(raw.get("token", cfg.token)).strip()
        return apply_environment(cfg)

    def save(self, cfg: SDKConfig) -> None:
        """Write the config, readable only by the current user (mode 0600) from the moment it exists.

        The API key is stored in this file, as with other developer CLIs; NOTAMIFY_TOKEN avoids storing it.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        payload = {
            "api_base_url": cfg.api_base_url.strip(),
            "watcher_base_url": cfg.watcher_base_url.strip(),
            "base_url": cfg.watcher_base_url.strip(),
            "token": cfg.token.strip(),
        }
        write_private_json(self.path, payload)
