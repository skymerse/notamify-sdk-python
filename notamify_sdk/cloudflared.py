from __future__ import annotations

import queue
import re
import subprocess
import threading
import time
from dataclasses import dataclass

_TUNNEL_RE = re.compile(r"https://(?:[a-zA-Z0-9-]+\.)*trycloudflare\.com\b")


class CloudflaredError(Exception):
    pass


@dataclass
class TunnelInfo:
    public_url: str
    local_url: str


def extract_tunnel_url(line: str) -> str:
    match = _TUNNEL_RE.search(line)
    return match.group(0) if match else ""


class CloudflaredManager:
    def __init__(self, local_url: str, binary: str = "cloudflared") -> None:
        self.local_url = local_url
        self.binary = binary
        self.process: subprocess.Popen[str] | None = None
        self.public_url = ""
        self._reader: threading.Thread | None = None
        self._line_queue: queue.Queue[str] = queue.Queue()

    def is_running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def start(self, timeout_seconds: float = 20.0) -> TunnelInfo:
        if self.is_running() and self.public_url:
            return TunnelInfo(public_url=self.public_url, local_url=self.local_url)

        try:
            self.process = subprocess.Popen(
                [self.binary, "tunnel", "--url", self.local_url],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except FileNotFoundError as exc:
            raise CloudflaredError("cloudflared executable not found in PATH") from exc

        assert self.process.stdout is not None

        def _read_lines() -> None:
            for line in self.process.stdout:
                self._line_queue.put(line.rstrip("\n"))

        self._reader = threading.Thread(target=_read_lines, daemon=True)
        self._reader.start()

        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            if self.process.poll() is not None:
                raise CloudflaredError("cloudflared exited before tunnel URL became available")
            try:
                line = self._line_queue.get(timeout=0.25)
            except queue.Empty:
                continue
            url = extract_tunnel_url(line)
            if url:
                self.public_url = url
                return TunnelInfo(public_url=self.public_url, local_url=self.local_url)

        self.stop()
        raise CloudflaredError("timed out waiting for cloudflared quick tunnel URL")

    def stop(self) -> None:
        if self.process is None:
            return
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
        self.process = None
        self.public_url = ""
