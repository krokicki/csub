"""The fakes must be trusted before anything is tested against them."""

import json
import os
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from tests.support.fakelsf_api import FakeSandbox
from tests.support.procs import wait_until

SUBMIT_LINE = r"^Job <(\d+)> is submitted to (default )?queue <(\w+)>\.$"


def submit(fake_lsf, script: str, *args: str, env=None) -> str:
    r = fake_lsf.run("bsub", *args, stdin=script, env=env)
    assert r.returncode == 0, r.stderr
    lines = r.stdout.splitlines()
    assert lines[0] == "This job will be billed to testlab"
    import re

    m = re.match(SUBMIT_LINE, lines[1])
    assert m, lines
    return m.group(1)


BROKER_ARGS = (
    "-q",
    "short",
    "-n",
    "2",
    "-W",
    "10",
    "-J",
    "csub-x",
    "-g",
    "/csub/u/s",
    "-env",
    "none",
)


def test_bsub_runs_spooled_script(fake_lsf, tmp_path):
    out = tmp_path / "out" / "%J" / "lsf.out"
    jid = submit(fake_lsf, "#!/bin/sh\necho hello\n", *BROKER_ARGS, "-o", str(out))
    job = fake_lsf.wait_for(jid, {"DONE"})
    assert job["slots"] == 2 and job["walltime_min"] == 10 and job["group"] == "/csub/u/s"
    assert job["env_mode"] == "none" and job["exit_code"] == 0 and job["exec_host"] == "fakehost01"
    text = (tmp_path / "out" / jid / "lsf.out").read_text()
    assert text.startswith("hello\n") and "Successfully completed." in text
    assert open(job["spool"]).read() == "#!/bin/sh\necho hello\n"


def test_default_queue_line(fake_lsf):
    r = fake_lsf.run("bsub", stdin="echo hi\n")
    assert "is submitted to default queue <local>." in r.stdout


def test_env_none_scrubs_and_sets_lsf_vars(fake_lsf, tmp_path, monkeypatch):
    monkeypatch.setenv("LEAKY_VAR", "1")
    out = tmp_path / "o.txt"
    jid = submit(fake_lsf, "env\n", *BROKER_ARGS, "-gpu", "num=2", "-o", str(out))
    fake_lsf.wait_for(jid, {"DONE"})
    env = dict(
        line.split("=", 1) for line in out.read_text().split("\n---")[0].splitlines() if "=" in line
    )
    assert env["LSB_JOBID"] == jid and env["LSB_DJOB_NUMPROC"] == "2"
    assert env["CUDA_VISIBLE_DEVICES"] == "0,1" and env["LSB_JOBGROUP"] == "/csub/u/s"
    assert env["PATH"] == "/usr/local/bin:/usr/bin:/bin"
    assert "LEAKY_VAR" not in env and "PYTEST_CURRENT_TEST" not in env
    assert env["CSUB_FAKE_LSF_STATE"] == str(fake_lsf.state_path)  # documented test-only leak


def test_no_gpu_no_cuda_var(fake_lsf, tmp_path):
    out = tmp_path / "o.txt"
    jid = submit(fake_lsf, "env\n", *BROKER_ARGS, "-o", str(out))
    fake_lsf.wait_for(jid, {"DONE"})
    assert "CUDA_VISIBLE_DEVICES" not in out.read_text()


def test_exit_code_and_bjobs_formats(fake_lsf):
    jid = submit(fake_lsf, "exit 3\n", *BROKER_ARGS)
    fake_lsf.wait_for(jid, {"EXIT"})
    r = fake_lsf.run(
        "bjobs",
        "-a",
        "-noheader",
        "-o",
        'jobid stat exit_code queue delimiter="|"',
        "-g",
        "/csub/u",
        jid,
    )
    assert r.stdout == f"{jid}|EXIT|3|short\n"
    r = fake_lsf.run("bjobs", "-noheader", "-o", "jobid stat", jid)
    assert r.stdout.split() == [jid, "EXIT"]
    r = fake_lsf.run("bjobs", "-a", "-json", "-o", "jobid stat", jid)
    data = json.loads(r.stdout)
    assert data["JOBS"] == 1 and data["RECORDS"][0] == {"JOBID": jid, "STAT": "EXIT"}
    r = fake_lsf.run("bjobs", "-a")
    assert r.stdout.splitlines()[0].startswith("JOBID") and jid in r.stdout


def test_bjobs_visibility_rules(fake_lsf):
    r = fake_lsf.run("bjobs", "-a", "-g", "/csub/u")
    assert r.returncode == 255 and "No unfinished job found" in r.stderr
    done = submit(fake_lsf, "true\n", *BROKER_ARGS)
    fake_lsf.wait_for(done, {"DONE"})
    running = submit(fake_lsf, "sleep 30\n", *BROKER_ARGS)
    fake_lsf.wait_for(running, {"RUN"})
    without_a = fake_lsf.run("bjobs", "-noheader", "-o", "jobid", "-g", "/csub/u").stdout.split()
    assert without_a == [running]
    with_a = fake_lsf.run("bjobs", "-a", "-noheader", "-o", "jobid", "-g", "/csub/u").stdout.split()
    assert with_a == [done, running]
    other_group = fake_lsf.run(
        "bjobs", "-a", "-noheader", "-o", "jobid", "-g", "/other"
    ).stdout.split()
    assert other_group == []
    r = fake_lsf.run("bjobs", "-noheader", "-o", "jobid stat", "999999", running)
    assert "Job <999999> is not found" in r.stderr and r.returncode == 0 and running in r.stdout
    r = fake_lsf.run("bjobs", "999999")
    assert r.returncode == 255
    # History expiry
    r = fake_lsf.run(
        "bjobs",
        "-a",
        "-noheader",
        "-o",
        "jobid",
        "-g",
        "/csub/u",
        env={"CSUB_FAKE_LSF_CLEAN_PERIOD_S": "0"},
    )
    assert r.stdout.split() == [running]
    fake_lsf.run("bkill", running)
    fake_lsf.wait_for(running, {"EXIT"})


def test_bkill_running_and_pending(fake_lsf):
    running = submit(fake_lsf, "sleep 30\n", *BROKER_ARGS)
    fake_lsf.wait_for(running, {"RUN"})
    r = fake_lsf.run("bkill", running)
    assert r.returncode == 0 and r.stdout == f"Job <{running}> is being terminated\n"
    job = fake_lsf.wait_for(running, {"EXIT"})
    assert job["term_reason"] == "TERM_OWNER" and job["exit_code"] in (130, 143, 137)
    r = fake_lsf.run("bkill", running)
    assert r.returncode == 255 and "already finished" in r.stderr
    r = fake_lsf.run("bkill", "424242")
    assert r.returncode == 255 and "No matching job found" in r.stderr
    pend = submit(
        fake_lsf, "true\n", *BROKER_ARGS, "-w", f"exit({running})"
    )  # never satisfied (EXIT) -> actually satisfied
    # exit(<EXIT job>) is satisfied; use done(<EXIT job>) for a never-satisfied dependency instead
    never = submit(fake_lsf, "true\n", *BROKER_ARGS, "-w", f"done({running})")
    fake_lsf.wait_for(pend, {"DONE"})
    time.sleep(0.5)
    assert fake_lsf.job(never)["status"] == "PEND"
    r = fake_lsf.run("bkill", "-g", "/csub/u", "0")
    assert never in r.stdout
    assert fake_lsf.wait_for(never, {"EXIT"})["exit_code"] == 130


def test_bkill_group_scoping(fake_lsf):
    a = submit(fake_lsf, "sleep 30\n", *BROKER_ARGS)
    fake_lsf.wait_for(a, {"RUN"})
    r = fake_lsf.run("bkill", "-g", "/csub/other", a)
    assert r.returncode == 255 and "No matching job found" in r.stderr
    r = fake_lsf.run("bkill", "-g", "/csub/u/s", a)
    assert r.returncode == 0
    fake_lsf.wait_for(a, {"EXIT"})


def test_dependencies(fake_lsf):
    a = submit(fake_lsf, "sleep 1\n", *BROKER_ARGS)
    b = submit(fake_lsf, "true\n", *BROKER_ARGS, "-w", f"done({a})")
    fake_lsf.wait_for(a, {"RUN"})
    assert fake_lsf.job(b)["status"] == "PEND" and fake_lsf.job(b)["deps"] == [["done", a]]
    fake_lsf.wait_for(b, {"DONE"}, timeout=10)
    assert fake_lsf.job(b)["start_time"] >= fake_lsf.job(a)["end_time"]
    f = submit(fake_lsf, "exit 1\n", *BROKER_ARGS)
    c = submit(fake_lsf, "true\n", *BROKER_ARGS, "-w", f"ended({f})")
    e = submit(fake_lsf, "true\n", *BROKER_ARGS, "-w", f"exit({f}) && done({a})")
    fake_lsf.wait_for(c, {"DONE"})
    fake_lsf.wait_for(e, {"DONE"})
    r = fake_lsf.run("bsub", *BROKER_ARGS, "-w", "done(999999)", stdin="true\n")
    assert r.returncode == 255 and "Dependency" in r.stderr
    r = fake_lsf.run("bsub", *BROKER_ARGS, "-w", "started(1)", stdin="true\n")
    assert r.returncode == 255 and "Bad dependency" in r.stderr


def test_runlimit_kill(fake_lsf):
    jid = submit(
        fake_lsf, "sleep 30\n", "-W", "1", "-env", "none", env={"CSUB_FAKE_LSF_MINUTE_S": "1"}
    )
    job = fake_lsf.wait_for(jid, {"EXIT"}, timeout=15)
    assert job["term_reason"] == "TERM_RUNLIMIT"
    assert job["end_time"] - job["start_time"] < 6


def test_directives_and_precedence(fake_lsf):
    jid = submit(
        fake_lsf,
        "#!/bin/sh\n#BSUB -n 3\n#BSUB -q short\n#BSUB -J fromscript\ntrue\n",
        "-J",
        "fromargv",
    )
    job = fake_lsf.wait_for(jid, {"DONE"})
    assert job["slots"] == 3 and job["queue"] == "short" and job["name"] == "fromargv"


def test_bsub_rejections(fake_lsf):
    r = fake_lsf.run("bsub", "-bogus", "x", stdin="true\n")
    assert r.returncode == 255 and "Unknown option -bogus" in r.stderr
    r = fake_lsf.run(
        "bsub", "-q", "nope", stdin="true\n", env={"CSUB_FAKE_LSF_QUEUES": "short,local"}
    )
    assert r.returncode == 255 and "Bad queue name" in r.stderr
    r = fake_lsf.run("bsub", stdin="true\n", env={"CSUB_FAKE_LSF_BSUB_FAIL": "LSF is down"})
    assert r.returncode == 255 and r.stderr.strip() == "LSF is down" and r.stdout == ""
    r = fake_lsf.run("bsub", "-Is", "bash", stdin="")
    assert r.returncode == 255


def test_parallel_submissions_get_distinct_ids(fake_lsf):
    with ThreadPoolExecutor(8) as ex:
        ids = list(ex.map(lambda _: submit(fake_lsf, "true\n", *BROKER_ARGS), range(10)))
    assert len(set(ids)) == 10
    for jid in ids:
        fake_lsf.wait_for(jid, {"DONE"})


def test_positional_command(fake_lsf, tmp_path):
    out = tmp_path / "o.txt"
    r = fake_lsf.run("bsub", "-o", str(out), "echo", "pos", "args")
    assert r.returncode == 0
    jid = r.stdout.split("<")[1].split(">")[0]
    fake_lsf.wait_for(jid, {"DONE"})
    assert out.read_text().startswith("pos args\n")


# --- fake sandbox ---------------------------------------------------------------------------


def run_sandbox(
    fake_sandbox: FakeSandbox, *args: str, cwd, env=None
) -> subprocess.CompletedProcess[str]:
    full_env = {**os.environ, "LC_ALL": "C", **fake_sandbox.env(), **(env or {})}
    return subprocess.run(
        [str(fake_sandbox.scripts_dir / "podman-run.sh"), *args],
        capture_output=True,
        text=True,
        cwd=cwd,
        env=full_env,
        timeout=30,
    )


def test_sandbox_records_and_runs(fake_sandbox, tmp_path, monkeypatch):
    monkeypatch.setenv("LSB_JOBID", "77")
    monkeypatch.setenv("PYTEST_LEAK", "1")
    r = run_sandbox(
        fake_sandbox,
        "--keep-id", "--image", "ghcr.io/x/y:1", "--rw", "/a", "--ro", "/b", "--ro", "/c.sock",
        "--gpu",
        "--", "sh", "-c", "echo out; echo err >&2; env; exit 7",
        cwd=tmp_path,
    )  # fmt: skip
    assert r.returncode == 7
    assert r.stdout.startswith("out\n") and r.stderr.strip() == "err"
    assert "PYTEST_LEAK" not in r.stdout and f"HOME={os.environ['HOME']}" in r.stdout
    assert "container=podman" in r.stdout
    rec = json.loads((tmp_path / "podman-run.json").read_text())
    assert (
        rec["image"] == "ghcr.io/x/y:1" and rec["rw"] == ["/a"] and rec["ro"] == ["/b", "/c.sock"]
    )
    assert rec["gpu"] is True and rec["keep_id"] is True and rec["allow"] == []
    assert rec["cmd"][:2] == ["sh", "-c"] and rec["env"]["LSB_JOBID"] == "77"
    assert fake_sandbox.invocations() == [rec]
    assert fake_sandbox.for_job("77") == rec


def test_sandbox_root_identity_without_keep_id(fake_sandbox, tmp_path):
    r = run_sandbox(fake_sandbox, "--", "sh", "-c", "echo $HOME", cwd=tmp_path)
    assert r.stdout == "/root\n"


def test_sandbox_allow_indirection_keeps_argv(fake_sandbox, tmp_path):
    r = run_sandbox(
        fake_sandbox, "--allow", "pypi.org", "--allow", "github.com",
        "--", "sh", "-c", 'printf "%s|" "$@"; echo; echo "$http_proxy $CSUB_FAKE_ALLOW_HOSTS"',
        "x", "a b", "it's",
        cwd=tmp_path,
    )  # fmt: skip
    assert r.returncode == 0, r.stderr
    assert r.stdout.splitlines()[0] == "a b|it's|"
    assert r.stdout.splitlines()[1] == "http://127.0.0.1:1 pypi.org,github.com"
    rec = fake_sandbox.invocations()[-1]
    assert rec["allow"] == ["pypi.org", "github.com"]


def test_sandbox_errors(fake_sandbox, tmp_path):
    r = run_sandbox(fake_sandbox, "--bogus", "--", "true", cwd=tmp_path)
    assert r.returncode == 1 and "Unknown option" in r.stderr
    r = run_sandbox(fake_sandbox, "--", cwd=tmp_path)
    assert r.returncode == 1 and "No command" in r.stderr
    r = run_sandbox(
        fake_sandbox, "--", "true", cwd=tmp_path, env={"CSUB_FAKE_SANDBOX_FAIL_RC": "125"}
    )
    assert r.returncode == 125 and "fake podman failure" in r.stderr
    assert fake_sandbox.invocations()[-1]["cmd"] == ["true"]  # recorded before failing


def test_sandbox_jsonl_appends(fake_sandbox, tmp_path):
    for _ in range(3):
        run_sandbox(fake_sandbox, "--", "true", cwd=tmp_path)
    assert len(fake_sandbox.invocations()) == 3


def test_wait_until_times_out():
    with pytest.raises(TimeoutError):
        wait_until(lambda: False, timeout=0.2, poll=0.05)
