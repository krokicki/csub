"""Build broker policies for tests.

Limits and queues are copied from deploy/broker.janelia.toml so the tests and the shipped
Janelia policy cannot drift apart.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    import tomli as tomllib  # type: ignore[no-redef]

REPO_ROOT = Path(__file__).resolve().parents[2]
JANELIA_POLICY = REPO_ROOT / "deploy" / "broker.janelia.toml"


def janelia_tables() -> dict[str, Any]:
    with open(JANELIA_POLICY, "rb") as f:
        data = tomllib.load(f)
    return {"limits": data["limits"], "queues": data["queues"]}


def default_policy_dict(
    *,
    home: Path,
    scripts_dir: Path,
    allowed_roots: list[Path],
    denied_roots: list[Path] = (),
    readonly_roots: list[Path] = (),
    scratch_root: Path,
    lsf_dir: Path | None = None,
) -> dict[str, Any]:
    tables = janelia_tables()
    lsf = {"profile": "", "bsub": "bsub", "bjobs": "bjobs", "bkill": "bkill"}
    if lsf_dir is not None:
        lsf = {"profile": "", **{t: str(lsf_dir / t) for t in ("bsub", "bjobs", "bkill")}}
    return {
        "broker": {
            "state_dir": str(home / ".csub"),
            "sandbox_scripts_dir": str(scripts_dir),
            "default_image": "ghcr.io/test/agent:latest",
            "allowed_images": ["ghcr.io/test/agent:*", "ghcr.io/test/other-*:*"],
            "allowed_roots": [str(p) for p in allowed_roots],
            "denied_roots": [str(p) for p in denied_roots],
            "readonly_roots": [str(p) for p in readonly_roots],
            "allowed_hosts": ["pypi.org", "files.pythonhosted.org", "github.com", "*.janelia.org"],
            "scratch": True,
            "scratch_root": str(scratch_root),
            "keep_id": True,
            "env_allow": ["OMP_NUM_THREADS", "MY_PROJECT_*", "CUDA_*"],
            "lsf_extra_allow": [],
        },
        "limits": dict(tables["limits"]),
        "queues": {k: dict(v) for k, v in tables["queues"].items()},
        "lsf": lsf,
    }


def _toml_value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):
        return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    raise TypeError(f"cannot encode {type(v).__name__}")


def to_toml(d: dict[str, Any]) -> str:
    """Minimal TOML writer: scalars, string lists, one or two levels of tables."""
    lines: list[str] = []
    for section, body in d.items():
        if not isinstance(body, dict):
            raise TypeError("top level must be tables")
        nested = {k: v for k, v in body.items() if isinstance(v, dict)}
        flat = {k: v for k, v in body.items() if not isinstance(v, dict)}
        if flat or not nested:
            lines.append(f"[{section}]")
            for k, v in flat.items():
                lines.append(f"{k} = {_toml_value(v)}")
            lines.append("")
        for sub, subbody in nested.items():
            lines.append(f"[{section}.{sub}]")
            for k, v in subbody.items():
                lines.append(f"{k} = {_toml_value(v)}")
            lines.append("")
    return "\n".join(lines)


def write_policy(path: Path, d: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(to_toml(d))
    os.chmod(path, 0o600)
    return path
