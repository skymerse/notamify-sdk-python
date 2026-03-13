from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from notamify_sdk.config import ConfigStore as _ConfigStore
from notamify_sdk.config import SDKConfig as _SDKConfig

_DEFAULT_BASE_URL = "https://watcher.notamify.com"
_DEFAULT_CONFIG_PATH = Path.home() / ".config" / "notamify-watcher" / "config.json"
_NEW_DEFAULT_CONFIG_PATH = Path.home() / ".config" / "notamify" / "config.json"


@dataclass
class SDKConfig:
    base_url: str = _DEFAULT_BASE_URL
    token: str = ""

    def to_sdk_config(self) -> _SDKConfig:
        return _SDKConfig(watcher_base_url=self.base_url, token=self.token)

    @classmethod
    def from_sdk_config(cls, cfg: _SDKConfig) -> "SDKConfig":
        return cls(base_url=cfg.watcher_base_url, token=cfg.token)


class ConfigStore:
    def __init__(self, path: Path | None = None) -> None:
        self._store = _ConfigStore(path or self.default_path())

    @property
    def path(self) -> Path:
        return self._store.path

    @staticmethod
    def default_path() -> Path:
        legacy_custom = os.getenv("NOTAMIFY_WATCHER_CONFIG_FILE", "").strip()
        if legacy_custom:
            return Path(legacy_custom)
        custom = os.getenv("NOTAMIFY_CONFIG_FILE", "").strip()
        if custom:
            return Path(custom)
        if _DEFAULT_CONFIG_PATH.exists():
            return _DEFAULT_CONFIG_PATH
        if _NEW_DEFAULT_CONFIG_PATH.exists():
            return _NEW_DEFAULT_CONFIG_PATH
        return _DEFAULT_CONFIG_PATH

    def load(self) -> SDKConfig:
        return SDKConfig.from_sdk_config(self._store.load())

    def save(self, cfg: SDKConfig) -> None:
        api_base_url = _SDKConfig().api_base_url
        if self.path.exists():
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            api_base_url = str(raw.get("api_base_url", api_base_url)).strip() or api_base_url
        self._store.save(_SDKConfig(api_base_url=api_base_url, watcher_base_url=cfg.base_url, token=cfg.token))
