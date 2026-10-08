"""Integration fixtures: a real csub-broker subprocess, fake LSF, fake sandbox."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from tests.conftest import Roots
from tests.support.policyfile import default_policy_dict, write_policy
from tests.support.procs import wait_until

pytestmark = pytest.mark.integration


@pytest.fixture
def int_policy_dict(tmp_home: Path, roots: Roots, fake_lsf, fake_sandbox) -> dict[str, Any]:
    d = default_policy_dict(
        home=tmp_home,
        scripts_dir=fake_sandbox.scripts_dir,
        allowed_roots=[roots.allowed, roots.nrs],
        denied_roots=[roots.denied],
        readonly_roots=[roots.raw_ro],
        scratch_root=roots.scratch_root,
        lsf_dir=fake_lsf.bsub.parent,
    )
    d["broker"]["allowed_hosts"].append("api.anthropic.com")
    return d


@pytest.fixture
def policy_path(int_policy_dict, tmp_home: Path) -> Path:
    return write_policy(tmp_home / ".config" / "csub" / "broker.toml", int_policy_dict)


@pytest.fixture
def broker_cmd(policy_path: Path, tmp_home: Path) -> list[str]:
    return [
        sys.executable,
        "-m",
        "csub.broker",
        "--policy",
        str(policy_path),
        "--home",
        str(tmp_home),
    ]


@pytest.fixture
def broker_env(tmp_home: Path, fake_lsf, fake_sandbox) -> dict[str, str]:
    return {
        "HOME": str(tmp_home),
        "PATH": os.environ["PATH"],
        "LC_ALL": "C",
        **fake_lsf.env(),
        **fake_sandbox.env(),
    }


class Broker:
    def __init__(self, cmd: list[str], env: dict[str, str]):
        self.cmd = cmd
        self.env = env

    def raw(self, text: str) -> tuple[dict, int, str]:
        cp = subprocess.run(
            self.cmd, input=text, capture_output=True, text=True, env=self.env, timeout=60
        )
        return json.loads(cp.stdout), cp.returncode, cp.stderr

    def call(self, req: dict) -> dict:
        resp, rc, stderr = self.raw(json.dumps(req))
        assert rc == (0 if resp["ok"] else 1), (resp, stderr)
        return resp

    def ok(self, req: dict) -> dict:
        resp = self.call(req)
        assert resp["ok"], resp
        return resp

    def error(self, req: dict, code: str) -> dict:
        resp = self.call(req)
        assert not resp["ok"] and resp["error"]["code"] == code, resp
        return resp["error"]

    def status(self, *ids: str, session: str | None = None) -> dict[str, dict]:
        req: dict[str, Any] = {"protocol": 1, "op": "status"}
        if ids:
            req["job_ids"] = list(ids)
        if session:
            req["session"] = session
        return {e["job_id"]: e for e in self.ok(req)["jobs"]}

    def wait(self, job_id: str, states=("DONE", "EXIT"), timeout: float = 30.0) -> dict:
        return wait_until(
            lambda: (e := self.status(job_id)[job_id]) and e["state"] in states and e,
            timeout=timeout,
            what=f"job {job_id} in {states}",
        )


@pytest.fixture
def broker(broker_cmd, broker_env) -> Broker:
    return Broker(broker_cmd, broker_env)


@pytest.fixture
def submit(broker: Broker, project_dir: Path, roots: Roots):
    def _submit(command=("sh", "-c", "echo hi"), *, expect_ok=True, **fields) -> dict:
        job: dict[str, Any] = {
            "command": list(command) if not isinstance(command, str) else command,
            "cwd": str(project_dir),
            "mounts": [
                {"path": str(project_dir), "mode": "rw"},
                {"path": str(roots.nrs), "mode": "ro"},
            ],
            "session": "testsess",
        }
        job.update(fields)
        req = {"protocol": 1, "op": "submit", "job": job}
        return broker.ok(req) if expect_ok else broker.call(req)

    return _submit


@pytest.fixture
def job_dir(project_dir: Path):
    return lambda job_id: project_dir / ".csub" / "jobs" / str(job_id)
