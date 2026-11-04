import base64
import gzip
import hashlib
import importlib.util
import re
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest
import serial
import typer
from PIL import Image
from typer.testing import CliRunner

spec = importlib.util.spec_from_file_location("r36s_console", Path(__file__).resolve().parents[1] / "r36s-console.py")
con = importlib.util.module_from_spec(spec)
spec.loader.exec_module(con)
DARKOS = (con.HERE.parents[1] / "boot" / "dArkOS" / "consoles" / "clone type2 panel1 amp" / "boot.ini").read_bytes()
USBMODE = (con.HERE / "boot.ini.usbmode").read_bytes()
CARD_PART = USBMODE[: USBMODE.index(b"\n", USBMODE.index(con.FALLBACK)) + 1]
LOG = b"[1.00] R36S: checking for a PC on the OTG port...\n"
DMESG = b"[    0.000000] Booting Linux on physical CPU 0x0\n"
SHARED = {"lun.0": "/dev/mmcblk1", "lun.1": "/dev/mmcblk0"}
# pyserial's error for a port Windows lists before its driver has started (seen on the R36S after a rearm restart).
NOT_FOUND = "could not open port '{}': FileNotFoundError(2, 'The system cannot find the file specified.', None, 2)"


class FakeR36S:
    """A pyserial port with the helper's console (console_session in r36s-usb.v7.sh) behind it, as Windows sees it. Each
    look at the port list moves the port to the next state in timeline (the last one then stays):
    - "up": listed, answers;
    - "old": listed, answers from the start before a restart (the helper hasn't restarted yet);
    - "gone": not listed;
    - "error": listing the ports fails;
    - "unopenable": listed, but its driver hasn't started, so opening it fails;
    - "deaf": opens, but card mode doesn't read it yet, so writes time out.
    A restart into card mode makes the next "up" a new start: another boot ID, both cards shared again."""

    def __init__(self) -> None:
        self.out, self.pending, self.chunk = bytearray(), b"", 1 << 20
        self.commands: list[str] = []
        self.events: list[str] = []
        self.shares = dict(SHARED)
        self.garble, self.closed, self.opens, self.closes, self.looks = False, False, 0, 0, 0
        self.state, self.timeline, self.restarting, self.boot = "up", [], False, 1
        self.restart_timeline = ["old", "gone", "gone", "unopenable", "deaf", "up"]
        self.handlers = {
            "r36s_status": self.status,
            "r36s_reboot": lambda: self.reboot("now"),
            "r36s_reboot unplug": lambda: self.reboot("unplug"),
            "$BB cat /tmp/r36s-usb.log": lambda: (LOG, 0),
            "$BB dmesg": lambda: (DMESG, 0),
            con.BOOT_ID: lambda: (f"{self.boot:08x}-1c2d-4e5f-8a9b-0c1d2e3f4a5b\n".encode(), 0),
        }

    @property
    def in_waiting(self) -> int:
        return len(self.out)

    def read(self, size: int = 1) -> bytes:
        data = bytes(self.out[: min(size, self.chunk)])
        del self.out[: len(data)]
        return data

    def write(self, data: bytes) -> int:
        if self.state == "deaf":
            raise serial.SerialTimeoutException("Write timeout")
        self.pending += data
        while b"\n" in self.pending:
            line, self.pending = self.pending.split(b"\n", 1)
            if m := re.fullmatch(rb"@@(\S+) (\d+) (.*)", line):
                self.answer(m[1].decode(), int(m[2]), m[3].decode())
        return len(data)

    def answer(self, token: str, length: int, command: str) -> None:
        if length != len(command) or self.garble:
            self.out += f"@@X {token} length\n".encode()
            return
        if wrapped := re.fullmatch(r'eval "\$\(echo (\S+) \| \$XB base64 -d\)"', command):
            command = base64.b64decode(wrapped[1]).decode()
        self.commands.append(command)
        output, code = self.handlers.get(command, lambda: (b"sh: not found\n", 127))()
        self.out += f"@@B {token}\n".encode() + output + f"\n@@E {token} {code}\n".encode()

    def status(self) -> tuple[bytes, int]:
        lines = ["helper 7.2", "host darkos", "usb configured", "serial on", "screens 1", "boot.ini restored", *(f"share {lun} {file}" for lun, file in self.shares.items())]
        return "".join(f"{line}\n" for line in lines).encode(), 0

    def reboot(self, when: str) -> tuple[bytes, int]:
        if open_files := [file for file in self.shares.values() if file]:
            return f"Not restarting: {open_files[0]} is still shared. Eject both cards on the PC first.\n".encode(), 1
        self.events.append(f"reboot {when}")
        if when == "unplug":
            return b"Unplug the cable now: the R36S then restarts by itself.\n", 0
        self.restarting, self.timeline = True, list(self.restart_timeline)
        return b"Restarting in a moment.\n", 0

    def find_port(self, device: str | None = None) -> str | None:
        self.looks += 1
        if self.timeline:
            self.state = self.timeline.pop(0)
        if self.state == "up" and self.restarting:
            self.restarting, self.boot, self.shares = False, self.boot + 1, dict(SHARED)
        if self.state == "error":
            raise OSError(22, "SetupDiGetDeviceInstanceId failed")
        return None if self.state == "gone" else device or "COM9"

    def open(self, device: str) -> "FakeR36S":
        if self.state not in ("up", "old", "deaf"):
            raise serial.SerialException(NOT_FOUND.format(device))
        self.opens, self.closed = self.opens + 1, False
        return self

    def eject(self) -> list[str]:
        self.events.append("eject")
        self.shares = dict.fromkeys(self.shares, "")
        return ["F:", "G:", "I:", "J:"]

    def close(self) -> None:
        self.closes, self.closed = self.closes + 1, True


@pytest.fixture
def r36s(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeR36S:
    fake = FakeR36S()
    (tmp_path / "Work").mkdir()
    monkeypatch.setattr(con, "WORK", tmp_path / "Work")
    monkeypatch.setattr(con, "find_port", fake.find_port)
    monkeypatch.setattr(con, "open_port", fake.open)
    monkeypatch.setattr(con, "eject_cards", fake.eject)
    monkeypatch.setattr(con, "pause", lambda seconds: None)
    return fake


@pytest.fixture
def boot(tmp_path: Path) -> Path:
    """A BOOT drive with card mode installed (stand-ins for the big files) and dArkOS's boot.ini."""
    root = tmp_path / "BOOT"
    (root / "usbmode").mkdir(parents=True)
    files = {"usbmode/KERNEL": b"kernel", "usbmode/r36s-usb.v7.sh": b"helper", "post-flash.sh": b"hook", "boot.ini.usbmode": USBMODE}
    for name, data in files.items():
        (root / name).write_bytes(data)
    names = {name: name.removeprefix("usbmode/") if name.startswith("usbmode/") else f"../{name}" for name in files}
    (root / "usbmode" / "usbmode.md5").write_bytes("".join(f"{hashlib.md5(data).hexdigest()}  {names[name]}\n" for name, data in files.items()).encode())
    for name in ("boot.ini", "boot.ini.darkos-original"):
        (root / name).write_bytes(DARKOS)
    return root


def cli(*args: str) -> tuple[int, str]:
    result = CliRunner().invoke(con.app, list(args))
    return result.exit_code, result.output


def screen(name: str) -> bytes:
    with tarfile.open(con.HERE / "screens.tar") as tar:
        return gzip.decompress(tar.extractfile(f"{name}.gz").read())


def shot_output(frame: bytes, md5: str | None = None) -> bytes:
    head = f"geometry 640,480 32 2560\ndisplay card0-DSI-1 connected enabled On\nmd5 {md5 or hashlib.md5(frame).hexdigest()}\ndata\n"
    return head.encode() + base64.encodebytes(gzip.compress(frame))


def test_run_frames_commands_and_returns_output_and_exit_code(r36s: FakeR36S) -> None:
    r36s.handlers |= {"echo hi": lambda: (b"hi\n", 0), "false": lambda: (b"", 1), "printf x": lambda: (b"x", 0)}
    console = con.Console(r36s)
    assert console.run("echo hi") == (0, b"hi\n")
    assert console.run("false") == (1, b"")
    assert console.run("printf x") == (0, b"x")


def test_run_skips_stale_output_and_reads_in_small_pieces(r36s: FakeR36S) -> None:
    r36s.chunk = 3
    r36s.out += b"@@B 0badf00d\nleft over\n\n@@E 0badf00d 0\nnoise without a newline"
    r36s.handlers["uname"] = lambda: (b"Linux\n", 0)
    assert con.Console(r36s).run("uname") == (0, b"Linux\n")


def test_run_sends_multiline_and_non_ascii_commands_base64_encoded(r36s: FakeR36S) -> None:
    script = "echo a\necho é"
    r36s.handlers[script] = lambda: ("a\né\n".encode(), 0)
    assert con.Console(r36s).run(script) == (0, "a\né\n".encode())
    assert r36s.commands == [script]


def test_run_raises_when_garbled_or_unanswered(r36s: FakeR36S) -> None:
    r36s.garble = True
    with pytest.raises(con.ConsoleError, match="garbled"):
        con.Console(r36s).run("echo hi")
    r36s.garble = False
    r36s.answer = lambda *args: None
    with pytest.raises(con.ConsoleError, match="No answer"):
        con.Console(r36s).run("echo hi", timeout=0.2)


def test_find_port_matches_the_usb_id(monkeypatch: pytest.MonkeyPatch) -> None:
    from serial.tools import list_ports

    ports = [SimpleNamespace(device="COM3", vid=None, pid=None), SimpleNamespace(device="COM5", vid=0x1D6B, pid=0x0104), SimpleNamespace(device="COM7", vid=0x1D6B, pid=0x0106)]
    monkeypatch.setattr(list_ports, "comports", lambda: ports)
    assert con.find_port() == "COM7"
    assert con.find_port("COM3") == "COM3"
    assert con.find_port("COM9") is None


def test_status_and_run(r36s: FakeR36S) -> None:
    assert cli("status") == (0, r36s.status()[0].decode())
    r36s.handlers |= {"ls /flash": lambda: (b"boot.ini\n", 0), "test -e /nope": lambda: (b"", 2)}
    assert cli("run", "ls /flash") == (0, "boot.ini\n")
    assert cli("run", "test -e /nope") == (2, "")


def test_shot_saves_the_exact_screen_and_names_it(r36s: FakeR36S, tmp_path: Path) -> None:
    frame = screen("active")
    r36s.handlers["r36s_shot"] = lambda: (shot_output(frame), 0)
    code, output = cli("shot", "--out", str(tmp_path / "s.png"))
    assert code == 0, output
    rgb = bytearray(len(frame) // 4 * 3)
    rgb[0::3], rgb[1::3], rgb[2::3] = frame[2::4], frame[1::4], frame[0::4]
    with Image.open(tmp_path / "s.png") as image:
        assert (image.size, image.mode, image.tobytes()) == ((640, 480), "RGB", bytes(rgb))
    assert "Screen: active" in output
    assert "Display: card0-DSI-1 connected enabled On" in output


def test_shot_default_folder_and_unknown_screen(r36s: FakeR36S, tmp_path: Path) -> None:
    r36s.handlers["r36s_shot"] = lambda: (shot_output(bytes(640 * 480 * 4)), 0)
    code, output = cli("shot")
    assert code == 0, output
    assert len(list((tmp_path / "Work" / "cardmode-shots").glob("r36s-*.png"))) == 1
    assert "none of the card-mode screens" in output


def test_shot_refuses_damaged_data(r36s: FakeR36S, tmp_path: Path) -> None:
    r36s.handlers["r36s_shot"] = lambda: (shot_output(screen("connect"), md5="0" * 32), 0)
    code, output = cli("shot", "--out", str(tmp_path / "s.png"))
    assert code == 1
    assert "MD5 mismatch" in output
    assert not (tmp_path / "s.png").exists()


def test_log_dmesg_and_save(r36s: FakeR36S, tmp_path: Path) -> None:
    assert cli("log") == (0, LOG.decode())
    assert cli("log", "--dmesg") == (0, DMESG.decode())
    code, output = cli("log", "--save")
    assert code == 0, output
    saved = next((tmp_path / "Work" / "cardmode-logs").glob("r36s-usb-*-live.log")).read_bytes()
    assert saved == b"--- status ---\n" + r36s.status()[0] + b"--- card-mode log ---\n" + LOG + b"--- kernel log ---\n" + DMESG


def test_follow_log_shows_only_new_lines(r36s: FakeR36S, capsys: pytest.CaptureFixture[str]) -> None:
    logs = iter([b"a\n", b"a\nb\n", b"a\nb\nc\n"])
    r36s.handlers["$BB cat /tmp/r36s-usb.log"] = lambda: (next(logs), 0)
    con.follow_log(con.Console(r36s), dmesg=False, rounds=3)
    assert capsys.readouterr().out == "a\nb\nc\n"
    assert con.new_part("1\n2\n3\n", "2\n3\n4\n") == "4\n"
    assert con.new_part("x\n", "y\n") == "y\n"


def test_arm_gives_the_tools_scripts_boot_ini(boot: Path) -> None:
    assert "boot.ini armed" in con.arm_boot(boot)
    assert (boot / "boot.ini").read_bytes() == USBMODE
    assert (boot / "boot.ini.darkos").read_bytes() == DARKOS
    assert not (boot / "boot.ini.new").exists()
    assert "armed already" in con.arm_boot(boot)
    assert (boot / "boot.ini").read_bytes() == USBMODE


def test_arm_falls_back_to_the_current_darkos_boot_ini(boot: Path) -> None:
    newer = DARKOS.replace(b"max_cpufreq=1296", b"max_cpufreq=1416")
    assert newer != DARKOS
    (boot / "boot.ini").write_bytes(newer)
    con.arm_boot(boot)
    assert (boot / "boot.ini").read_bytes() == CARD_PART + newer[newer.index(b"\n") + 1 :]
    assert (boot / "boot.ini.darkos").read_bytes() == newer


@pytest.mark.parametrize(("damage", "message"), [("usbmode/KERNEL", "damaged or missing"), ("boot.ini", "isn't dArkOS's boot.ini")])
def test_arm_changes_nothing_when_something_is_wrong(boot: Path, damage: str, message: str) -> None:
    (boot / damage).write_bytes(b"something else")
    before = {p.name: p.read_bytes() for p in boot.iterdir() if p.is_file()}
    code, output = cli("arm", "--boot", str(boot))
    assert code == 1
    assert message in output
    assert {p.name: p.read_bytes() for p in boot.iterdir() if p.is_file()} == before


def test_disarm_puts_darkos_boot_ini_back(boot: Path) -> None:
    assert "is dArkOS's" in con.disarm_boot(boot)
    con.arm_boot(boot)
    assert "back (from boot.ini.darkos)" in con.disarm_boot(boot)
    assert (boot / "boot.ini").read_bytes() == DARKOS
    con.arm_boot(boot)
    (boot / "boot.ini.darkos").write_bytes(USBMODE)
    assert "back (from boot.ini.darkos-original)" in con.disarm_boot(boot)
    assert (boot / "boot.ini").read_bytes() == DARKOS


def test_rearm_arms_saves_the_log_ejects_then_restarts(r36s: FakeR36S, boot: Path, tmp_path: Path) -> None:
    code, output = cli("rearm", "--boot", str(boot))
    assert code == 0, output
    assert (boot / "boot.ini").read_bytes() == USBMODE
    assert r36s.events == ["eject", "reboot now"]
    assert r36s.commands.index("$BB dmesg") < r36s.commands.index(con.BOOT_ID) < r36s.commands.index("r36s_reboot")
    assert len(list((tmp_path / "Work" / "cardmode-logs").glob("*-live.log"))) == 1
    assert "Restarting in a moment." in output
    assert output.endswith(r36s.status()[0].decode())
    assert r36s.boot == 2
    assert r36s.opens == r36s.closes


# The R36S on 11/3 at 22:18: its port stayed a moment, was gone for about 13 s while it restarted, came back listed but
# not yet openable (rearm stopped there: "Can't open COM8: ... FileNotFoundError(2 ..."), then answered once card mode was up.
AS_ON_THE_R36S = ["old", "old", *["gone"] * 13, "unopenable", "unopenable", "deaf", "up"]


@pytest.mark.parametrize(
    "timeline",
    [
        pytest.param(AS_ON_THE_R36S, id="as on the R36S"),
        pytest.param(["old", "error", "old", "old", "gone", "up"], id="port list fails as the port goes"),
        pytest.param(["old", *["gone"] * 150, "up"], id="slow start"),
    ],
)
def test_rearm_waits_through_the_restart_for_a_new_start(r36s: FakeR36S, boot: Path, timeline: list[str]) -> None:
    r36s.restart_timeline = timeline
    code, output = cli("rearm", "--boot", str(boot))
    assert code == 0, output
    assert output.index("Restarting in a moment.") < output.index("Waiting up to 180 s for card mode") < output.index(r36s.status()[0].decode())
    assert output.endswith(r36s.status()[0].decode())
    assert r36s.boot == 2
    assert r36s.opens == r36s.closes


def test_rearm_reports_a_restart_that_doesnt_happen(r36s: FakeR36S, boot: Path) -> None:
    r36s.restart_timeline = ["old"]
    code, output = cli("rearm", "--boot", str(boot))
    assert code == 1
    assert "The R36S hasn't restarted: its COM port is still there 30 s later." in output
    assert "Waiting up to" not in output
    assert r36s.boot == 1


def test_rearm_reports_card_mode_that_doesnt_come_back(r36s: FakeR36S, boot: Path) -> None:
    r36s.restart_timeline = ["old", "gone"]
    code, output = cli("rearm", "--boot", str(boot), "--timeout", "20")
    assert code == 1
    assert "Card mode isn't back after 20 s: no R36S serial port (USB 1d6b:0106). If the R36S shows card mode" in output


def test_rearm_doesnt_restart_while_a_card_is_open(r36s: FakeR36S, boot: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(con, "eject_cards", lambda: ["F:"])
    code, output = cli("rearm", "--boot", str(boot))
    assert code == 1
    assert "still has /dev/mmcblk1, /dev/mmcblk0 open" in output
    assert "r36s_reboot" not in r36s.commands


def test_exit_puts_darkos_back_and_restarts_once_unplugged(r36s: FakeR36S, boot: Path) -> None:
    con.arm_boot(boot)
    code, output = cli("exit", "--boot", str(boot))
    assert code == 0, output
    assert (boot / "boot.ini").read_bytes() == DARKOS
    assert r36s.events == ["eject", "reboot unplug"]
    assert "Unplug the cable now" in output


def test_wait_for_the_port(r36s: FakeR36S) -> None:
    r36s.timeline = ["gone"] * 5 + ["up"]
    assert cli("wait") == (0, r36s.status()[0].decode())
    r36s.timeline = ["gone"]
    code, output = cli("wait", "--timeout", "3")
    assert code == 1
    assert "No answer from the R36S's console within 3 s: no R36S serial port (USB 1d6b:0106). Is card mode" in output


def test_wait_and_status_ride_out_a_port_that_isnt_ready(r36s: FakeR36S) -> None:
    r36s.timeline = ["gone", "error", "unopenable", "unopenable", "deaf", "up"]
    assert cli("wait") == (0, r36s.status()[0].decode())
    r36s.timeline = ["unopenable", "unopenable", "deaf", "up"]
    assert cli("status") == (0, r36s.status()[0].decode())
    r36s.timeline = ["unopenable"]
    code, output = cli("status")
    assert code == 1
    assert f"The R36S's console didn't answer within 15 s: {NOT_FOUND.format('COM9')}" in output


def test_status_without_a_port_fails_at_once(r36s: FakeR36S) -> None:
    r36s.timeline = ["gone"]
    code, output = cli("status")
    assert code == 1
    assert "No R36S serial port (USB 1d6b:0106)." in output
    assert r36s.looks == 1


def test_wait_for_stops_at_the_deadline_when_tries_are_slow(monkeypatch: pytest.MonkeyPatch) -> None:
    now = iter(range(0, 1000, 4))
    monkeypatch.setattr(con, "clock", lambda: next(now))
    monkeypatch.setattr(con, "pause", lambda seconds: None)
    tries: list[int] = []
    assert con.wait_for(lambda: tries.append(1), 30) is None
    assert len(tries) == 8


def test_eject_cards_ejects_each_card_with_all_its_volumes(monkeypatch: pytest.MonkeyPatch) -> None:
    volumes = [("G:", 2, "EASYROMS"), ("F:", 2, "BOOT"), ("I:", 3, "R36S-GAMES"), ("J:", 3, "EMUELEC")]
    monkeypatch.setattr(con, "r36s_volumes", lambda: [{"letter": letter, "disk": disk, "label": label} for letter, disk, label in volumes])
    ejected, explorer = [], []
    monkeypatch.setattr(con, "eject_disk", ejected.append)
    monkeypatch.setattr(con, "explorer_eject", explorer.append)
    assert con.eject_cards() == ["F:", "G:", "I:", "J:"]
    assert (ejected, explorer) == ([["F:", "G:"], ["I:", "J:"]], [])

    def denied(letters: list[str]) -> None:
        raise con.EjectError(f"Can't open {letters[0]}: Access is denied.", denied=True)

    monkeypatch.setattr(con, "eject_disk", denied)
    con.eject_cards()
    assert explorer == [["F:", "G:"], ["I:", "J:"]]

    def busy(letters: list[str]) -> None:
        raise con.EjectError(f"{letters[0]} is in use")

    monkeypatch.setattr(con, "eject_disk", busy)
    with pytest.raises(typer.Exit):
        con.eject_cards()


@pytest.mark.parametrize(
    ("stdout", "volumes"),
    [("[]", []), ("", []), ("[null]", []), ('{"disk":2,"letter":"F:","label":"BOOT"}', [{"disk": 2, "letter": "F:", "label": "BOOT"}])],
)
def test_r36s_volumes_reads_powershell_json(monkeypatch: pytest.MonkeyPatch, stdout: str, volumes: list) -> None:
    monkeypatch.setattr(con.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=stdout, stderr=""))
    assert con.r36s_volumes() == volumes
