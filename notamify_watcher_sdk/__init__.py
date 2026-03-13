"""Compatibility exports for the legacy Notamify Watcher SDK package."""

from .client import APIError, WatcherClient
from .cloudflared import CloudflaredError, CloudflaredManager, TunnelInfo, extract_tunnel_url
from .config import ConfigStore, SDKConfig
from .receiver import ReceiverConfig, ReceivedEvent, WebhookReceiver
from .signature import SignatureVerificationError, compute_signature, parse_signature_header, verify_signature

__all__ = [
    "APIError",
    "CloudflaredError",
    "CloudflaredManager",
    "ConfigStore",
    "ReceiverConfig",
    "ReceivedEvent",
    "SDKConfig",
    "SignatureVerificationError",
    "TunnelInfo",
    "WatcherClient",
    "WebhookReceiver",
    "compute_signature",
    "extract_tunnel_url",
    "parse_signature_header",
    "verify_signature",
]
