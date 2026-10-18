"""Path and pattern helpers shared by broker and client. Standard library only."""

from __future__ import annotations

import os
import re

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


def normalize_abs(path: str) -> str:
    """Return ``os.path.normpath(path)``; raise ``ValueError`` unless ``path`` is absolute.

    Control characters (NUL, newline, tab, ...) are refused: paths land in the spooled job
    script, and LSF honours a ``#BSUB`` line wherever it appears in that script.
    """
    if not isinstance(path, str) or not path.startswith("/"):
        raise ValueError(f"not an absolute path: {path!r}")
    m = _CONTROL_RE.search(path)
    if m:
        raise ValueError(f"path contains a control character (U+{ord(m.group()):04X})")
    return os.path.normpath(path)


def is_under(path: str, root: str) -> bool:
    """True if ``path`` equals ``root`` or lies below it. Both must be normalized."""
    if root == "/":
        return path.startswith("/")
    return path == root or path.startswith(root.rstrip("/") + "/")


def glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Shell-style glob where ``*`` and ``?`` do not cross ``/``.

    ``fnmatch`` lets ``*`` match ``/``, which would make ``ghcr.io/org/*`` match
    ``ghcr.io/org/evil/image``. Image allowlists need the narrower semantics.
    """
    out: list[str] = []
    for ch in pattern:
        if ch == "*":
            out.append("[^/]*")
        elif ch == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(ch))
    return re.compile("".join(out))


def image_registry(image: str) -> str | None:
    """Return the registry component of an OCI image reference, or None if unqualified.

    Docker's rule: the first path component is a registry iff it contains ``.`` or ``:``
    or is ``localhost``.
    """
    first, sep, _rest = image.partition("/")
    if not sep:
        return None
    if "." in first or ":" in first or first == "localhost":
        return first
    return None
