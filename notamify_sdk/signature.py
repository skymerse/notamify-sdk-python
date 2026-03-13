from __future__ import annotations

import hashlib
import hmac
import time
from dataclasses import dataclass


class SignatureVerificationError(Exception):
    pass


@dataclass
class ParsedSignature:
    timestamp: int
    signatures: list[str]


def parse_signature_header(header: str) -> ParsedSignature:
    if not header.strip():
        raise SignatureVerificationError("missing signature header")
    timestamp = None
    signatures: list[str] = []
    for part in header.split(","):
        part = part.strip()
        if part.startswith("t="):
            try:
                timestamp = int(part[2:])
            except ValueError as exc:
                raise SignatureVerificationError("invalid signature timestamp") from exc
        elif part.startswith("v1="):
            signatures.append(part[3:])
    if timestamp is None or not signatures:
        raise SignatureVerificationError("invalid signature header")
    return ParsedSignature(timestamp=timestamp, signatures=signatures)


def compute_signature(secret: str, timestamp: int, body: bytes) -> str:
    mac = hmac.new(secret.encode("utf-8"), digestmod=hashlib.sha256)
    mac.update(str(timestamp).encode("utf-8"))
    mac.update(b".")
    mac.update(body)
    return mac.hexdigest()


def verify_signature(
    header: str,
    secret: str,
    body: bytes,
    tolerance_seconds: int = 600,
    now_ts: int | None = None,
) -> bool:
    parsed = parse_signature_header(header)
    now = now_ts if now_ts is not None else int(time.time())
    if tolerance_seconds > 0 and abs(now - parsed.timestamp) > tolerance_seconds:
        raise SignatureVerificationError("signature timestamp outside tolerance")
    expected = compute_signature(secret, parsed.timestamp, body)
    for candidate in parsed.signatures:
        if hmac.compare_digest(candidate, expected):
            return True
    raise SignatureVerificationError("signature mismatch")
