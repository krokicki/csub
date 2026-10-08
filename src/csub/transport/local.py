"""Run csub-broker as a subprocess (humans on a submit host; tests)."""

from __future__ import annotations

import shlex
import subprocess
from collections.abc import Mapping
from typing import Any

from csub.transport.base import TransportError, encode, finish


class LocalTransport:
    def __init__(
        self,
        broker_cmd: str | list[str] = "csub-broker",
        *,
        env: Mapping[str, str] | None = None,
        timeout_s: float = 600.0,
    ):
        self.argv = shlex.split(broker_cmd) if isinstance(broker_cmd, str) else list(broker_cmd)
        self.env = dict(env) if env is not None else None
        self.timeout_s = timeout_s

    def call(self, request: dict[str, Any]) -> dict[str, Any]:
        try:
            cp = subprocess.run(
                self.argv,
                input=encode(request),
                capture_output=True,
                text=True,
                env=self.env,
                timeout=self.timeout_s,
            )
        except FileNotFoundError:
            raise TransportError(f"broker command not found: {self.argv[0]}") from None
        except subprocess.TimeoutExpired:
            raise TransportError(f"broker did not answer within {self.timeout_s}s") from None
        return finish(cp.stdout, cp.returncode, cp.stderr, "csub-broker")
