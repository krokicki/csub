"""csub-mcp against an in-memory MCP client session and a FakeClient."""

import json

import pytest

pytest.importorskip("mcp")
import anyio  # noqa: E402
from mcp.client.session import ClientSession  # noqa: E402
from mcp.shared.memory import create_client_server_memory_streams  # noqa: E402

from csub.client.api import Logs, WaitEntry, WaitResult  # noqa: E402
from csub.mcp.server import build_server  # noqa: E402
from csub.mcp.tools import TOOLS  # noqa: E402
from csub.protocol import (
    CsubError,
    JobStatus,
    KillResult,
    ProbeResult,
    SubmitResult,
    jobspec_schema,
)  # noqa: E402


class FakeClient:
    def __init__(self):
        self.calls = []
        self.error = None

    def probe(self):
        return ProbeResult(broker_version="0.1.0", user="u", queues={"short": {}})

    def submit(self, spec):
        self.calls.append(("submit", spec))
        if self.error:
            raise self.error
        return SubmitResult(job_id="42", queue="short", slots=1, walltime_min=60)

    def status(self, job_ids=None):
        self.calls.append(("status", job_ids))
        return [JobStatus("42", "RUN")]

    def wait(self, job_ids, *, timeout_s=None, tail_lines=50):
        self.calls.append(("wait", list(job_ids), timeout_s, tail_lines))
        return WaitResult(
            jobs=[WaitEntry(JobStatus("42", "DONE", exit_code=0, cwd="/w"), stdout_tail="hi\n")],
            timed_out=False,
            elapsed_s=2.0,
        )

    def kill(self, job_ids=None):
        self.calls.append(("kill", job_ids))
        return KillResult(killed=list(job_ids or []), already_finished=[])

    def logs(self, job_id, *, stream="both", tail=None, cwd=None):
        self.calls.append(("logs", job_id, stream, tail))
        return Logs(job_id=job_id, job_dir="/w/.csub/jobs/42", stdout="out\n", stderr="")


def run_session(fake, scenario):
    """Run `scenario(session)` against build_server(fake) over in-memory streams."""

    async def go():
        server = build_server(lambda: fake)
        async with create_client_server_memory_streams() as (client_streams, server_streams):
            async with anyio.create_task_group() as tg:
                tg.start_soon(
                    server.run,
                    server_streams[0],
                    server_streams[1],
                    server.create_initialization_options(),
                )
                async with ClientSession(client_streams[0], client_streams[1]) as session:
                    await session.initialize()
                    result = await scenario(session)
                tg.cancel_scope.cancel()
        return result

    return anyio.run(go)


def test_list_tools_matches_definitions():
    async def scenario(session):
        return await session.list_tools()

    tools = run_session(FakeClient(), scenario).tools
    assert [t.name for t in tools] == list(TOOLS)
    by_name = {t.name: t for t in tools}
    assert by_name["csub_submit"].input_schema == jobspec_schema(exclude=("mounts", "session"))
    assert "mounts" not in by_name["csub_submit"].input_schema["properties"]
    assert by_name["csub_kill"].annotations.destructive_hint is True
    assert by_name["csub_probe"].annotations.read_only_hint is True
    assert all(t.description for t in tools)


def test_probe_submit_wait_logs():
    fake = FakeClient()

    async def scenario(session):
        probe = await session.call_tool("csub_probe", {})
        submit = await session.call_tool(
            "csub_submit", {"command": ["python", "x.py"], "cpus": 2, "walltime_min": 30}
        )
        wait = await session.call_tool(
            "csub_wait", {"job_ids": ["42"], "timeout_s": 5, "tail_lines": 3}
        )
        logs = await session.call_tool("csub_logs", {"job_id": "42", "stream": "stdout"})
        status = await session.call_tool("csub_status", {})
        kill = await session.call_tool("csub_kill", {"job_ids": ["42"]})
        return probe, submit, wait, logs, status, kill

    probe, submit, wait, logs, status, kill = run_session(fake, scenario)
    assert not probe.is_error and probe.structured_content["user"] == "u"
    assert (
        submit.structured_content["job_id"] == "42"
        and json.loads(submit.content[0].text)["job_id"] == "42"
    )
    spec = fake.calls[0][1]
    assert spec.command == ("python", "x.py") and spec.cpus == 2 and spec.walltime_min == 30
    assert wait.structured_content["jobs"][0]["stdout_tail"] == "hi\n" and fake.calls[1] == (
        "wait",
        ["42"],
        5,
        3,
    )
    assert logs.structured_content["stdout"] == "out\n" and fake.calls[2] == (
        "logs",
        "42",
        "stdout",
        200,
    )
    assert status.structured_content["jobs"][0]["state"] == "RUN" and fake.calls[3] == (
        "status",
        None,
    )
    assert kill.structured_content["killed"] == ["42"]


def test_argument_validation_and_errors():
    fake = FakeClient()

    async def scenario(session):
        bad_type = await session.call_tool("csub_submit", {"command": ["x"], "cpus": "4"})
        unknown = await session.call_tool("csub_submit", {"command": ["x"], "bogus": 1})
        no_ids = await session.call_tool("csub_kill", {})
        fake.error = CsubError("policy_violation", "too expensive")
        policy = await session.call_tool("csub_submit", {"command": ["x"]})
        return bad_type, unknown, no_ids, policy

    bad_type, unknown, no_ids, policy = run_session(fake, scenario)
    assert bad_type.is_error and "cpus" in bad_type.content[0].text
    assert unknown.is_error and "bogus" in unknown.content[0].text
    assert no_ids.is_error and no_ids.structured_content["error"]["code"] == "invalid_request"
    assert policy.is_error and policy.structured_content["error"] == {
        "code": "policy_violation",
        "message": "too expensive",
    }
    assert policy.content[0].text == "policy_violation: too expensive"
