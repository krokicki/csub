"""Client -> LocalTransport -> real broker -> fakes, including nested submission from a job."""

import shlex
import sys
import time

import pytest

from csub.client.api import Client
from csub.client.config import ClientConfig
from csub.protocol import Mount
from csub.transport import LocalTransport

pytestmark = pytest.mark.integration


@pytest.fixture
def client(broker_cmd, broker_env, project_dir, roots, monkeypatch) -> Client:
    monkeypatch.chdir(project_dir)
    cfg = ClientConfig(
        transport="local",
        broker_cmd=shlex.join(broker_cmd),
        mounts=(Mount(str(project_dir), "rw"), Mount(str(roots.nrs), "ro")),
        session="testsess",
        wait_max_poll_s=0.5,
    )
    return Client(cfg, LocalTransport(broker_cmd, env=broker_env))


def test_submit_wait_logs_probe(client, project_dir):
    assert client.probe().queues["short"]["mem_per_slot_mb"] == 15360
    r = client.submit(command=["sh", "-c", "echo hello; echo err >&2; exit 0"], name="hi")
    assert r.queue == "short" and r.job_dir == f"{project_dir}/.csub/jobs/{r.job_id}"
    w = client.wait([r.job_id], timeout_s=60)
    assert w.all_done and w.jobs[0].stdout_tail == "hello\n" and w.jobs[0].stderr_tail == "err\n"
    lg = client.logs(r.job_id)
    assert lg.stdout == "hello\n" and lg.job_dir == r.job_dir
    assert [s.job_id for s in client.status()] == [r.job_id]


def test_kill_via_client(client):
    r = client.submit(command=["sleep", "30"])
    while client.status([r.job_id])[0].state != "RUN":
        time.sleep(0.1)
    assert client.kill([r.job_id]).killed == [r.job_id]
    w = client.wait([r.job_id], timeout_s=30)
    assert w.jobs[0].status.state == "EXIT"


NESTED = """
import csub, os, sys
me = csub.self_job()
print("self", me.job_id, me.session, me.walltime_min, me.seconds_left() > 0)
r = csub.submit(command=["sh", "-c", "echo child-out; echo $CSUB_JOB_ID"], name="child")
print("child", r.job_id, r.session, r.image)
w = csub.wait([r.job_id], timeout_s=60)
print("state", w.jobs[0].status.state, w.jobs[0].stdout_tail.strip().replace("\\n", "|"))
"""


def test_nested_submission_from_inside_a_job(client, project_dir, fake_lsf, fake_sandbox):
    parent = client.submit(command=[sys.executable, "-c", NESTED], name="parent", walltime_min=5)
    w = client.wait([parent.job_id], timeout_s=90)
    out = w.jobs[0].stdout_tail
    assert w.jobs[0].status.state == "DONE", (out, w.jobs[0].stderr_tail)
    lines = dict(line.split(" ", 1) for line in out.strip().splitlines())
    assert lines["self"] == f"{parent.job_id} testsess 5 True"
    child_id, child_session, child_image = lines["child"].split()
    assert child_session == "testsess" and child_image == parent.image
    assert lines["state"] == f"DONE child-out|{child_id}"
    # the child is a first-class job: same group, same mounts, its own sandbox, visible to us
    assert fake_lsf.job(child_id)["group"] == fake_lsf.job(parent.job_id)["group"]
    rec = fake_sandbox.for_job(child_id)
    assert rec["rw"] == [str(project_dir)] and rec["image"] == parent.image
    assert (
        project_dir / ".csub" / "jobs" / child_id / "stdout"
    ).read_text() == f"child-out\n{child_id}\n"
    assert {s.job_id for s in client.status()} == {parent.job_id, child_id}
    assert client.logs(child_id).stdout.startswith("child-out")


RESUBMIT = """
import csub, os, sys, time
me = csub.self_job()
marker = os.path.join(os.getcwd(), "progress.txt")
if os.path.exists(marker) and open(marker).read().strip() == "finished":
    print("already finished, exiting")
    sys.exit(0)
# Queue the successor first so it runs whether we finish, fail or get killed at walltime.
succ = csub.submit(command=[sys.executable, __file__], name="training", walltime_min=1,
                   depends_on=[{"job_id": me.job_id, "when": "ended"}])
print("successor", succ.job_id, flush=True)
# "Train" for about a second, checkpoint, then let the walltime kill us. (Real code would
# checkpoint when seconds_left() gets small; the fake LSF's minute is only a few seconds.)
while me.seconds_left() > me.walltime_min * 60 - 1:
    time.sleep(0.1)
open(marker, "w").write("finished")
print("checkpointed", flush=True)
time.sleep(600)
"""


def test_checkpoint_and_resubmit_pattern(client, project_dir, fake_lsf):
    # One fake "minute" is 3 seconds, so a 1-minute job hits its run limit quickly.
    client.transport.env["CSUB_FAKE_LSF_MINUTE_S"] = "3"
    script = project_dir / "train.py"
    script.write_text(RESUBMIT)
    first = client.submit(command=[sys.executable, str(script)], name="training", walltime_min=1)
    w = client.wait([first.job_id], timeout_s=60)
    assert w.jobs[0].status.state == "EXIT", w.jobs[0].stderr_tail  # killed at walltime
    assert fake_lsf.job(first.job_id)["term_reason"] == "TERM_RUNLIMIT"
    out = w.jobs[0].stdout_tail
    assert "checkpointed" in out, out
    successor = out.split("successor ")[1].split()[0]
    assert fake_lsf.job(successor)["deps"] == [["ended", first.job_id]]
    w2 = client.wait([successor], timeout_s=60)
    assert w2.jobs[0].status.state == "DONE" and "already finished" in w2.jobs[0].stdout_tail
    assert (project_dir / "progress.txt").read_text() == "finished"


def test_nested_submission_via_injected_cli(client, project_dir, fake_lsf):
    """What deploy/smoke.sh does: a job calls `csub` without csub being in the image."""
    parent = client.submit(
        command=["sh", "-c", "csub submit --walltime 5 -- sh -c 'echo grandchild' && csub status"],
        name="cli-parent",
        walltime_min=5,
    )
    w = client.wait([parent.job_id], timeout_s=90)
    assert w.jobs[0].status.state == "DONE", (w.jobs[0].stdout_tail, w.jobs[0].stderr_tail)
    out = w.jobs[0].stdout_tail
    assert out.startswith("Job ") and "submitted to short" in out
    child = out.split()[1]
    assert fake_lsf.job(child)["group"] == fake_lsf.job(parent.job_id)["group"]
    assert client.wait([child], timeout_s=60).jobs[0].stdout_tail == "grandchild\n"
