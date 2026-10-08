"""Path and pattern helpers shared by broker and client. Standard library only."""

from __future__ import annotations

import os
import re


def normalize_abs(path: str) -> str:
    """Return ``os.path.normpath(path)``; raise ``ValueError`` unless ``path`` is absolute."""
    if not isinstance(path, str) or not path.startswith("/"):
        raise ValueError(f"not an absolute path: {path!r}")
    if "\0" in path:
        raise ValueError("path contains NUL")
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
