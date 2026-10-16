"""Read job output from the shared filesystem (no broker round trip).

``<cwd>/.csub/jobs/<id>/`` is written by the job, so every component below ``cwd`` is under
the agent's control. A reader outside a sandbox (a human on the submit host, an MCP server on
a workstation) resolves symlinks with its own access, so a child job could otherwise hand its
parent any file the reader can open. Each component is therefore opened relative to the one
above it with ``O_NOFOLLOW``, starting from ``/`` (``cwd`` is a realpath, but a deeper cwd
has agent-replaceable ancestors under the mount), only regular files are read, and at most
``MAX_BYTES`` of them. Files are opened non-blocking so a FIFO cannot hang the reader.
"""

from __future__ import annotations

import errno
import os
import stat
from pathlib import Path

from csub.protocol import CsubError

MAX_BYTES = 1 << 20  # the most a single read returns: the last MiB of the file
_NOFOLLOW = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
# directories: O_PATH needs only search permission on ancestors such as /groups/<lab>
_DIR = os.O_PATH | os.O_NOFOLLOW | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0)


def job_dir(cwd: str, job_id: str) -> Path:
    return Path(cwd) / ".csub" / "jobs" / str(job_id)


def _refuse(path: Path, e: OSError) -> CsubError:
    what = "is a symlink" if e.errno == errno.ELOOP else e.strerror
    return CsubError("not_found", f"refusing to read {path}: {what}")


def _open_job_dir(cwd: str, job_id: str) -> int | None:
    """Descriptor on ``<cwd>/.csub/jobs/<id>``, or None if it does not exist yet."""
    comps = [c for c in cwd.split("/") if c] + [".csub", "jobs", str(job_id)]
    fd = os.open("/", _DIR)
    try:
        for comp in comps:
            try:
                nfd = os.open(comp, _DIR, dir_fd=fd)
            except FileNotFoundError:
                return None
            except OSError as e:  # ELOOP (symlink), ENOTDIR (a file in the way)
                raise _refuse(job_dir(cwd, job_id), e) from None
            os.close(fd)
            fd = nfd
        out, fd = fd, -1
        return out
    finally:
        if fd >= 0:
            os.close(fd)


def _tail(dir_fd: int, name: str, lines: int | None, where: Path) -> str:
    """Last ``lines`` lines of ``name`` in ``dir_fd`` ('' if missing); None means the last
    MAX_BYTES as a whole."""
    if lines is not None and lines <= 0:
        return ""
    try:
        fd = os.open(name, _NOFOLLOW, dir_fd=dir_fd)
    except FileNotFoundError:
        return ""
    except OSError as e:
        raise _refuse(where / name, e) from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise CsubError("not_found", f"refusing to read {where / name}: not a regular file")
        start = max(0, st.st_size - MAX_BYTES)
        data = os.pread(fd, st.st_size - start, start)
    finally:
        os.close(fd)
    text = data.decode(errors="replace")
    if lines is None:
        return text
    return "\n".join(text.splitlines()[-lines:]) + ("\n" if text.endswith("\n") else "")


def read_output(cwd: str, job_id: str, name: str, lines: int | None) -> str:
    """One stream; '' when the job has not produced its directory or the file yet."""
    fd = _open_job_dir(cwd, job_id)
    if fd is None:
        return ""
    try:
        return _tail(fd, name, lines, job_dir(cwd, job_id))
    finally:
        os.close(fd)


def read_logs(
    cwd: str, job_id: str, *, stream: str = "both", tail: int | None = None
) -> dict[str, str]:
    d = job_dir(cwd, job_id)
    fd = _open_job_dir(cwd, job_id)
    if fd is None:
        raise CsubError("not_found", f"no output directory for job {job_id} at {d}")
    try:
        out: dict[str, str] = {"job_dir": str(d)}
        if stream in ("stdout", "both"):
            out["stdout"] = _tail(fd, "stdout", tail, d)
        if stream in ("stderr", "both"):
            out["stderr"] = _tail(fd, "stderr", tail, d)
        return out
    finally:
        os.close(fd)
