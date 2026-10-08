"""Test-side view of the fake LSF and fake sandbox."""

from __future__ import annotations

import json
import os
import signal
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tests.support.procs import wait_until

FAKES_DIR = Path(__file__).resolve().parents[1] / "fakes"
FAKE_LSF_DIR = FAKES_DIR / "lsf"
FAKE_SANDBOX_DIR = FAKES_DIR / "sandbox"


@dataclass
class FakeLsf:
    state_path: Path
    extra_env: dict[str, str] = field(default_factory=dict)

    @property
    def bsub(self) -> Path:
        return FAKE_LSF_DIR / "bsub"

    @property
    def bjobs(self) -> Path:
        return FAKE_LSF_DIR / "bjobs"

    @property
    def bkill(self) -> Path:
        return FAKE_LSF_DIR / "bkill"

    def env(self) -> dict[str, str]:
        return {"CSUB_FAKE_LSF_STATE": str(self.state_path), **self.extra_env}

    def run(
        self, tool: str, *args: str, stdin: str | None = None, env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        full_env = {**os.environ, "LC_ALL": "C", **self.env(), **(env or {})}
        return subprocess.run(
            [str(FAKE_LSF_DIR / tool), *args],
            input=stdin,
            capture_output=True,
            text=True,
            env=full_env,
            timeout=30,
        )

    def jobs(self) -> dict[str, dict[str, Any]]:
        try:
            return json.loads(self.state_path.read_text())["jobs"]
        except FileNotFoundError:
            return {}

    def job(self, job_id: str) -> dict[str, Any]:
        return self.jobs()[str(job_id)]

    def wait_for(self, job_id: str, states: set[str], timeout: float = 20.0) -> dict[str, Any]:
        return wait_until(
            lambda: (j := self.jobs().get(str(job_id))) and j["status"] in states and j,
            timeout=timeout,
            what=f"job {job_id} in {states}",
        )

    def kill_all(self) -> None:
        for j in self.jobs().values():
            for key in ("pgid", "runner_pid"):
                pid = j.get(key)
                if pid:
                    try:
                        os.killpg(pid, signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        pass


@dataclass
class FakeSandbox:
    log_path: Path
    fail_rc: int | None = None

    @property
    def scripts_dir(self) -> Path:
        return FAKE_SANDBOX_DIR

    def env(self) -> dict[str, str]:
        env = {"CSUB_FAKE_SANDBOX_LOG": str(self.log_path)}
        if self.fail_rc is not None:
            env["CSUB_FAKE_SANDBOX_FAIL_RC"] = str(self.fail_rc)
        return env

    def invocations(self) -> list[dict[str, Any]]:
        try:
            return [
                json.loads(line) for line in self.log_path.read_text().splitlines() if line.strip()
            ]
        except FileNotFoundError:
            return []

    def for_job(self, job_id: str) -> dict[str, Any]:
        matches = [r for r in self.invocations() if r["env"].get("LSB_JOBID") == str(job_id)]
        assert matches, f"no podman-run.sh invocation for job {job_id}"
        return matches[-1]
