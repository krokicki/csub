"""Shared fixtures: a hermetic home, allowed roots, a policy pointing at fakes."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from csub.broker.policy import Policy, parse_policy
from csub.broker.resolve import ResolveContext
from csub.protocol import JobSpec
from tests.support.policyfile import default_policy_dict

TEST_USER = "testuser"


@dataclass
class Roots:
    allowed: Path  # /groups/lab
    nrs: Path  # /nrs/lab
    raw_ro: Path  # /nrs/lab/raw  (readonly_roots)
    denied: Path  # /groups/lab/secret (denied_roots)
    outside: Path  # not allowed at all
    scratch_root: Path


@pytest.fixture
def tmp_home(tmp_path: Path) -> Path:
    home = (tmp_path / "home").resolve()
    (home / ".config" / "csub").mkdir(parents=True)
    return home


@pytest.fixture
def roots(tmp_path: Path) -> Roots:
    base = tmp_path.resolve()
    r = Roots(
        allowed=base / "groups" / "lab",
        nrs=base / "nrs" / "lab",
        raw_ro=base / "nrs" / "lab" / "raw",
        denied=base / "groups" / "lab" / "secret",
        outside=base / "elsewhere",
        scratch_root=base / "scratch",
    )
    for p in (r.allowed, r.nrs, r.raw_ro, r.denied, r.outside, r.scratch_root):
        p.mkdir(parents=True, exist_ok=True)
    return r


@pytest.fixture
def project_dir(roots: Roots) -> Path:
    p = roots.allowed / "project"
    p.mkdir()
    return p


@pytest.fixture
def scripts_dir(tmp_home: Path) -> Path:
    """A stand-in agentic-sandbox scripts dir under $HOME (the normal placement)."""
    d = tmp_home / ".local" / "share" / "csub" / "agentic-sandbox" / "scripts"
    d.mkdir(parents=True)
    script = d / "podman-run.sh"
    script.write_text("#!/bin/sh\nexit 0\n")
    os.chmod(script, 0o755)
    return d


@pytest.fixture
def policy_dict(tmp_home: Path, roots: Roots, scripts_dir: Path) -> dict[str, Any]:
    return default_policy_dict(
        home=tmp_home,
        scripts_dir=scripts_dir,
        allowed_roots=[roots.allowed, roots.nrs],
        denied_roots=[roots.denied],
        readonly_roots=[roots.raw_ro],
        scratch_root=roots.scratch_root,
    )


@pytest.fixture
def policy(policy_dict: dict[str, Any], tmp_home: Path) -> Policy:
    return parse_policy(policy_dict, home=str(tmp_home), source="test-policy")


@pytest.fixture
def make_policy(policy_dict: dict[str, Any], tmp_home: Path):
    """Return a Policy built from policy_dict after applying nested overrides."""

    def _make(**sections: dict[str, Any]) -> Policy:
        d = {k: dict(v) for k, v in policy_dict.items()}
        d["queues"] = {k: dict(v) for k, v in policy_dict["queues"].items()}
        for section, overrides in sections.items():
            d.setdefault(section, {}).update(overrides)
        return parse_policy(d, home=str(tmp_home), source="test-policy")

    return _make


@pytest.fixture
def ctx(tmp_home: Path, policy: Policy) -> ResolveContext:
    return ResolveContext(
        user=TEST_USER,
        home=str(tmp_home),
        protected_dirs=(
            os.path.realpath(policy.state_dir),
            os.path.realpath(policy.sandbox_scripts_dir),
        ),
    )


@pytest.fixture
def make_spec(project_dir: Path, roots: Roots):
    """JobSpec factory with sensible defaults; keyword overrides replace fields."""

    def _make(**overrides: Any) -> JobSpec:
        d: dict[str, Any] = {
            "command": ["sh", "-c", "echo hi"],
            "cwd": str(project_dir),
            "mounts": [
                {"path": str(project_dir), "mode": "rw"},
                {"path": str(roots.nrs), "mode": "ro"},
            ],
            "session": "testsess",
        }
        d.update(overrides)
        return JobSpec.from_dict({k: v for k, v in d.items() if v is not None})

    return _make


# --- fakes -----------------------------------------------------------------------------

from tests.support.fakelsf_api import FakeLsf, FakeSandbox  # noqa: E402


@pytest.fixture
def fake_lsf(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    f = FakeLsf(state_path=tmp_path.resolve() / "lsf" / "state.json")
    f.extra_env = {"CSUB_FAKE_LSF_BILLING_GROUP": "testlab", "CSUB_FAKE_LSF_KILL_GRACE_S": "0.3"}
    for k, v in f.env().items():
        monkeypatch.setenv(k, v)
    yield f
    f.kill_all()


@pytest.fixture
def fake_sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeSandbox:
    s = FakeSandbox(log_path=tmp_path.resolve() / "sandbox" / "invocations.jsonl")
    s.log_path.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("CSUB_FAKE_SANDBOX_LOG", str(s.log_path))
    return s


# --- snapshots ---------------------------------------------------------------------------


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption("--update-snapshots", action="store_true", help="rewrite snapshot files")


@pytest.fixture
def snapshot(request: pytest.FixtureRequest):
    """snapshot(name, text): compare text with tests/unit/snapshots/<name>, or rewrite it."""
    snap_dir = Path(__file__).parent / "unit" / "snapshots"

    def _check(name: str, text: str) -> None:
        path = snap_dir / name
        if request.config.getoption("--update-snapshots") or not path.exists():
            path.write_text(text)
            return
        expected = path.read_text()
        assert text == expected, f"snapshot {name} differs; run pytest --update-snapshots to accept"

    return _check
