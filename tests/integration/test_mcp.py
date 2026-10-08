"""csub-mcp over real stdio, through the real broker and fakes."""

import shlex
import sys

import pytest

pytest.importorskip("mcp")
import anyio  # noqa: E402
from mcp.client.session import ClientSession  # noqa: E402
from mcp.client.stdio import StdioServerParameters, stdio_client  # noqa: E402

pytestmark = pytest.mark.integration


def test_mcp_stdio_end_to_end(broker_cmd, broker_env, project_dir, roots):
    env = {
        **broker_env,
        "CSUB_TRANSPORT": "local",
        "CSUB_BROKER_CMD": shlex.join(broker_cmd),
        "CSUB_MOUNTS": f"{project_dir}:rw,{roots.nrs}:ro",
        "CSUB_SESSION": "mcpsess",
        "CSUB_WAIT_MAX_POLL_S": "0.5",
    }
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "csub.mcp"], env=env, cwd=str(project_dir)
    )

    async def go():
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                probe = await session.call_tool("csub_probe", {})
                submit = await session.call_tool(
                    "csub_submit", {"command": ["sh", "-c", "echo via-mcp"], "walltime_min": 12}
                )
                jid = submit.structured_content["job_id"]
                wait = await session.call_tool("csub_wait", {"job_ids": [jid], "timeout_s": 60})
                logs = await session.call_tool("csub_logs", {"job_id": jid})
                denied = await session.call_tool(
                    "csub_submit", {"command": ["true"], "allow_hosts": ["evil.example"]}
                )
                return tools, probe, submit, wait, logs, denied

    tools, probe, submit, wait, logs, denied = anyio.run(go)
    assert {t.name for t in tools.tools} == {
        "csub_probe",
        "csub_submit",
        "csub_status",
        "csub_wait",
        "csub_kill",
        "csub_logs",
    }
    assert probe.structured_content["queues"]["short"]["max_walltime_min"] == 60
    assert (
        submit.structured_content["queue"] == "short"
        and submit.structured_content["estimated_max_cost_usd"] == 0.01
    )
    assert (
        wait.structured_content["jobs"][0]["state"] == "DONE"
        and wait.structured_content["jobs"][0]["stdout_tail"] == "via-mcp\n"
    )
    assert logs.structured_content["stdout"] == "via-mcp\n"
    assert denied.is_error and denied.structured_content["error"]["code"] == "policy_violation"
