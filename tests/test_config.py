import json
import os
import tempfile
import unittest
from pathlib import Path

from notamify_sdk.config import ConfigStore, SDKConfig


class ConfigTests(unittest.TestCase):
    def test_load_save_and_env_override(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "config.json"
            store = ConfigStore(path)
            store.save(
                SDKConfig(
                    api_base_url="https://api.local/v2",
                    watcher_base_url="https://watcher.local",
                    token="x",
                )
            )
            cfg = store.load()
            self.assertEqual(cfg.api_base_url, "https://api.local/v2")
            self.assertEqual(cfg.watcher_base_url, "https://watcher.local")
            self.assertEqual(cfg.token, "x")

            os.environ["NOTAMIFY_API_BASE_URL"] = "https://env-api"
            os.environ["NOTAMIFY_WATCHER_BASE_URL"] = "https://env-watcher"
            os.environ["NOTAMIFY_TOKEN"] = "env-token"
            try:
                cfg = store.load()
            finally:
                del os.environ["NOTAMIFY_API_BASE_URL"]
                del os.environ["NOTAMIFY_WATCHER_BASE_URL"]
                del os.environ["NOTAMIFY_TOKEN"]
            self.assertEqual(cfg.api_base_url, "https://env-api")
            self.assertEqual(cfg.watcher_base_url, "https://env-watcher")
            self.assertEqual(cfg.token, "env-token")

            saved = json.loads(path.read_text())
            self.assertEqual(saved["api_base_url"], "https://api.local/v2")
            self.assertEqual(saved["watcher_base_url"], "https://watcher.local")
            self.assertEqual(saved["base_url"], "https://watcher.local")

    def test_load_legacy_config_and_token_env(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "legacy-config.json"
            path.write_text(json.dumps({"base_url": "https://legacy-watcher.local", "token": "legacy-file-token"}))
            store = ConfigStore(path)

            os.environ["NOTAMIFY_WATCHER_TOKEN"] = "legacy-env-token"
            try:
                cfg = store.load()
            finally:
                del os.environ["NOTAMIFY_WATCHER_TOKEN"]

            self.assertEqual(cfg.watcher_base_url, "https://legacy-watcher.local")
            self.assertEqual(cfg.base_url, "https://legacy-watcher.local")
            self.assertEqual(cfg.token, "legacy-env-token")

    def test_default_path_honors_legacy_env_var(self):
        with tempfile.TemporaryDirectory() as td:
            legacy_path = Path(td) / "watcher.json"
            os.environ["NOTAMIFY_WATCHER_CONFIG_FILE"] = str(legacy_path)
            try:
                self.assertEqual(ConfigStore.default_path(), legacy_path)
            finally:
                del os.environ["NOTAMIFY_WATCHER_CONFIG_FILE"]


if __name__ == "__main__":
    unittest.main()
