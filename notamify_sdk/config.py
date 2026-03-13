from __future__ import annotations

import json
import os
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

    def save(self, cfg: SDKConfig) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "api_base_url": cfg.api_base_url.strip(),
            "watcher_base_url": cfg.watcher_base_url.strip(),
            "base_url": cfg.watcher_base_url.strip(),
            "token": cfg.token.strip(),
        }
        self.path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
