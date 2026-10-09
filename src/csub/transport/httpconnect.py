"""ssh ProxyCommand that tunnels through the HTTP CONNECT proxy in $http_proxy.

Sandboxes such as agentic-sandbox have no network except an HTTP proxy; ssh cannot use one by
itself and socat/nc are not always installed, so this is the stdlib equivalent:

    ProxyCommand=python3 -m csub.transport.httpconnect %h %p

Without http_proxy/https_proxy in the environment it connects directly, so the same client
config works inside and outside the sandbox.
"""

from __future__ import annotations

import os
import select
import socket
import sys
from urllib.parse import urlsplit


def connect(host: str, port: int, proxy_url: str) -> tuple[socket.socket, bytes]:
    """Open a CONNECT tunnel; returns the socket and any bytes received after the headers."""
    u = urlsplit(proxy_url if "//" in proxy_url else "http://" + proxy_url)
    if not u.hostname or not u.port:
        raise OSError(f"bad proxy URL {proxy_url!r}")
    s = socket.create_connection((u.hostname, u.port))
    s.sendall(f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n".encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = s.recv(4096)
        if not chunk:
            raise OSError(f"proxy closed the connection before answering CONNECT {host}:{port}")
        buf += chunk
    head, _, rest = buf.partition(b"\r\n\r\n")
    status = head.split(b" ", 2)
    if len(status) < 2 or status[1] != b"200":
        reply = head.splitlines()[0].decode("latin1")
        raise OSError(f"proxy refused CONNECT {host}:{port}: {reply}")
    return s, rest


def _write_all(fd: int, data: bytes) -> None:
    while data:
        data = data[os.write(fd, data) :]


def pump(s: socket.socket, initial: bytes, in_fd: int = 0, out_fd: int = 1) -> None:
    """Copy in_fd -> socket and socket -> out_fd until the remote side closes."""
    _write_all(out_fd, initial)
    fds = [in_fd, s.fileno()]
    while True:
        ready, _, _ = select.select(fds, [], [])
        if in_fd in ready:
            data = os.read(in_fd, 65536)
            if data:
                s.sendall(data)
            else:
                fds.remove(in_fd)
                s.shutdown(socket.SHUT_WR)
        if s.fileno() in ready:
            data = s.recv(65536)
            if not data:
                return
            _write_all(out_fd, data)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 2:
        print("usage: python3 -m csub.transport.httpconnect HOST PORT", file=sys.stderr)
        return 2
    proxy_url = os.environ.get("https_proxy") or os.environ.get("http_proxy")
    try:
        if proxy_url:
            s, rest = connect(argv[0], int(argv[1]), proxy_url)
        else:
            s, rest = socket.create_connection((argv[0], int(argv[1]))), b""
    except OSError as e:
        print(f"httpconnect: {e}", file=sys.stderr)
        return 1
    with s:
        pump(s, rest)
    return 0


if __name__ == "__main__":
    sys.exit(main())
