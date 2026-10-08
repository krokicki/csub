"""The ssh transport against a real, unprivileged sshd with a forced-command key.

Skipped unless an ``sshd`` binary is available (the pixi dev environment ships one via
conda-forge's openssh).
"""

from __future__ import annotations

import os
import pwd
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from csub.client.api import Client
from csub.client.config import ClientConfig
from csub.protocol import Mount
from csub.transport import SshTransport, TransportError
from tests.support.procs import wait_until

pytestmark = [pytest.mark.e2e, pytest.mark.integration]

SSHD = shutil.which("sshd") or ("/usr/sbin/sshd" if os.path.exists("/usr/sbin/sshd") else None)
pytestmark.append(pytest.mark.skipif(SSHD is None, reason="sshd not installed"))


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Sshd:
    def __init__(self, base: Path, port: int, client_key: Path, proc: subprocess.Popen, log: Path):
        self.base, self.port, self.client_key, self.proc, self.log = (
            base,
            port,
            client_key,
            proc,
            log,
        )


@pytest.fixture
def sshd(broker_cmd, broker_env, tmp_home):
    base = Path(
        tempfile.mkdtemp(prefix="csub-e2e-")
    )  # short path: ControlPath has a 108-byte limit
    try:
        subprocess.run(
            ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(base / "host_key")],
            check=True,
        )
        subprocess.run(
            ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(base / "client_key")],
            check=True,
        )
        pubkey = (base / "client_key.pub").read_text().strip()
        # The forced command *is* the broker; the fakes' configuration travels as key-bound
        # environment (PermitUserEnvironment), which is how a test stands in for ~/.config.
        env_opts = ",".join(
            f'environment="{k}={v}"'
            for k, v in broker_env.items()
            if k.startswith("CSUB_FAKE_") or k == "LC_ALL"
        )
        (base / "authorized_keys").write_text(
            f'restrict,command="{shlex.join(broker_cmd)}",{env_opts} {pubkey}\n'
        )
        (base / "authorized_keys").chmod(0o600)
        port = free_port()
        (base / "sshd_config").write_text(
            "\n".join(
                [
                    f"Port {port}",
                    "ListenAddress 127.0.0.1",
                    f"HostKey {base}/host_key",
                    "PidFile none",
                    f"AuthorizedKeysFile {base}/authorized_keys",
                    "StrictModes no",
                    "PubkeyAuthentication yes",
                    "PasswordAuthentication no",
                    "KbdInteractiveAuthentication no",
                    "UsePAM no",
                    "PermitUserEnvironment yes",
                    "AllowTcpForwarding no",
                    "X11Forwarding no",
                    "LogLevel VERBOSE",
                    "",
                ]
            )
        )
        log = base / "sshd.log"
        proc = subprocess.Popen(
            [SSHD, "-D", "-e", "-f", str(base / "sshd_config")],
            stdout=open(log, "ab"),
            stderr=subprocess.STDOUT,
        )

        def up():
            if proc.poll() is not None:
                raise RuntimeError(f"sshd exited: {log.read_text()}")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                    return True
            except OSError:
                return False

        wait_until(up, timeout=15, what="sshd to listen")
        yield Sshd(base, port, base / "client_key", proc, log)
    finally:
        if "proc" in locals():
            proc.terminate()
            proc.wait(timeout=10)
        shutil.rmtree(base, ignore_errors=True)


@pytest.fixture
def ssh_transport(sshd):
    return SshTransport(
        "127.0.0.1",
        user=pwd.getpwuid(os.getuid()).pw_name,
        key=str(sshd.client_key),
        port=sshd.port,
        broker_cmd="csub-broker",  # ignored by the forced command
        opts=("StrictHostKeyChecking=no", f"UserKnownHostsFile={sshd.base}/kh"),
        control_dir=str(sshd.base / "cm"),
        connect_timeout_s=10,
    )


@pytest.fixture
def ssh_client(ssh_transport, project_dir, roots, monkeypatch):
    monkeypatch.chdir(project_dir)
    cfg = ClientConfig(
        transport="ssh",
        mounts=(Mount(str(project_dir), "rw"), Mount(str(roots.nrs), "ro")),
        session="sshsess",
        wait_max_poll_s=0.5,
    )
    return Client(cfg, ssh_transport)


def test_submit_over_ssh(ssh_client, project_dir, fake_lsf, sshd):
    r = ssh_client.submit(command=["sh", "-c", "echo over-ssh; env"], name="ssh")
    assert r.queue == "short" and r.billing_group == "testlab"
    w = ssh_client.wait([r.job_id], timeout_s=60, tail_lines=500)
    assert w.all_done, (w.jobs[0].stderr_tail, sshd.log.read_text()[-2000:])
    out = w.jobs[0].stdout_tail
    assert out.startswith("over-ssh\n")
    env = dict(line.split("=", 1) for line in out.splitlines()[1:] if "=" in line)
    assert not any(k.startswith("SSH_") for k in env), "ssh session leaked into the job"
    assert env["CSUB_SESSION"] == "sshsess"
    assert fake_lsf.job(r.job_id)["env_mode"] == "none"
    # ControlMaster: the first call left a control socket behind for the next ones
    assert any(p.name.startswith("cm-") for p in (sshd.base / "cm").iterdir())
    assert [s.job_id for s in ssh_client.status()] == [r.job_id]


def test_forced_command_ignores_requested_command(ssh_transport, sshd):
    os.makedirs(ssh_transport.control_dir, exist_ok=True)
    argv = ssh_transport.argv[:-1] + ["echo pwned; cat /etc/passwd"]
    cp = subprocess.run(
        argv, input='{"protocol": 1, "op": "probe"}', capture_output=True, text=True, timeout=60
    )
    assert cp.returncode == 0, cp.stderr
    assert "pwned" not in cp.stdout and '"ok":true' in cp.stdout.replace(" ", "")
    assert "broker_version" in cp.stdout


def test_probe_and_policy_error_over_ssh(ssh_client, ssh_transport):
    assert ssh_client.probe().queues["gpu_l4"]["slots_per_gpu"] == 8
    with pytest.raises(Exception) as e:
        ssh_client.submit(command=["true"], image="localhost/x:1")
    assert getattr(e.value, "code", None) == "policy_violation"


def test_ssh_failure_is_transport_error(sshd, ssh_transport):
    bad = SshTransport(
        "127.0.0.1",
        user=ssh_transport.user,
        key=ssh_transport.key,
        port=free_port(),
        opts=ssh_transport.opts,
        control_dir=str(sshd.base / "cm2"),
        connect_timeout_s=3,
    )
    with pytest.raises(TransportError):
        bad.call({"protocol": 1, "op": "probe"})
    t0 = time.time()
    assert time.time() - t0 < 30 and sys.platform == "linux"
