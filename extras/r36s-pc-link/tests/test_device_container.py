"""Tests that need Docker Desktop. Skipped without it.

- pclinkd.sh on fake hardware (tests/device), in an arm64 chroot of the dArkOS4Clone root file system (the image in
  R36S_IMAGE, mounted read-only at /mnt/a4c in the docker-desktop WSL distro, done here if it isn't), and in
  its Debian 13 stand-in (the tools image), which needs no image;
- the PC Link menu ("PC Link.sh", scripted through a fake dialog) in both;
- pclink_agent.py in that chroot;
- the kernel ABI check and the button tests natively (amd64), where uinput works;
- end to end: this PC's ssh.exe and r36s-remote.py -> dArkOS's own sshd (PC Link's config) -> the agent.
The tests that need the image are skipped without it.
"""

import hashlib
import os
import shutil
import struct
import subprocess
import sys
import time
import uuid
import zlib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
IMAGE = os.environ.get("R36S_IMAGE", "")
WSL = ["wsl", "-d", "docker-desktop", "-u", "root", "-e", "sh", "-c"]
# Our own arm64 image: running debian:trixie-slim with --platform linux/arm64 would re-point that shared tag.
BASE = "r36s-pclink-tools:arm64"
ARM64 = ["docker", "run", "--rm", "--privileged", "--platform", "linux/arm64", "-v", "/mnt/a4c:/r:ro", "-v", f"{ROOT}:/w:ro", BASE, "sh", "/w/tests/device/chroot-run.sh"]
LABEL = "r36s.pclink.dockerfile"


def sh(cmd: list[str], timeout: float = 900) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=False, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)


def tools_image(arch: str) -> str:
    """r36s-pclink-tools:<arch> (tests/device/Dockerfile), built when missing or made from another Dockerfile.
    BuildKit leaves the debian tag alone."""
    tag = f"r36s-pclink-tools:{arch}"
    want = hashlib.sha256((ROOT / "tests/device/Dockerfile").read_bytes()).hexdigest()[:16]
    have = sh(["docker", "image", "inspect", "-f", f'{{{{index .Config.Labels "{LABEL}"}}}}', tag], 60)
    if have.returncode or have.stdout.strip() != want:
        built = sh(["docker", "build", "--platform", f"linux/{arch}", "-q", "--label", f"{LABEL}={want}", "-t", tag, str(ROOT / "tests/device")], 1800)
        assert built.returncode == 0, built.stdout[-2000:] + built.stderr[-3000:]
    return tag


@pytest.fixture(scope="module", autouse=True)
def docker():
    if not shutil.which("docker") or sh(["docker", "info"], 60).returncode:
        pytest.skip("Docker Desktop isn't running")
    sh([*WSL, "modprobe dummy; modprobe uinput; modprobe evdev"], 60)


@pytest.fixture(scope="module")
def rootfs():
    if sh([*WSL, "test -d /mnt/a4c/usr"], 60).returncode:
        if not IMAGE or not Path(IMAGE).is_file():
            pytest.skip(f"no dArkOS4Clone image: set R36S_IMAGE (now {IMAGE or 'unset'})")
        drive, rest = IMAGE[:2], IMAGE[3:].replace("\\", "/")
        mount = f"mkdir -p /mnt/r36s-src /mnt/a4c && (mountpoint -q /mnt/r36s-src || mount -t drvfs {drive} /mnt/r36s-src -o ro) && mount -o loop,ro '/mnt/r36s-src/{rest}' /mnt/a4c"
        assert sh([*WSL, mount], 120).returncode == 0, "couldn't mount the image read-only"
    tools_image("arm64")


def test_pclinkd_on_fake_hardware(rootfs):
    result = sh([*ARM64, "bash", "/w/tests/device/test_pclinkd.sh"])
    assert result.returncode == 0 and "failed 0" in result.stdout, result.stdout[-4000:] + result.stderr[-2000:]


def test_pclinkd_on_the_debian13_standin():
    tag = tools_image("amd64")
    result = sh(["docker", "run", "--rm", "--privileged", "--platform", "linux/amd64", "-v", f"{ROOT}:/w:ro", tag, "sh", "/w/tests/device/standin-run.sh", "bash", "/w/tests/device/test_pclinkd.sh"])
    assert result.returncode == 0 and "failed 0" in result.stdout, result.stdout[-4000:] + result.stderr[-2000:]


def test_menu_on_the_debian13_standin():
    tag = tools_image("amd64")
    result = sh(["docker", "run", "--rm", "--privileged", "--platform", "linux/amd64", "-v", f"{ROOT}:/w:ro", tag, "sh", "/w/tests/device/standin-run.sh", "bash", "/w/tests/device/test_menu.sh"])
    assert result.returncode == 0 and "failed 0" in result.stdout, result.stdout[-4000:] + result.stderr[-2000:]


def test_menu_in_the_darkos_rootfs(rootfs):
    result = sh([*ARM64, "bash", "/w/tests/device/test_menu.sh"])
    assert result.returncode == 0 and "failed 0" in result.stdout, result.stdout[-4000:] + result.stderr[-2000:]


def test_agent_in_the_darkos_rootfs(rootfs):
    result = sh([*ARM64, "bash", "/w/tests/device/run-agent-tests.sh"])
    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-4000:]


@pytest.mark.parametrize("arch", ["amd64", "arm64"])
def test_kernel_abi_and_buttons(arch):
    tag = tools_image(arch)
    extra = ["--buttons"] if arch == "amd64" else []  # uinput ioctls only work natively, not under arm64 emulation
    result = sh(["docker", "run", "--rm", "--privileged", "--platform", f"linux/{arch}", "-v", f"{ROOT}:/w:ro", tag, "bash", "/w/tests/device/run-native-tests.sh", *extra])
    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-3000:]
    assert "30 of 30 ABI values match" in result.stdout


def png_pixel(data: bytes, x: int, y: int) -> tuple[int, int, int]:
    width = struct.unpack(">I", data[16:20])[0]
    pos, idat = 8, b""
    while pos < len(data):
        (length,) = struct.unpack(">I", data[pos : pos + 4])
        if data[pos + 4 : pos + 8] == b"IDAT":
            idat += data[pos + 8 : pos + 8 + length]
        pos += 12 + length
    row = zlib.decompress(idat)[y * (width * 3 + 1) + 1 :]
    return tuple(row[x * 3 : x * 3 + 3])


@pytest.fixture(scope="module")
def link(rootfs, tmp_path_factory):
    """PC Link (Wi-Fi mode) in the dArkOS chroot, its sshd published on a free localhost port."""
    if not shutil.which("ssh-keygen") or not shutil.which("ssh"):
        pytest.skip("needs Windows' OpenSSH client")
    tmp = tmp_path_factory.mktemp("e2e")
    key = tmp / "key"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "e2e", "-f", str(key)], check=True)
    name = f"r36s-pclink-e2e-{uuid.uuid4().hex[:8]}"
    cmd = ["docker", "run", "-d", "--name", name, "--privileged", "--platform", "linux/arm64", "-p", "127.0.0.1:0:2222"]
    cmd += ["-v", "/mnt/a4c:/r:ro", "-v", f"{ROOT}:/w:ro", "-v", f"{tmp}:/k:ro", BASE, "sh", "/w/tests/device/chroot-run.sh", "bash", "/w/tests/device/e2e-device.sh"]
    assert sh(cmd, 120).returncode == 0
    try:
        deadline = time.monotonic() + 300
        while "E2E-READY" not in (logs := sh(["docker", "logs", name], 60).stdout):
            running = sh(["docker", "inspect", "-f", "{{.State.Running}}", name], 60).stdout.strip() == "true"
            assert time.monotonic() < deadline and running, logs[-3000:] + sh(["docker", "logs", name], 60).stderr[-3000:]
            time.sleep(2)
        port = sh(["docker", "port", name, "2222"], 60).stdout.split(":")[-1].strip()
        yield {"port": port, "key": key, "known": tmp / "known_hosts", "tmp": tmp}
    finally:
        sh(["docker", "rm", "-f", name], 120)


def pc(link: dict, *args: str) -> subprocess.CompletedProcess:
    base = [sys.executable, str(ROOT / "r36s-remote.py"), "--host", "127.0.0.1", "--port", link["port"], "--key", str(link["key"]), "--known-hosts", str(link["known"])]
    return sh([*base, *args], 300)


def test_end_to_end_from_windows(link):
    up = pc(link, "wait", "--timeout", "120")
    assert up.returncode == 0 and "PC Link is up" in up.stdout, up.stdout + up.stderr
    assert "r36s-pclink" in link["known"].read_text()  # host key pinned under the alias

    status = pc(link, "status")
    assert status.returncode == 0 and "Link: wifi, ready" in status.stdout, status.stdout + status.stderr

    shot = link["tmp"] / "shot.png"
    taken = pc(link, "screenshot", str(shot))
    assert taken.returncode == 0, taken.stderr
    data = shot.read_bytes()
    assert struct.unpack(">II", data[16:24]) == (640, 480)
    assert png_pixel(data, 150, 100) == (255, 0, 0) and png_pixel(data, 10, 10) == (0, 0, 0)

    assert "ThemeSet = es-theme-nes-box (string)" in pc(link, "es", "get", "ThemeSet").stdout
    changed = pc(link, "es", "set", "ScreenSaverBehavior", "black")
    assert changed.returncode == 0 and "ES restarted" in changed.stdout, changed.stdout + changed.stderr
    assert "ScreenSaverBehavior = black" in pc(link, "es", "get", "ScreenSaverBehavior").stdout

    plan = pc(link, "launch", "/roms/snes/E2E Test (USA).sfc", "--dry-run")
    assert plan.returncode == 0 and "snes: retroarch / snes9x" in plan.stdout and r"/roms/snes/E2E\ Test\ \(USA\).sfc" in plan.stdout, plan.stdout + plan.stderr

    press = pc(link, "press", "a")  # no pad in the emulated container: the agent's error comes through
    assert press.returncode == 1 and 'no input device named "GO-Super Gamepad"' in press.stderr

    assert "ready" in pc(link, "logs", "pclink").stdout
    ran = pc(link, "run", "echo hello; id -un")
    assert ran.returncode == 0 and ran.stdout.split() == ["hello", "ark"], ran.stdout + ran.stderr
    assert pc(link, "run", "--root", "id -u").stdout.strip() == "0"
    assert pc(link, "run", "exit 7").returncode == 7

    stopped = pc(link, "stop-link")
    assert stopped.returncode == 0 and "stopping" in stopped.stdout
    time.sleep(5)
    after = pc(link, "status")
    assert after.returncode == 1 and "SSH to 127.0.0.1" in after.stderr
