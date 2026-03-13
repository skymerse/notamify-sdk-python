import importlib.util
import io
import tempfile
import unittest
from pathlib import Path
from urllib.error import HTTPError
from unittest import mock

from notamify_sdk.client import SDK_VERSION
from notamify_sdk.models import Listener
from notamify_watcher_sdk import ConfigStore as LegacyConfigStore
from notamify_watcher_sdk import SDKConfig as LegacySDKConfig
from notamify_watcher_sdk import WatcherClient


def _load_local_service_run_module():
    module_path = Path(__file__).resolve().parents[1] / "examples" / "local_service_run.py"
    spec = importlib.util.spec_from_file_location("local_service_run", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CompatTests(unittest.TestCase):
    def test_watcher_client_uses_legacy_base_url_argument(self):
        client = WatcherClient(token="t", base_url="https://watcher.legacy.local")
        self.assertEqual(client.base_url, "https://watcher.legacy.local")
        self.assertEqual(client.watcher_base_url, "https://watcher.legacy.local")
        self.assertEqual(client.api_base_url, "https://api.notamify.com/api/v2")

    def test_watcher_client_default_user_agent_tracks_sdk_version(self):
        client = WatcherClient(token="t")
        self.assertEqual(client.user_agent, f"notamify-watcher-sdk-python/{SDK_VERSION}")

    def test_legacy_config_store_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "config.json"
            store = LegacyConfigStore(path)
            store.save(LegacySDKConfig(base_url="https://watcher.legacy.local", token="legacy-token"))
            cfg = store.load()
            self.assertEqual(cfg.base_url, "https://watcher.legacy.local")
            self.assertEqual(cfg.token, "legacy-token")

    def test_legacy_config_store_preserves_existing_api_base_url(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "config.json"
            path.write_text(
                '{"api_base_url":"https://api.staging.local/v2","watcher_base_url":"https://watcher.local","token":"x"}'
            )
            store = LegacyConfigStore(path)
            store.save(LegacySDKConfig(base_url="https://watcher.legacy.local", token="legacy-token"))
            saved = path.read_text()
            self.assertIn('"api_base_url": "https://api.staging.local/v2"', saved)
            self.assertIn('"watcher_base_url": "https://watcher.legacy.local"', saved)

    def test_choose_listener_does_not_reuse_prod_listener_by_name(self):
        module = _load_local_service_run_module()
        prod = Listener.model_validate(
            {
                "id": "prod-1",
                "name": "shared-name",
                "webhook_url": "https://prod.example.com/notamify",
                "mode": "prod",
            }
        )
        sandbox = Listener.model_validate(
            {
                "id": "sbx-1",
                "name": "shared-name",
                "webhook_url": "https://demo.trycloudflare.com/notamify",
                "mode": "sandbox",
            }
        )

        chosen = module.choose_listener([prod, sandbox], preferred_listener_id="", preferred_listener_name="shared-name")
        self.assertEqual(chosen.id, "sbx-1")

        chosen_none = module.choose_listener([prod], preferred_listener_id="", preferred_listener_name="shared-name")
        self.assertIsNone(chosen_none)

    def test_choose_listener_rejects_prod_listener_by_id(self):
        module = _load_local_service_run_module()
        prod = Listener.model_validate(
            {
                "id": "prod-1",
                "name": "prod-listener",
                "webhook_url": "https://prod.example.com/notamify",
                "mode": "prod",
            }
        )

        with self.assertRaises(SystemExit):
            module.choose_listener([prod], preferred_listener_id="prod-1", preferred_listener_name="")

    def test_wait_for_tunnel_reachable_accepts_http_error_response(self):
        module = _load_local_service_run_module()
        http_error = HTTPError("https://demo.trycloudflare.com/notamify", 405, "Method Not Allowed", {}, io.BytesIO(b""))

        with mock.patch.object(module.urllib.request, "urlopen", side_effect=http_error), mock.patch.object(
            module.time, "sleep"
        ) as sleep_mock:
            module.wait_for_tunnel_reachable("https://demo.trycloudflare.com/notamify", attempts=1, delay_seconds=0.01)

        sleep_mock.assert_not_called()

    def test_wait_for_tunnel_reachable_raises_when_unreachable(self):
        module = _load_local_service_run_module()

        with mock.patch.object(module.urllib.request, "urlopen", side_effect=OSError("connection refused")), mock.patch.object(
            module.time, "sleep"
        ) as sleep_mock:
            with self.assertRaises(RuntimeError):
                module.wait_for_tunnel_reachable("https://demo.trycloudflare.com/notamify", attempts=2, delay_seconds=0.01)

        sleep_mock.assert_called_once_with(0.01)


if __name__ == "__main__":
    unittest.main()
