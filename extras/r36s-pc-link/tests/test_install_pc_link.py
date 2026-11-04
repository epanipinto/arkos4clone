import shutil
import subprocess

import pytest
from typer.testing import CliRunner

runner = CliRunner()


@pytest.fixture
def pubkey(tmp_path):
    if not shutil.which("ssh-keygen"):
        pytest.skip("needs OpenSSH's ssh-keygen")
    key = tmp_path / "keys" / "k"
    key.parent.mkdir()
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "test", "-f", str(key)], check=True)
    return key


@pytest.fixture
def tools(tmp_path):
    folders = [tmp_path / "EASYROMS" / "tools", tmp_path / "R36S-GAMES" / "tools"]
    for folder in folders:
        folder.mkdir(parents=True)
    return folders


def args(tools, pubkey, *extra):
    return [a for folder in tools for a in ("--tools", str(folder))] + ["--pubkey", str(pubkey) + ".pub", "--force", *extra]


def test_refuses_folders_not_on_the_r36s_cards(installer, tools, pubkey):
    result = runner.invoke(installer.app, ["--tools", str(tools[0]), "--pubkey", str(pubkey) + ".pub"])
    assert result.exit_code == 1
    assert "not a tools folder on EASYROMS or R36S-GAMES" in result.output


def test_dry_run_writes_nothing(installer, tools, pubkey):
    result = runner.invoke(installer.app, args(tools, pubkey, "--dry-run"))
    assert result.exit_code == 0, result.output
    assert "Dry run: nothing written (8 of 8 files would be)" in result.output
    assert all(not any(folder.iterdir()) for folder in tools)


def test_install_then_reinstall(installer, tools, pubkey):
    result = runner.invoke(installer.app, args(tools, pubkey))
    assert result.exit_code == 0, result.output
    assert "Installed: 8 files written" in result.output
    for folder in tools:
        assert (folder / "PC Link.sh").read_bytes() == (installer.DEVICE / "PC Link.sh").read_bytes()
        assert (folder / ".pclink/pclinkd.sh").read_bytes() == (installer.DEVICE / ".pclink/pclinkd.sh").read_bytes()
        assert (folder / ".pclink/pclink_agent.py").read_bytes() == (installer.DEVICE / ".pclink/pclink_agent.py").read_bytes()
        assert (folder / ".pclink/authorized_keys").read_text() == (pubkey.with_name("k.pub")).read_text().strip() + "\n"
        assert b"\r" not in (folder / ".pclink/pclinkd.sh").read_bytes()
    again = runner.invoke(installer.app, args(tools, pubkey))
    assert "Installed: 0 files written, 8 already up to date" in again.output


def test_private_key_and_garbage_refused(installer, tools, pubkey, tmp_path):
    result = runner.invoke(installer.app, ["--tools", str(tools[0]), "--pubkey", str(pubkey), "--force"])
    assert result.exit_code == 1
    assert "is a private key" in result.output
    bad = tmp_path / "bad.pub"
    bad.write_text("ssh-ed25519 not base64!\nsecond line\n")
    result = runner.invoke(installer.app, ["--tools", str(tools[0]), "--pubkey", str(bad), "--force"])
    assert "isn't a single OpenSSH public key line" in result.output
    assert not any(tools[0].iterdir())


def test_conf_and_remove(installer, tools, pubkey):
    result = runner.invoke(installer.app, args(tools[:1], pubkey, "--subnet", "192.168.236", "--port", "2323"))
    assert result.exit_code == 0, result.output
    assert (tools[0] / ".pclink/pclink.conf").read_text() == "subnet=192.168.236\nport=2323\n"
    assert "1024-65535" in runner.invoke(installer.app, args(tools[:1], pubkey, "--port", "80")).output
    assert "first three parts" in runner.invoke(installer.app, args(tools[:1], pubkey, "--subnet", "10.300.1")).output
    dry = runner.invoke(installer.app, args(tools[:1], pubkey, "--remove", "--dry-run"))
    assert "Dry run: nothing deleted (5 files would be)" in dry.output
    gone = runner.invoke(installer.app, args(tools[:1], pubkey, "--remove"))
    assert gone.exit_code == 0, gone.output
    assert not any(tools[0].iterdir())
