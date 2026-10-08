"""The broker, protocol and (later) client must import nothing outside the stdlib."""

import subprocess
import sys

import pytest

MODULES = [
    "csub",
    "csub._paths",
    "csub.protocol",
    "csub.broker.policy",
    "csub.broker.resolve",
    "csub.broker.wrapper",
    "csub.broker.state",
    "csub.broker.lsf",
    "csub.broker.ops",
    "csub.broker.main",
    "csub.broker.serve",
    "csub.transport",
    "csub.client",
    "csub.cli",
]

SNIPPET = r"""
import sys
before = set(sys.modules)
for m in %r:
    __import__(m)
extra = sorted(
    name.split(".")[0]
    for name in set(sys.modules) - before
    if not name.startswith("csub") and name.split(".")[0] not in sys.stdlib_module_names
)
print(",".join(extra))
"""


def test_broker_side_modules_are_stdlib_only():
    if not hasattr(sys, "stdlib_module_names"):
        pytest.skip("needs Python 3.10+ to enumerate the stdlib")
    out = subprocess.run(
        [sys.executable, "-c", SNIPPET % MODULES], capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == "", f"third-party imports: {out.stdout.strip()}"
