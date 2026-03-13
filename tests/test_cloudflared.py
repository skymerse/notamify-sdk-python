import unittest

from notamify_sdk.cloudflared import CloudflaredError, CloudflaredManager, extract_tunnel_url


class CloudflaredTests(unittest.TestCase):
    def test_extract_tunnel_url(self):
        line = "INF Your quick Tunnel has been created! Visit it at https://abc.trycloudflare.com"
        self.assertEqual(extract_tunnel_url(line), "https://abc.trycloudflare.com")

    def test_missing_binary(self):
        manager = CloudflaredManager(local_url="http://127.0.0.1:8080", binary="__missing_cloudflared_binary__")
        with self.assertRaises(CloudflaredError):
            manager.start(timeout_seconds=0.1)

    def test_extract_tunnel_url_rejects_lookalike_host(self):
        line = "INF Visit https://eviltrycloudflare.com"
        self.assertEqual(extract_tunnel_url(line), "")


if __name__ == "__main__":
    unittest.main()
