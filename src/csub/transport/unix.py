"""Talk to the per-job broker over the Unix socket the wrapper bind-mounted into the sandbox."""

from __future__ import annotations

import socket
from typing import Any

from csub.protocol import parse_response
from csub.transport.base import TransportError, encode


class UnixTransport:
    def __init__(self, path: str, *, timeout_s: float = 600.0):
        self.path = path
        self.timeout_s = timeout_s

    def call(self, request: dict[str, Any]) -> dict[str, Any]:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(self.timeout_s)
                s.connect(self.path)
                s.sendall(encode(request).encode())
                s.shutdown(socket.SHUT_WR)
                chunks: list[bytes] = []
                while True:
                    chunk = s.recv(65536)
                    if not chunk:
                        break
                    chunks.append(chunk)
        except socket.timeout:  # alias of TimeoutError on 3.10+
            raise TransportError(
                f"broker at {self.path} did not answer within {self.timeout_s}s"
            ) from None
        except OSError as e:
            raise TransportError(f"cannot reach broker socket {self.path}: {e}") from None
        data = b"".join(chunks)
        if not data:
            raise TransportError(f"broker at {self.path} closed the connection without a response")
        return parse_response(data)
