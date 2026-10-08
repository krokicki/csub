import json

import pytest

from csub.client.api import Client, self_job
from csub.client.config import ClientConfig, ConfigError
from csub.protocol import CsubError, JobSpec, Mount


class FakeTransport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def call(self, request):
        self.requests.append(json.loads(json.dumps(request)))
        r = self.responses.pop(0)
        return r if isinstance(r, dict) else r()


def ok(**payload):
    return {"protocol": 1, "ok": True, **payload}


def err(code, message="m"):
    return {"protocol": 1, "ok": False, "error": {"code": code, "message": message}}


CFG = ClientConfig(
    transport="local", mounts=(Mount("/g/p", "rw"), Mount("/n/d", "ro")), image="ghcr.io/x/y:1"
)


def test_submit_completes_spec(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    t = FakeTransport(ok(job_id="5", queue="short", slots=1, walltime_min=60))
    c = Client(CFG, t)
    r = c.submit(command=["true"], cpus=2)
    assert r.job_id == "5" and r.queue == "short"
    job = t.requests[0]["job"]
    assert job["cwd"] == str(tmp_path.resolve()) and job["cpus"] == 2
    assert job["mounts"] == [{"path": "/g/p", "mode": "rw"}, {"path": "/n/d", "mode": "ro"}]
    assert job["image"] == "ghcr.io/x/y:1" and len(job["session"]) == 12
    assert t.requests[0]["protocol"] == 1 and t.requests[0]["op"] == "submit"


def test_submit_respects_explicit_fields():
    t = FakeTransport(ok(job_id="6", queue="local", slots=1, walltime_min=90))
    spec = JobSpec.from_dict(
        {"command": ["x"], "cwd": "/g/p/sub", "session": "mine", "image": "ghcr.io/a/b:2"}
    )
    Client(CFG.with_(session="env-session"), t).submit(spec, name="n")
    job = t.requests[0]["job"]
    assert (
        job["cwd"] == "/g/p/sub"
        and job["session"] == "mine"
        and job["image"] == "ghcr.io/a/b:2"
        and job["name"] == "n"
    )


def test_submit_requires_mounts():
    with pytest.raises(ConfigError, match="CSUB_MOUNTS"):
        Client(ClientConfig(transport="local"), FakeTransport()).submit(command=["x"])


def test_errors_are_raised():
    t = FakeTransport(err("policy_violation", "nope"))
    with pytest.raises(CsubError, match="nope") as e:
        Client(CFG, t).submit(command=["x"], cwd="/g/p")
    assert e.value.code == "policy_violation"


def test_status_kill_probe():
    t = FakeTransport(
        ok(jobs=[{"job_id": "1", "state": "RUN"}]),
        ok(jobs=[]),
        ok(killed=["1"], already_finished=[]),
        ok(killed=[], already_finished=["2"]),
        ok(broker_version="0.1.0", user="u", queues={"short": {}}),
    )
    c = Client(CFG.with_(session="s"), t)
    assert c.status(["1"])[0].state == "RUN" and t.requests[0] == {
        "protocol": 1,
        "op": "status",
        "job_ids": ["1"],
    }
    assert c.status() == [] and t.requests[1] == {"protocol": 1, "op": "status", "session": "s"}
    assert c.kill(["1"]).killed == ["1"] and t.requests[2]["job_ids"] == ["1"]
    assert c.kill().already_finished == ["2"] and t.requests[3] == {
        "protocol": 1,
        "op": "kill",
        "session": "s",
    }
    assert c.probe().user == "u"


def test_wait_backoff_and_tails(tmp_path):
    jd = tmp_path / ".csub" / "jobs" / "9"
    jd.mkdir(parents=True)
    (jd / "stdout").write_text("".join(f"line {i}\n" for i in range(100)))
    (jd / "stderr").write_text("oops\n")
    cwd = str(tmp_path)
    pend = ok(jobs=[{"job_id": "9", "state": "PEND", "cwd": cwd}])
    run = ok(jobs=[{"job_id": "9", "state": "RUN", "cwd": cwd}])
    done = ok(jobs=[{"job_id": "9", "state": "DONE", "exit_code": 0, "cwd": cwd}])
    t = FakeTransport(pend, pend, run, run, done)
    sleeps = []
    clock = [0.0]

    def sleep(s):
        sleeps.append(s)
        clock[0] += s

    r = Client(CFG, t).wait(["9"], tail_lines=3, sleep=sleep, clock=lambda: clock[0])
    assert sleeps == [1.0, 2.0, 4.0, 8.0] and not r.timed_out and r.all_done
    e = r.jobs[0]
    assert e.stdout_tail == "line 97\nline 98\nline 99\n" and e.stderr_tail == "oops\n"
    assert e.stdout_path == str(jd / "stdout")
    assert r.to_dict()["jobs"][0]["state"] == "DONE"


def test_wait_timeout_and_cap():
    pend = ok(jobs=[{"job_id": "9", "state": "PEND"}])
    t = FakeTransport(*[pend] * 10)
    sleeps = []
    clock = [0.0]

    def sleep(s):
        sleeps.append(s)
        clock[0] += s

    r = Client(CFG.with_(wait_max_poll_s=3), t).wait(
        ["9"], timeout_s=10, sleep=sleep, clock=lambda: clock[0]
    )
    assert r.timed_out and not r.all_done and sum(sleeps) == 10 and max(sleeps) <= 3
    assert r.jobs[0].status.state == "PEND" and r.jobs[0].stdout_tail == ""


def test_logs(tmp_path):
    jd = tmp_path / ".csub" / "jobs" / "3"
    jd.mkdir(parents=True)
    (jd / "stdout").write_text("a\nb\nc\n")
    t = FakeTransport(ok(jobs=[{"job_id": "3", "state": "DONE", "cwd": str(tmp_path)}]))
    c = Client(CFG, t)
    lg = c.logs("3", tail=2)
    assert lg.stdout == "b\nc\n" and lg.stderr == "" and lg.job_dir == str(jd)
    lg = c.logs("3", cwd=str(tmp_path), stream="stdout")
    assert lg.stdout == "a\nb\nc\n" and lg.stderr is None and t.requests == t.requests[:1]
    with pytest.raises(CsubError, match="no output directory"):
        c.logs("4", cwd=str(tmp_path))


def test_self_job():
    assert self_job({}) is None
    sj = self_job(
        {
            "CSUB_JOB_ID": "12",
            "CSUB_WALLTIME_MIN": "30",
            "CSUB_DEADLINE_EPOCH": "1000",
            "CSUB_SESSION": "s",
        }
    )
    assert (
        sj.job_id == "12"
        and sj.walltime_min == 30
        and sj.seconds_left(now=400) == 600
        and sj.session == "s"
    )
    assert self_job({"CSUB_JOB_ID": "1"}).seconds_left() is None
