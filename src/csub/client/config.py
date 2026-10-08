"""Client configuration: CSUB_* environment > ~/.config/csub/client.toml > defaults."""

from __future__ import annotations

import hashlib
import os
import shlex
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from typing import Any

from csub.protocol import CsubError, Mount, ProtocolError
from csub.transport import LocalTransport, SshTransport, Transport, UnixTransport

DEFAULT_SSH_HOST = "submit.int.janelia.org"
TRANSPORTS = ("ssh", "local", "unix")


class ConfigError(CsubError):
    def __init__(self, message: str):
        super().__init__("invalid_request", message)


@dataclass(frozen=True)
class ClientConfig:
    transport: str = "ssh"
    ssh_host: str = DEFAULT_SSH_HOST
    ssh_port: int = 22
    ssh_user: str | None = None
    ssh_key: str = "~/.ssh/csub_ed25519"
    ssh_opts: tuple[str, ...] = ()
    broker_cmd: str = "csub-broker"
    socket: str | None = None
    mounts: tuple[Mount, ...] = ()
    session: str | None = None
    image: str | None = None
    wait_max_poll_s: float = 30.0
    source: str = "defaults"

    def with_(self, **changes: Any) -> ClientConfig:
        from dataclasses import replace

        return replace(self, **changes)


def parse_mounts(value: str | list[str]) -> tuple[Mount, ...]:
    """``"/a:rw,/b:ro"`` (or a list of such items) -> Mounts. Mode defaults to rw."""
    items = [x.strip() for x in (value.split(",") if isinstance(value, str) else value)]
    out: list[Mount] = []
    for item in items:
        if not item:
            continue
        path, _, mode = item.rpartition(":")
        if not path:  # no colon at all
            path, mode = item, "rw"
        try:
            m = Mount.from_dict({"path": path, "mode": mode or "rw"}, f"CSUB_MOUNTS entry {item!r}")
        except ProtocolError as e:
            raise ConfigError(str(e.message)) from None
        if any(m.path == x.path for x in out):
            raise ConfigError(f"CSUB_MOUNTS: duplicate path {m.path}")
        out.append(m)
    return tuple(out)


def mounts_to_string(mounts: tuple[Mount, ...]) -> str:
    return ",".join(f"{m.path}:{m.mode}" for m in mounts)


def derive_session(mounts: tuple[Mount, ...]) -> str:
    """A stable id for 'this set of mounts': jobs from the same container share it."""
    canonical = "\n".join(sorted(f"{m.path}:{m.mode}" for m in mounts))
    return hashlib.sha256(canonical.encode()).hexdigest()[:12]


def _read_toml(path: str) -> dict[str, Any]:
    if not os.path.exists(path):
        return {}
    # Imported lazily: inside a job the client is injected into images that may lack tomli.
    try:
        import tomllib
    except ModuleNotFoundError:  # Python < 3.11
        import tomli as tomllib  # type: ignore[no-redef]
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        return {}
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: invalid TOML: {e}") from None
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: top level must be a table")
    return data


def load_client_config(
    environ: Mapping[str, str] | None = None, *, home: str | None = None, path: str | None = None
) -> ClientConfig:
    env = os.environ if environ is None else environ
    home = home or env.get("HOME") or os.path.expanduser("~")
    path = path or os.path.join(home, ".config", "csub", "client.toml")
    values: dict[str, Any] = {}
    source = "defaults"

    file_data = _read_toml(path)
    if file_data:
        source = path
        known = {f.name for f in fields(ClientConfig)} - {"source"}
        unknown = sorted(set(file_data) - known)
        if unknown:
            raise ConfigError(f"{path}: unknown key(s): {', '.join(unknown)}")
        values.update(file_data)

    env_keys = {
        "CSUB_TRANSPORT": "transport",
        "CSUB_SSH_HOST": "ssh_host",
        "CSUB_SSH_PORT": "ssh_port",
        "CSUB_SSH_USER": "ssh_user",
        "CSUB_SSH_KEY": "ssh_key",
        "CSUB_SSH_OPTS": "ssh_opts",
        "CSUB_BROKER_CMD": "broker_cmd",
        "CSUB_SOCKET": "socket",
        "CSUB_MOUNTS": "mounts",
        "CSUB_SESSION": "session",
        "CSUB_IMAGE": "image",
        "CSUB_WAIT_MAX_POLL_S": "wait_max_poll_s",
    }
    for var, key in env_keys.items():
        if env.get(var) not in (None, ""):
            values[key] = env[var]
            source = "environment" if source == "defaults" else source + "+environment"

    # normalize
    if "ssh_port" in values:
        try:
            values["ssh_port"] = int(values["ssh_port"])
        except (TypeError, ValueError):
            raise ConfigError(f"ssh_port: not an integer: {values['ssh_port']!r}") from None
    if "wait_max_poll_s" in values:
        try:
            values["wait_max_poll_s"] = float(values["wait_max_poll_s"])
        except (TypeError, ValueError):
            raise ConfigError("wait_max_poll_s: not a number") from None
    if "ssh_opts" in values:
        v = values["ssh_opts"]
        values["ssh_opts"] = tuple(shlex.split(v)) if isinstance(v, str) else tuple(v)
    if "mounts" in values:
        values["mounts"] = parse_mounts(values["mounts"])
    if values.get("transport") not in (None, *TRANSPORTS):
        raise ConfigError(f"CSUB_TRANSPORT: expected one of {', '.join(TRANSPORTS)}")
    if "broker_cmd" in values and isinstance(values["broker_cmd"], list):
        values["broker_cmd"] = shlex.join(values["broker_cmd"])
    return ClientConfig(**values, source=source)


def make_transport(cfg: ClientConfig) -> Transport:
    if cfg.transport == "ssh":
        return SshTransport(
            cfg.ssh_host,
            user=cfg.ssh_user,
            key=cfg.ssh_key,
            port=cfg.ssh_port,
            broker_cmd=cfg.broker_cmd,
            opts=cfg.ssh_opts,
        )
    if cfg.transport == "local":
        return LocalTransport(cfg.broker_cmd)
    if cfg.transport == "unix":
        if not cfg.socket:
            raise ConfigError("CSUB_SOCKET is required for the unix transport")
        return UnixTransport(cfg.socket)
    raise ConfigError(f"unknown transport {cfg.transport!r}")


__all__ = [
    "ClientConfig",
    "ConfigError",
    "derive_session",
    "load_client_config",
    "make_transport",
    "mounts_to_string",
    "parse_mounts",
]
_ = field  # keep dataclasses.field import used for future defaults
