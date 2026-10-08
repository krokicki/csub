from __future__ import annotations

import json
from typing import Any, Protocol

from csub.protocol import CsubError, parse_response


class TransportError(CsubError):
    def __init__(self, message: str):
        super().__init__("transport_error", message)


class Transport(Protocol):
    def call(self, request: dict[str, Any]) -> dict[str, Any]:
        """Deliver one request; return the parsed response envelope (ok or error)."""


def encode(request: dict[str, Any]) -> str:
    return json.dumps(request, separators=(",", ":"))


def finish(stdout: str, returncode: int, stderr: str, what: str) -> dict[str, Any]:
    """The broker exits 0 (ok) or 1 (error response); anything else is a transport failure."""
    if returncode not in (0, 1):
        tail = "\n".join(stderr.strip().splitlines()[-5:]) or f"exit status {returncode}"
        raise TransportError(f"{what} failed: {tail}")
    return parse_response(stdout)
