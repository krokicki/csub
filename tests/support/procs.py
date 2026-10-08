"""Small process/polling helpers for tests."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T")


def wait_until(
    pred: Callable[[], T], timeout: float = 20.0, poll: float = 0.1, what: str = "condition"
) -> T:
    """Poll ``pred`` until it returns a truthy value; return it. Raise on timeout."""
    deadline = time.time() + timeout
    while True:
        value = pred()
        if value:
            return value
        if time.time() > deadline:
            raise TimeoutError(f"timed out after {timeout}s waiting for {what}")
        time.sleep(poll)
