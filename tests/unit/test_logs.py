"""Job output is read through an agent-written tree: no symlinks, regular files, bounded."""

import os

import pytest

from csub.client.logs import MAX_BYTES, read_logs, read_output
from csub.protocol import CsubError


@pytest.fixture
def tree(tmp_path):
    cwd = tmp_path / "proj"
    jd = cwd / ".csub" / "jobs" / "7"
    jd.mkdir(parents=True)
    (jd / "stdout").write_text("a\nb\nc\n")
    secret = tmp_path / "secret"
    secret.write_text("key\n")
    return cwd, jd, secret


def test_reads_and_tails(tree):
    cwd, jd, _ = tree
    assert read_logs(str(cwd), "7", tail=2)["stdout"] == "b\nc\n"
    assert read_logs(str(cwd), "7")["stderr"] == ""  # missing stream is empty, not an error
    assert read_output(str(cwd), "7", "stdout", None) == "a\nb\nc\n"
    assert read_output(str(cwd), "8", "stdout", 5) == ""  # no job dir yet
    with pytest.raises(CsubError, match="no output directory"):
        read_logs(str(cwd), "8")


@pytest.mark.parametrize("where", ["file", "jobdir", "jobs", "csub", "cwd"])
def test_symlinks_are_refused(tree, tmp_path, where):
    cwd, jd, secret = tree
    if where == "file":
        (jd / "stdout").unlink()
        (jd / "stdout").symlink_to(secret)
    else:
        target = {"jobdir": jd, "jobs": jd.parent, "csub": jd.parent.parent, "cwd": cwd}[where]
        alt = tmp_path / "alt"
        os.rename(target, alt)
        target.symlink_to(alt)
        (secret.parent / "alt").joinpath(".csub", "jobs", "7").mkdir(parents=True, exist_ok=True)
    with pytest.raises(CsubError, match="refusing to read"):
        read_logs(str(cwd), "7")
    with pytest.raises(CsubError, match="refusing to read"):
        read_output(str(cwd), "7", "stdout", 10)


def test_not_a_regular_file(tree):
    cwd, jd, _ = tree
    (jd / "stderr").mkdir()
    with pytest.raises(CsubError, match="not a regular file"):
        read_logs(str(cwd), "7", stream="stderr")


def test_read_is_bounded(tree):
    cwd, jd, _ = tree
    with open(jd / "stdout", "wb") as f:
        f.write(b"x" * (2 * MAX_BYTES))  # no newline at all
        f.write(b"\nlast\n")
    out = read_logs(str(cwd), "7", tail=1)["stdout"]
    assert out == "last\n"
    whole = read_logs(str(cwd), "7")["stdout"]
    assert len(whole) == MAX_BYTES and whole.endswith("\nlast\n")
