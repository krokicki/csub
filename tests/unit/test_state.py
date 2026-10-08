import os
import stat

from csub.broker.state import JobRecord, StateDir


def _rec(job_id="1001", session="s1", user="u"):
    return JobRecord(
        job_id=job_id,
        user=user,
        session=session,
        name="n",
        queue="short",
        cwd="/p",
        image="i",
        submitted_at=1.0,
    )


def test_layout_and_permissions(tmp_path):
    s = StateDir(tmp_path / "state")
    for d in (s.root, s.jobs_dir, s.pending_dir):
        assert d.is_dir() and stat.S_IMODE(d.stat().st_mode) == 0o700
    token, pdir = s.new_pending()
    assert pdir.parent == s.pending_dir and stat.S_IMODE(pdir.stat().st_mode) == 0o700
    s.write_private(pdir / "wrapper.sh", "#!/bin/sh\n")
    assert stat.S_IMODE((pdir / "wrapper.sh").stat().st_mode) == 0o600
    s.write_private(pdir / "meta.json", __import__("json").dumps(_rec().to_dict()))
    # The wrapper may have created jobs/<id> already (mkdir -p at job start).
    (s.jobs_dir / "1001").mkdir()
    dest = s.finalize(token, "1001")
    assert dest == s.jobs_dir / "1001" and (dest / "wrapper.sh").exists() and not pdir.exists()
    assert s.record("1001") == _rec()
    assert s.lookup("1001") == ("u", "s1") and s.lookup("999") is None
    assert s.read_exit_code("1001") is None
    (dest / "exit_code").write_text("3\n")
    assert s.read_exit_code("1001") == 3
    (dest / "exit_code").write_text("garbage")
    assert s.read_exit_code("1001") is None


def test_list_jobs_and_discard(tmp_path):
    s = StateDir(tmp_path / "state")
    for jid, session, user in [
        ("5", "a", "u"),
        ("12", "a", "u"),
        ("7", "b", "u"),
        ("8", "a", "other"),
    ]:
        token, pdir = s.new_pending()
        s.write_private(
            pdir / "meta.json", __import__("json").dumps(_rec(jid, session, user).to_dict())
        )
        s.finalize(token, jid)
    (s.jobs_dir / "junk").mkdir()
    assert [r.job_id for r in s.list_jobs("u")] == ["5", "7", "12"]
    assert [r.job_id for r in s.list_jobs("u", "a")] == ["5", "12"]
    token, pdir = s.new_pending()
    s.write_private(pdir / "x", "y")
    s.discard_pending(token)
    assert not pdir.exists()
    s.discard_pending("nonexistent")


def test_log_exception(tmp_path):
    s = StateDir(tmp_path / "state")
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        ref = s.log_exception("submit")
    text = s.log_path.read_text()
    assert ref in text and "boom" in text and "RuntimeError" in text
    assert stat.S_IMODE(os.stat(s.log_path).st_mode) == 0o600
