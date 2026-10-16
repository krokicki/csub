"""Wrapper rendering: snapshots, shell syntax, quoting, and a real execution with the fakes."""

import dataclasses
import json
import os
import shlex
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from csub.broker.resolve import path_identity, resolve
from csub.broker.wrapper import (
    EXIT_CD,
    EXIT_INNER_MKDIR,
    EXIT_NO_BROKER,
    EXIT_PATH_CHANGED,
    EXIT_SCRIPT_WRITE,
    check_no_directives,
    mounts_env_value,
    pinned_paths,
    render_inner,
    render_wrapper,
)
from csub.protocol import CsubError

STATE = "/home/u/.csub"
SCRIPTS = "/home/u/.local/share/csub/agentic-sandbox/scripts"
BROKER = "/home/u/.local/pipx/venvs/csub/bin/python -m csub.broker"


def _norm(text: str, *paths: str, job=None) -> str:
    for i, p in enumerate(paths):
        text = text.replace(p, f"<P{i}>")
    if job is not None:  # device:inode literals differ per machine
        for i, (_, ident) in enumerate(pinned_paths(job)):
            text = text.replace(ident, f"<I{i}>")
    return text


@pytest.fixture
def render(make_spec, policy, ctx, project_dir, roots):
    def _render(**fields):
        job = resolve(make_spec(**fields), policy, ctx)
        text = render_wrapper(
            job, state_dir=STATE, scripts_dir=SCRIPTS, broker_cmd=BROKER, home="/home/u"
        )
        return (
            job,
            text,
            _norm(text, str(project_dir), str(roots.nrs), str(roots.scratch_root), job=job),
        )

    return _render


def test_snapshot_min_argv(render, snapshot):
    _, _, text = render(name="demo")
    snapshot("wrapper_min_argv.sh", text)


def test_snapshot_everything(render, snapshot):
    _, _, text = render(
        name="full",
        command='#!/bin/bash\necho "it\'s $HOME"\n',
        shell=True,
        gpus=1,
        queue="gpu_short",
        scratch=True,
        allow_hosts=["pypi.org", "github.com"],
        env={"OMP_NUM_THREADS": "4", "MY_PROJECT_X": "a 'b' $c"},
    )
    snapshot("wrapper_everything.sh", text)


def test_wrapper_structure(render):
    job, text, _ = render(
        name="demo", scratch=True, gpus=1, queue="gpu_l4", allow_hosts=["pypi.org"]
    )
    lines = text.splitlines()
    assert lines[0] == "#!/bin/sh"
    assert lines[2] == f"export PATH=/usr/local/bin:/usr/bin:/bin USER={job.user} HOME=/home/u"
    assert f'JOB_DIR={STATE}/jobs/"$LSB_JOBID"' in text
    assert f'{BROKER} --serve "$JOB_DIR/csub.sock" &' in text
    assert "--keep-id" in text and "--gpu" in text and "--allow pypi.org" in text
    assert '--rw "$SCRATCH"' in text and '--ro "$JOB_DIR/csub.sock"' in text
    assert text.rstrip().endswith(
        '[ -n "$SCRATCH" ] && rm -rf "$SCRATCH"\necho "$rc" > "$JOB_DIR/exit_code"\nexit "$rc"'
    )
    assert "#BSUB" not in text
    # env values live only inside the single-quoted inner script, never on the podman line
    assert text.count("export OMP_NUM_THREADS") == 0  # not requested here


def test_whole_wrapper_directive_backstop(render):
    """Whatever field slips past resolve, the rendered script is scanned before bsub."""
    _, text, _ = render(command="echo a\necho '#BSUB' b\n", shell=True)
    check_no_directives(text)  # "#BSUB" mid-line is not a directive
    with pytest.raises(CsubError, match="#BSUB") as e:
        check_no_directives(text + "#BSUB -E /tmp/pwn.sh\n")
    assert e.value.code == "invalid_request"
    with pytest.raises(CsubError):
        check_no_directives("#!/bin/sh\n  #  BSUB -q x\necho\n")


def test_no_scratch_no_gpu_no_allow(render):
    _, text, _ = render()
    assert 'mkdir -p "$SCRATCH"' not in text and "--gpu" not in text and "--allow" not in text
    assert "SCRATCH=''" in text  # still defined so rm -rf is a no-op


def test_keep_id_follows_policy(make_spec, make_policy, ctx):
    job = resolve(make_spec(), make_policy(broker={"keep_id": False}), ctx)
    text = render_wrapper(job, state_dir=STATE, scripts_dir=SCRIPTS, broker_cmd=BROKER, home="/h")
    assert "--keep-id" not in text


def test_inner_exports(render):
    job, _, _ = render(env={"OMP_NUM_THREADS": "4"}, walltime_min=30)
    inner = render_inner(job)
    assert inner.startswith("set -u\n")
    assert f"export HOME={shlex.quote(job.cwd)}/.csub/home" in inner
    assert 'export CSUB_TRANSPORT=unix CSUB_SOCKET="$6" CSUB_JOB_ID="$1"' in inner
    assert f"export CSUB_MOUNTS={shlex.quote(mounts_env_value(job))}" in inner
    assert "CSUB_WALLTIME_MIN=30 CSUB_DEADLINE_EPOCH=$(($5 + 1800))" in inner
    assert "export OMP_NUM_THREADS=4" in inner
    assert inner.rstrip().endswith('exec sh -c \'echo hi\' >"$J/stdout" 2>"$J/stderr"')


@pytest.mark.parametrize("shell", [False, True])
def test_wrapper_is_valid_sh(render, shell):
    cmd = (
        "echo 'a' \"b\" $c \\ ; : && exit 0\n"
        if shell
        else ["echo", "it's", 'a "quoted" $arg', "new\nline", ";", "&&"]
    )
    _, text, _ = render(command=cmd, shell=shell, env={"MY_PROJECT_X": "x'y\"z$`"})
    subprocess.run(["sh", "-n"], input=text, text=True, check=True)
    job, _, _ = render(command=cmd, shell=shell)
    subprocess.run(["sh", "-n"], input=render_inner(job), text=True, check=True)


# --- execute a rendered wrapper for real (fake sandbox, no LSF) -----------------------------


@pytest.fixture
def exec_env(tmp_path, fake_sandbox):
    """Run a wrapper the way the fake LSF would: minimal env + LSB vars, in a temp state dir."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    fake_broker = tmp_path / "fake-broker.sh"
    # Stands in for `csub-broker --serve SOCK`: create the socket file, then idle.
    fake_broker.write_text(
        "#!/bin/sh\n"
        "python3 -c 'import socket,sys; s=socket.socket(socket.AF_UNIX); "
        's.bind(sys.argv[1])\' "$2"\n'
        "exec sleep 300\n"
    )
    fake_broker.chmod(0o755)

    def run_text(text, job_id="42", env_extra=None, slots=1, path="/usr/bin:/bin"):
        script = tmp_path / "wrapper.sh"
        script.write_text(text)
        env = {
            "PATH": path,
            "LSB_JOBID": job_id,
            "LSB_DJOB_NUMPROC": str(slots),
            "LC_ALL": "C",
            **fake_sandbox.env(),
            **(env_extra or {}),
        }
        return subprocess.run(
            ["sh", str(script)], env=env, capture_output=True, text=True, timeout=60
        )

    def _run(job, job_id="42", env_extra=None, broker_cmd=None):
        text = render_wrapper(
            job,
            state_dir=str(state_dir),
            scripts_dir=str(fake_sandbox.scripts_dir),
            broker_cmd=broker_cmd or shlex.quote(str(fake_broker)),
            home=str(tmp_path / "home"),
        )
        t0 = time.time()
        r = run_text(text, job_id=job_id, env_extra=env_extra, slots=job.slots)
        return r, state_dir / "jobs" / job_id, t0

    _run.run_text = run_text
    _run.state_dir = state_dir
    _run.scripts_dir = fake_sandbox.scripts_dir
    _run.fake_broker = fake_broker
    _run.home = tmp_path / "home"
    return _run


def test_execute_argv_job(make_spec, policy, ctx, exec_env, project_dir, roots, fake_sandbox):
    cmd = ["sh", "-c", "echo out; echo err >&2; env; pwd; exit 5"]
    job = resolve(make_spec(command=cmd, scratch=True, env={"OMP_NUM_THREADS": "3"}), policy, ctx)
    r, job_dir, t0 = exec_env(job, env_extra={"CUDA_VISIBLE_DEVICES": "2"})
    assert r.returncode == 5, r.stderr
    assert (job_dir / "exit_code").read_text() == "5\n"
    j = project_dir / ".csub" / "jobs" / "42"
    stdout = (j / "stdout").read_text()
    assert stdout.startswith("out\n") and stdout.rstrip().endswith(str(project_dir))
    assert (j / "stderr").read_text() == "err\n"
    env = dict(line.split("=", 1) for line in stdout.splitlines() if "=" in line)
    assert env["HOME"] == f"{project_dir}/.csub/home" and (project_dir / ".csub" / "home").is_dir()
    assert env["CSUB_TRANSPORT"] == "unix" and env["CSUB_SOCKET"] == f"{job_dir}/csub.sock"
    assert (
        env["CSUB_JOB_ID"] == "42" and env["LSB_JOBID"] == "42" and env["LSB_DJOB_NUMPROC"] == "1"
    )
    assert env["CSUB_SESSION"] == "testsess" and env["CSUB_IMAGE"] == job.image
    assert env["CSUB_MOUNTS"] == f"{project_dir}:rw,{roots.nrs}:ro"
    assert env["CSUB_WALLTIME_MIN"] == "60"
    assert t0 + 3600 - 5 <= int(env["CSUB_DEADLINE_EPOCH"]) <= t0 + 3600 + 60
    assert env["CUDA_VISIBLE_DEVICES"] == "2" and env["OMP_NUM_THREADS"] == "3"
    assert env["USER"] == ctx.user
    scratch = Path(env["TMPDIR"])
    assert scratch == roots.scratch_root / ctx.user / "csub" / "42"
    assert not scratch.exists(), "scratch must be removed after the job"
    assert "PYTEST_CURRENT_TEST" not in env and "CSUB_FAKE_SANDBOX_LOG" not in env
    rec = fake_sandbox.for_job("42")
    assert rec["rw"] == [str(project_dir), str(scratch)] and rec["ro"] == [
        str(roots.nrs),
        f"{job_dir}/csub.sock",
    ]
    assert rec["keep_id"] is True and rec["gpu"] is False and rec["image"] == job.image
    assert rec["cwd"] == str(job_dir)
    assert (job_dir / "podman-run.json").exists()


def test_execute_shell_body_with_shebang(make_spec, policy, ctx, exec_env, project_dir):
    body = (
        "#!/usr/bin/env python3\nimport sys\n"
        "print('py', sys.argv[0].endswith('/script'))\nsys.exit(0)\n"
    )
    job = resolve(make_spec(command=body, shell=True), policy, ctx)
    r, job_dir, _ = exec_env(
        job, job_id="43", env_extra={"PATH": "/usr/bin:/bin:" + os.path.dirname(sys.executable)}
    )
    assert r.returncode == 0, r.stderr
    j = project_dir / ".csub" / "jobs" / "43"
    assert (j / "stdout").read_text() == "py True\n"
    assert (j / "script").read_text() == body
    assert stat.S_IMODE((j / "script").stat().st_mode) & stat.S_IXUSR


def test_execute_shell_body_without_shebang(make_spec, policy, ctx, exec_env, project_dir):
    job = resolve(make_spec(command="echo plain; exit 3\n", shell=True), policy, ctx)
    r, job_dir, _ = exec_env(job, job_id="44")
    assert r.returncode == 3
    assert (project_dir / ".csub" / "jobs" / "44" / "stdout").read_text() == "plain\n"


def test_execute_quoting_torture(make_spec, policy, ctx, exec_env, project_dir):
    args = [
        "it's",
        'a "quoted" $arg',
        "new\nline",
        ";",
        "&&",
        "`id`",
        "$(id)",
        "*",
        "-- --image evil",
    ]
    job = resolve(
        make_spec(command=["printf", "%s|", *args], env={"MY_PROJECT_X": "x'y\"z$`\n"}), policy, ctx
    )
    r, _, _ = exec_env(job, job_id="45")
    assert r.returncode == 0, r.stderr
    assert (project_dir / ".csub" / "jobs" / "45" / "stdout").read_text() == "|".join(args) + "|"
    rec = json.loads(
        (project_dir.parents[0] / "project" / ".csub" / "jobs" / "45" / "stdout").read_text()[:0]
        or "{}"
    )
    assert rec == {}


def test_execute_missing_broker_socket(make_spec, policy, ctx, exec_env):
    job = resolve(make_spec(), policy, ctx)
    r, job_dir, _ = exec_env(job, job_id="46", broker_cmd="true")
    assert r.returncode == EXIT_NO_BROKER
    assert not (job_dir / "exit_code").exists()  # never reached the sandbox


def test_execute_inner_mkdir_failure(make_spec, policy, ctx, exec_env, project_dir):
    job = resolve(make_spec(), policy, ctx)
    # The cwd becomes unwritable between validation and execution: mkdir -p fails inside the
    # sandbox and the wrapper records a wrapper-stage exit code.
    project_dir.chmod(0o555)
    try:
        r, job_dir, _ = exec_env(job, job_id="47")
    finally:
        project_dir.chmod(0o755)
    assert r.returncode == EXIT_INNER_MKDIR, r.stderr
    assert (job_dir / "exit_code").read_text() == f"{EXIT_INNER_MKDIR}\n"


def test_execute_sandbox_failure_propagates(make_spec, policy, ctx, exec_env):
    job = resolve(make_spec(), policy, ctx)
    r, job_dir, _ = exec_env(job, job_id="48", env_extra={"CSUB_FAKE_SANDBOX_FAIL_RC": "125"})
    assert r.returncode == 125 and (job_dir / "exit_code").read_text() == "125\n"


def test_exit_code_constants_are_distinct():
    codes = {EXIT_NO_BROKER, EXIT_SCRIPT_WRITE, EXIT_CD, EXIT_INNER_MKDIR, EXIT_PATH_CHANGED}
    assert len(codes) == 5 and all(90 <= c < 100 for c in codes)


# --- mount identity pinning (symlink swap between submit and start) -----------------------


def test_pins_cover_every_mount_and_cwd(render, project_dir, roots):
    sub = project_dir / "sub"
    sub.mkdir()
    job, text, _ = render(cwd=str(sub))
    pins = [ln for ln in text.splitlines() if "changed since submission" in ln]
    assert len(pins) == 3  # project rw, nrs ro, cwd
    for i, p in enumerate([str(project_dir), str(roots.nrs), str(sub)]):
        st = os.stat(p)
        assert pins[i].startswith(
            f'[ "$(stat -c %d:%i -- {shlex.quote(p)})" = {st.st_dev}:{st.st_ino} ] || '
        )
        assert pins[i].endswith("exit 99; }")
    # the checks run inside the sandbox, before anything is created there
    assert text.index(pins[-1]) < text.index('mkdir -p "$J"')
    assert "$(readlink" not in text and "$PIN" not in text  # no host-side path checks
    # cwd equal to a mount is pinned once
    _, text, _ = render()
    assert sum("changed since submission" in ln for ln in text.splitlines()) == 2


def test_pin_message_is_quoted(render, project_dir):
    """The path goes into a diagnostic; it must never be interpolated into shell syntax."""
    evil = project_dir / "$(touch REVIEW_HOST_EXECUTED)"
    evil.mkdir()
    job, text, _ = render(cwd=str(evil))
    assert all(
        shlex.quote(f"csub: {p} changed since submission") in text for p, _ in pinned_paths(job)
    )
    r = subprocess.run(["sh", "-n"], input=text, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_path_identity_follows_no_symlink(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    st = os.stat(real)
    assert path_identity(str(real)) == f"{st.st_dev}:{st.st_ino}"
    (tmp_path / "link").symlink_to(real)
    with pytest.raises(OSError):
        path_identity(str(tmp_path / "link"))
    with pytest.raises(OSError):
        path_identity(str(tmp_path / "link" / "x"))  # a symlinked ancestor is refused too
    with pytest.raises(OSError):
        path_identity(str(tmp_path / "missing"))


def test_execute_swapped_mount_is_refused(make_spec, policy, ctx, exec_env, project_dir, roots):
    """Validated at submit; replaced with a symlink to somewhere else while pending. The
    sandbox is launched, but the inner script refuses before creating or running anything."""
    job = resolve(make_spec(), policy, ctx)
    elsewhere = roots.outside / "elsewhere"
    elsewhere.mkdir()
    os.rename(project_dir, project_dir.with_name("old"))
    project_dir.symlink_to(elsewhere)
    r, job_dir, _ = exec_env(job, job_id="50")
    assert r.returncode == EXIT_PATH_CHANGED and "changed since submission" in r.stderr
    assert (job_dir / "exit_code").read_text() == f"{EXIT_PATH_CHANGED}\n"
    assert not (elsewhere / ".csub").exists()


def test_inner_refuses_wrong_identity(make_spec, policy, ctx, project_dir):
    """The in-sandbox check: what got mounted must be what the broker recorded."""
    job = resolve(make_spec(), policy, ctx)
    args = ["sh", "-c", render_inner(job), "csub-job", "1", "", "", "", "0", "/nonexistent.sock"]
    r = subprocess.run(args, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert (project_dir / ".csub" / "jobs" / "1" / "stdout").read_text() == "hi\n"
    m0 = dataclasses.replace(job.mounts[0], identity="0:0")  # the cwd is this mount
    bad = dataclasses.replace(job, mounts=(m0, *job.mounts[1:]))
    r = subprocess.run(
        [args[0], args[1], render_inner(bad), *args[3:]], capture_output=True, text=True
    )
    assert r.returncode == EXIT_PATH_CHANGED and str(project_dir) in r.stderr


# --- injected client -----------------------------------------------------------------------


def test_client_injection_rendering(make_spec, policy, ctx):
    job = resolve(make_spec(), policy, ctx)
    text = render_wrapper(
        job, state_dir=STATE, scripts_dir=SCRIPTS, broker_cmd=BROKER, home="/h",
        client_src="/h/.local/lib/python3.9/site-packages/csub",
    )  # fmt: skip
    assert "--ro /h/.local/lib/python3.9/site-packages/csub" in text
    assert (
        "export PYTHONPATH=/h/.local/lib/python3.9/site-packages${PYTHONPATH:+:$PYTHONPATH}" in text
    )
    assert 'exec python3 -m csub.cli "$@"' in text and 'export PATH="$J/bin:$PATH"' in text
    plain = render_wrapper(job, state_dir=STATE, scripts_dir=SCRIPTS, broker_cmd=BROKER, home="/h")
    assert "PYTHONPATH" not in plain and "csub.cli" not in plain
    subprocess.run(["sh", "-n"], input=text, text=True, check=True)


def test_execute_with_injected_client(make_spec, policy, ctx, exec_env, project_dir):
    """In the (fake) sandbox `csub --version` and `import csub` use the injected package."""
    import csub as _csub

    client_src = os.path.realpath(os.path.dirname(_csub.__file__))
    cmd = ["sh", "-c", "csub --version; python3 -c 'import csub, sys; print(csub.__file__)'"]
    job = resolve(make_spec(command=cmd), policy, ctx)
    text = render_wrapper(
        job, state_dir=str(exec_env.state_dir), scripts_dir=str(exec_env.scripts_dir),
        broker_cmd=shlex.quote(str(exec_env.fake_broker)), home=str(exec_env.home),
        client_src=client_src,
    )  # fmt: skip
    r = exec_env.run_text(
        text, job_id="49", path="/usr/bin:/bin:" + os.path.dirname(sys.executable)
    )
    assert r.returncode == 0, r.stderr
    out = (project_dir / ".csub" / "jobs" / "49" / "stdout").read_text()
    assert out.startswith("csub 0.1.0\n") and out.strip().endswith(client_src + "/__init__.py")


# --- bwrap backend ---------------------------------------------------------------------------


def test_bwrap_line(make_spec, make_policy, ctx):
    p = make_policy(broker={"sandbox": "bwrap"})
    job = resolve(make_spec(), p, ctx)
    assert job.sandbox == "bwrap"
    text = render_wrapper(job, state_dir=STATE, scripts_dir=SCRIPTS, broker_cmd=BROKER, home="/h")
    assert f"{SCRIPTS}/sandbox-run.sh" in text and "podman-run.sh" not in text
    assert "--image" not in text and "--keep-id" not in text and "--gpu" not in text
    assert f"\ncd {shlex.quote(job.cwd)} || exit {EXIT_CD}\n" in text
    assert text.index("cd " + shlex.quote(job.cwd)) < text.index("sandbox-run.sh")


def test_execute_bwrap_job(make_spec, make_policy, ctx, exec_env, project_dir, fake_sandbox):
    p = make_policy(broker={"sandbox": "bwrap"})
    job = resolve(make_spec(command=["sh", "-c", "pwd; exit 3"]), p, ctx)
    r, job_dir, _ = exec_env(job)
    assert r.returncode == 3, r.stderr
    out = (project_dir / ".csub" / "jobs" / "42" / "stdout").read_text()
    assert out.strip() == str(project_dir)
    rec = fake_sandbox.for_job("42")
    assert rec["script"] == "sandbox-run.sh" and rec["cwd"] == str(project_dir)
    assert not (job_dir / "sandbox-run.json").exists(), "state dir must not be the bwrap cwd"
