import unittest

from notamify_sdk.signature import SignatureVerificationError, compute_signature, parse_signature_header, verify_signature


class SignatureTests(unittest.TestCase):
    def test_parse_header(self):
        parsed = parse_signature_header("t=1700000000,v1=abc,v1=def")
        self.assertEqual(parsed.timestamp, 1700000000)
        self.assertEqual(parsed.signatures, ["abc", "def"])

    def test_verify_valid(self):
        secret = "nmf_wh_test"
        ts = 1700000000
        body = b'{"ok":true}'
        sig = compute_signature(secret, ts, body)
        header = f"t={ts},v1={sig}"
        self.assertTrue(verify_signature(header, secret, body, tolerance_seconds=100000000, now_ts=ts))

    def test_verify_invalid_signature(self):
        with self.assertRaises(SignatureVerificationError):
            verify_signature("t=1700000000,v1=bad", "secret", b"{}", tolerance_seconds=100000000, now_ts=1700000000)

    def test_verify_tolerance(self):
        secret = "x"
        ts = 1700000000
        body = b"{}"
        sig = compute_signature(secret, ts, body)
        with self.assertRaises(SignatureVerificationError):
            verify_signature(f"t={ts},v1={sig}", secret, body, tolerance_seconds=10, now_ts=ts + 100)


if __name__ == "__main__":
    unittest.main()
