import base64
import json
import shutil
import socket
import struct
import subprocess
import threading
import zlib
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

runner = CliRunner()


def tiny_png(width: int = 640, height: int = 480) -> bytes:
    def chunk(kind: bytes, payload: bytes) -> bytes:
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + bytes(width * 3) for _ in range(height))
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


@pytest.fixture
def fake_ssh(remote, monkeypatch):
    """Replaces run_ssh: answers[0] is returned for the first call, and so on; calls are recorded."""
    calls, answers = [], []

    def run_ssh(target, argv, root=False):
        calls.append((target, argv, root))
        code, out, err = answers.pop(0) if answers else (0, "{}", "")
        return subprocess.CompletedProcess(argv, code, out, err)

    monkeypatch.setattr(remote, "run_ssh", run_ssh)
    return calls, answers


def test_ssh_command_pins_the_host_and_uses_only_the_key(remote, tmp_path):
    t = remote.Target("10.36.36.1", 2222, tmp_path / "k", tmp_path / "kh")
    cmd = remote.ssh_command(t, "true")
    joined = " ".join(cmd)
    for part in (
        "-p 2222",
        f"-i {tmp_path / 'k'}",
        "IdentitiesOnly=yes",
        "BatchMode=yes",
        f"UserKnownHostsFile={tmp_path / 'kh'}",
        "StrictHostKeyChecking=accept-new",
        "HostKeyAlias=r36s-pclink",
        "-T",
    ):
        assert part in joined
    assert cmd[-2:] == ["ark@10.36.36.1", "true"]
    assert "-t" in remote.ssh_command(t, None, tty=True)


def test_remote_line_quotes_for_the_r36s_shell(remote):
    line = remote.remote_line(["/usr/bin/python3", remote.AGENT, "launch", "/roms2/snes/Super Mario World (USA).sfc"], root=True)
    assert line == "sudo -n /usr/bin/python3 /run/pclink/bin/pclink_agent.py launch '/roms2/snes/Super Mario World (USA).sfc'"


def test_buttons_are_checked_before_connecting(remote, fake_ssh):
    calls, _ = fake_ssh
    result = runner.invoke(remote.app, ["press", "a", "fn+turbo"])
    assert result.exit_code == 1
    assert "Unknown button 'turbo'" in result.output
    assert calls == []
    remote.check_buttons(["fn+start", "UP"])
    with pytest.raises(typer.Exit):
        remote.check_buttons(["a+"])


def test_press_passes_timing_and_prints_result(remote, fake_ssh):
    calls, answers = fake_ssh
    answers.append((0, json.dumps({"pressed": ["a", "b+start"]}), ""))
    result = runner.invoke(remote.app, ["press", "a", "b+start", "--hold-ms", "80"])
    assert result.exit_code == 0, result.output
    assert "Pressed: a b+start" in result.output
    _, argv, root = calls[0]
    assert root and argv[1:] == [remote.AGENT, "press", "a", "b+start", "--hold-ms", "80", "--gap-ms", "250"]


def test_screenshot_writes_png(remote, fake_ssh, tmp_path):
    _, answers = fake_ssh
    png = tiny_png()
    answers.append((0, base64.b64encode(png).decode() + "\n", "drm 640x480 32bpp\n"))
    out = tmp_path / "shot.png"
    result = runner.invoke(remote.app, ["screenshot", str(out)])
    assert result.exit_code == 0, result.output
    assert out.read_bytes() == png
    assert "640x480, drm 640x480 32bpp" in result.output
    answers.append((0, base64.b64encode(png).decode(), ""))
    result = runner.invoke(remote.app, ["screenshot", str(tmp_path)])
    assert result.exit_code == 0
    assert len(list(tmp_path.glob("r36s-*.png"))) == 1


def test_screenshot_rejects_non_png(remote, fake_ssh, tmp_path):
    _, answers = fake_ssh
    answers.append((0, base64.b64encode(b"not a png").decode(), ""))
    result = runner.invoke(remote.app, ["screenshot", str(tmp_path / "x.png")])
    assert result.exit_code == 1
    assert "didn't send a PNG" in result.output
    assert not (tmp_path / "x.png").exists()


def test_agent_errors_and_ssh_errors_are_explained(remote, fake_ssh):
    _, answers = fake_ssh
    answers.append((1, "", "error: a game is running (retroarch); quit it first, or add --force\n"))
    result = runner.invoke(remote.app, ["es", "set", "ThemeSet", "x"])
    assert result.exit_code == 1
    assert "a game is running (retroarch)" in result.output
    answers.append((255, "", "ark@10.36.36.1: Permission denied (publickey).\r\n"))
    result = runner.invoke(remote.app, ["status"])
    assert result.exit_code == 1
    assert "refused this PC's key" in result.output and "Show the log" in result.output
    answers.append((255, "", "kex_exchange_identification: read: Connection reset\r\n"))
    result = runner.invoke(remote.app, ["status"])
    assert "closed the connection straight away" in result.output
    answers.append((255, "", "ssh: connect to host 10.36.36.1 port 2222: Connection timed out"))
    result = runner.invoke(remote.app, ["--key", __file__, "status"])
    assert "Is PC Link on" in result.output


def test_status_pretty(remote, fake_ssh):
    _, answers = fake_ssh
    status = {
        "link": {"mode": "usb", "state": "connected", "pc": "10.36.36.10", "udc": "configured", "message": "Connected to the PC."},
        "otg": {"power": "off", "5v_seen": True},
        "es": "running",
        "game": [{"pid": 42, "comm": "retroarch", "cmdline": "retroarch -L snes9x"}],
        "display": {"mode": "640x480@60"},
        "battery": {"capacity": "87", "status": "Charging"},
        "uptime_s": 3725,
        "time": "2026-11-03 20:00:00 MST",
    }
    answers.append((0, json.dumps(status), ""))
    result = runner.invoke(remote.app, ["status"])
    assert result.exit_code == 0, result.output
    for text in (
        "Link: usb, connected (PC 10.36.36.10); USB: configured",
        "OTG power: off; 5 V seen on the port: yes",
        "Game: retroarch (pid 42)",
        "Display: 640x480@60",
        "Battery: 87% (Charging)",
        "Up 1:02",
    ):
        assert text in result.output


def test_launch_and_es_commands(remote, fake_ssh):
    calls, answers = fake_ssh
    plan = {"system": "snes", "emulator": "retroarch", "core": "snes9x", "governor": "", "command": "sudo perfmax ...", "dry_run": True}
    answers.append((0, json.dumps(plan), ""))
    result = runner.invoke(remote.app, ["launch", "/roms2/snes/a b.sfc", "--core", "snes9x", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "snes: retroarch / snes9x" in result.output and "Dry run" in result.output
    assert calls[-1][1][2:] == ["launch", "/roms2/snes/a b.sfc", "--core=snes9x", "--dry-run"]
    answers.append((0, json.dumps({"name": "ThemeSet", "type": "string", "value": "x", "backup": "/b", "es_restarted": True}), ""))
    result = runner.invoke(remote.app, ["es", "set", "ThemeSet", "x"])
    assert "ThemeSet = x (string); backup /b; ES restarted" in result.output
    answers.append((0, json.dumps({"stopped": [{"pid": 7, "comm": "retroarch"}]}), ""))
    assert "Stopped: retroarch (7)" in runner.invoke(remote.app, ["quit-game"]).output


def test_wait_finds_a_listening_link(remote, fake_ssh):
    _, answers = fake_ssh
    answers.append((0, json.dumps({"ok": True}), ""))
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen()
    threading.Thread(target=lambda: server.accept()[0].close(), daemon=True).start()
    port = server.getsockname()[1]
    result = runner.invoke(remote.app, ["--host", "127.0.0.1", "--port", str(port), "wait", "--timeout", "5"])
    server.close()
    assert result.exit_code == 0, result.output
    assert f"PC Link is up: ark@127.0.0.1:{port}" in result.output


def test_wait_gives_up(remote, fake_ssh):
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    port = server.getsockname()[1]
    server.close()
    result = runner.invoke(remote.app, ["--host", "127.0.0.1", "--port", str(port), "wait", "--timeout", "1"])
    assert result.exit_code == 1
    assert "No PC Link at 127.0.0.1" in result.output


def test_stop_link_needs_the_stop_sent(remote, fake_ssh):
    calls, answers = fake_ssh
    answers.append((0, "stop sent\nservice=active\n", ""))
    result = runner.invoke(remote.app, ["stop-link"])
    assert result.exit_code == 0 and "stopping" in result.output
    assert calls[-1][1] == ["/bin/bash", remote.LINK, "stop", "--no-block"] and calls[-1][2]
    answers.append((255, "stop sent\n", "Connection to 10.36.36.1 closed by remote host.\n"))  # the session ended with the link
    result = runner.invoke(remote.app, ["stop-link"])
    assert result.exit_code == 0 and "stopping" in result.output
    answers.append((255, "", "ssh: connect to host 10.36.36.1 port 2222: Connection refused"))
    result = runner.invoke(remote.app, ["stop-link"])
    assert result.exit_code == 1 and "SSH to 10.36.36.1:2222 failed" in result.output
    answers.append((1, "stop sent\n", "systemctl couldn't stop PC Link.\n"))
    result = runner.invoke(remote.app, ["stop-link"])
    assert result.exit_code == 1 and "couldn't stop" in result.output


@pytest.mark.skipif(not shutil.which("ssh-keygen"), reason="needs OpenSSH's ssh-keygen")
def test_setup_key_prints_only_the_fingerprint(remote, tmp_path):
    key = tmp_path / "pclink_key"
    result = runner.invoke(remote.app, ["--key", str(key), "setup-key"])
    assert result.exit_code == 0, result.output
    pub = Path(str(key) + ".pub").read_text().split()
    assert pub[0] == "ssh-ed25519" and key.is_file()
    assert "Fingerprint: 256 SHA256:" in result.output
    assert pub[1] not in result.output and "PRIVATE" not in result.output
    assert "BEGIN OPENSSH PRIVATE KEY" not in result.output
    again = runner.invoke(remote.app, ["--key", str(key), "setup-key"])
    assert "Key already there" in again.output and Path(str(key) + ".pub").read_text().split()[1] == pub[1]


@pytest.mark.skipif(not shutil.which("ssh-keygen"), reason="needs OpenSSH's ssh-keygen")
def test_forget_host(remote, tmp_path):
    known = tmp_path / "kh"
    known.write_text(
        "r36s-pclink ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIDtJ7bJ8ZB+kNtrnbChS2GnZUjE10mi/lWQaJ6Bu3o4V\nother ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIDtJ7bJ8ZB+kNtrnbChS2GnZUjE10mi/lWQaJ6Bu3o4V\n"
    )
    result = runner.invoke(remote.app, ["--known-hosts", str(known), "forget-host"])
    assert result.exit_code == 0, result.output
    text = known.read_text()
    assert "r36s-pclink" not in text and "other ssh-ed25519" in text
