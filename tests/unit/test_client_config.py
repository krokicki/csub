import pytest

from csub.client.config import (
    ClientConfig,
    ConfigError,
    derive_session,
    load_client_config,
    make_transport,
    mounts_to_string,
    parse_mounts,
)
from csub.protocol import Mount
from csub.transport import LocalTransport, SshTransport, UnixTransport


def test_defaults(tmp_path):
    cfg = load_client_config({}, home=str(tmp_path))
    assert cfg == ClientConfig()
    assert cfg.transport == "ssh" and cfg.ssh_host == "submit.int.janelia.org"


def test_env_overrides_file(tmp_path):
    (tmp_path / ".config" / "csub").mkdir(parents=True)
    (tmp_path / ".config" / "csub" / "client.toml").write_text(
        'transport = "local"\nssh_host = "login1.int.janelia.org"\n'
        'mounts = ["/a:rw", "/b:ro"]\nssh_opts = ["-v"]\n'
    )
    cfg = load_client_config(
        {"CSUB_SSH_HOST": "submit.int.janelia.org", "CSUB_SSH_PORT": "2222"}, home=str(tmp_path)
    )
    assert (
        cfg.transport == "local"
        and cfg.ssh_host == "submit.int.janelia.org"
        and cfg.ssh_port == 2222
    )
    assert cfg.mounts == (Mount("/a", "rw"), Mount("/b", "ro")) and cfg.ssh_opts == ("-v",)
    assert "client.toml" in cfg.source and "environment" in cfg.source


def test_file_errors(tmp_path):
    p = tmp_path / ".config" / "csub"
    p.mkdir(parents=True)
    (p / "client.toml").write_text("bogus = 1\n")
    with pytest.raises(ConfigError, match="unknown key"):
        load_client_config({}, home=str(tmp_path))
    (p / "client.toml").write_text("[broken\n")
    with pytest.raises(ConfigError, match="invalid TOML"):
        load_client_config({}, home=str(tmp_path))


def test_env_validation(tmp_path):
    with pytest.raises(ConfigError, match="CSUB_TRANSPORT"):
        load_client_config({"CSUB_TRANSPORT": "carrier-pigeon"}, home=str(tmp_path))
    with pytest.raises(ConfigError, match="ssh_port"):
        load_client_config({"CSUB_SSH_PORT": "abc"}, home=str(tmp_path))
    cfg = load_client_config(
        {"CSUB_SSH_OPTS": "-o StrictHostKeyChecking=no -o 'UserKnownHostsFile=/x y'"},
        home=str(tmp_path),
    )
    assert cfg.ssh_opts == ("-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/x y")
    cfg = load_client_config({"CSUB_MOUNTS": "", "CSUB_SESSION": ""}, home=str(tmp_path))
    assert cfg.mounts == () and cfg.session is None


def test_parse_mounts():
    assert parse_mounts("/groups/lab/p:rw, /nrs/lab/d:ro") == (
        Mount("/groups/lab/p", "rw"),
        Mount("/nrs/lab/d", "ro"),
    )
    assert parse_mounts("/a") == (Mount("/a", "rw"),)
    assert parse_mounts("/a/,,") == (Mount("/a", "rw"),)
    assert mounts_to_string(parse_mounts("/a:ro,/b")) == "/a:ro,/b:rw"
    for bad, match in [
        ("/a:rwx", "mode"),
        ("relative:rw", "absolute"),
        ("/a:rw,/a:ro", "duplicate"),
    ]:
        with pytest.raises(ConfigError, match=match):
            parse_mounts(bad)


def test_derive_session_is_stable_and_order_independent():
    a = derive_session(parse_mounts("/a:rw,/b:ro"))
    b = derive_session(parse_mounts("/b:ro,/a:rw"))
    assert a == b and len(a) == 12 and a != derive_session(parse_mounts("/a:ro,/b:ro"))


def test_make_transport():
    assert isinstance(make_transport(ClientConfig(transport="local")), LocalTransport)
    assert isinstance(make_transport(ClientConfig(transport="unix", socket="/s")), UnixTransport)
    with pytest.raises(ConfigError, match="CSUB_SOCKET"):
        make_transport(ClientConfig(transport="unix"))
    t = make_transport(
        ClientConfig(transport="ssh", ssh_user="u", ssh_opts=("StrictHostKeyChecking=no",))
    )
    assert isinstance(t, SshTransport)
    argv = t.argv
    assert argv[:4] == ["ssh", "-T", "-p", "22"] and argv[-2:] == [
        "submit.int.janelia.org",
        "csub-broker",
    ]
    assert "-l" in argv and argv[argv.index("-l") + 1] == "u"
    assert (
        "StrictHostKeyChecking=no" in argv
        and "BatchMode=yes" in argv
        and "ControlMaster=auto" in argv
    )
    assert argv[argv.index("-i") + 1].endswith("/.ssh/csub_ed25519")
