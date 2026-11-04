r"""Work with the R36S over USB while it's in USB card mode (helper v7.2 or later, started from the dArkOS card).

In card mode the R36S is a card reader for both of its cards and, from v7.2, also a USB serial port (a COM port, USB ID
1d6b:0106) with a root shell. This tool finds that port by its USB ID and uses it to save exact screenshots of the R36S's
screen, read the card-mode log and the kernel log, run commands, and restart the R36S: into card mode again (rearm) or back
to dArkOS (exit, once the cable is unplugged). Before a restart it ejects both R36S drives in Windows (the cards only, so the
COM port stays), and the R36S refuses to restart until it has closed both cards, so nothing written to them is lost. The R36S
doesn't write to BOOT once the PC has it, so the tool saves the live log on the PC first (in cardmode-logs under the folder
in R36S_WORK, else the current folder).

Needs Windows and Python 3.11+ with typer, pillow and pyserial (python -m pip install --user pyserial).

Usage: python r36s-console.py COMMAND [--help]
  wait                 wait for the R36S's console (card mode starting, or after a restart), then show the status
  status               helper version, USB state, serial port, screens, boot.ini, shared cards
  shot                 save the screen as a PNG (cardmode-shots, see above) and name the card-mode screen it shows
  log                  the card-mode log; --dmesg the kernel log; --follow keep reading; --save both to a file
  run "ls -l /flash"   run a command as root on the R36S (busybox sh; there's no Ctrl-C, so it must end by itself)
  rearm                restart into card mode: arm boot.ini on BOOT, save the log, eject both drives, restart, then
                       wait until card mode is back (a new start of the R36S, told apart by the kernel's boot ID)
  exit                 back to dArkOS: check boot.ini, save the log, eject both drives; unplugging then restarts the R36S
                       into dArkOS (which powers the OTG port itself, so it mustn't start with the PC connected)
  arm, disarm, eject   single steps (arm and disarm only change boot.ini on the PC's BOOT drive)
An empty file BOOT\usbmode\no-serial turns the serial port off: card mode is then the card reader alone, as in v7.1.
"""

import base64
import ctypes
import gzip
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import tarfile
import time
from collections.abc import Callable
from ctypes import wintypes
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, NoReturn

import typer

app = typer.Typer(add_completion=False, no_args_is_help=True, pretty_exceptions_show_locals=False)

HERE = Path(__file__).resolve().parent
WORK = Path(os.environ.get("R36S_WORK", Path.cwd()))
VID, PID = 0x1D6B, 0x0106
MARK = b"R36S-USB-CARD-MODE"
FALLBACK = b"# R36S-USB-CARD-MODE-FALLBACK"
LOG_COMMANDS = {False: "$BB cat /tmp/r36s-usb.log", True: "$BB dmesg"}
BOOT_ID = "$BB cat /proc/sys/kernel/random/boot_id"
# Drive letters on the R36S's USB disks (inquiry vendor "R36S": "R36S TF1 boot card", "R36S TF2 card").
PS_VOLUMES = r"""
$out = [System.Collections.Generic.List[object]]::new()
foreach ($d in Get-CimInstance Win32_DiskDrive | Where-Object { $_.PNPDeviceID -like '*VEN_R36S*' }) {
  foreach ($p in Get-CimAssociatedInstance -InputObject $d -ResultClassName Win32_DiskPartition) {
    foreach ($l in Get-CimAssociatedInstance -InputObject $p -ResultClassName Win32_LogicalDisk) {
      $out.Add([pscustomobject]@{ disk = [int]$d.Index; letter = [string]$l.DeviceID; label = [string]$l.VolumeName })
    }
  }
}
ConvertTo-Json -Compress -InputObject $out.ToArray()
"""
GENERIC_READ_WRITE, FILE_SHARE_READ_WRITE, OPEN_EXISTING, ERROR_ACCESS_DENIED = 0xC0000000, 3, 3, 5
FSCTL_LOCK_VOLUME, FSCTL_DISMOUNT_VOLUME, IOCTL_STORAGE_MEDIA_REMOVAL, IOCTL_STORAGE_EJECT_MEDIA = 0x90018, 0x90020, 0x2D4804, 0x2D4808

PortOption = Annotated[str | None, typer.Option(help="COM port (default: the one with USB ID 1d6b:0106)")]
BootOption = Annotated[Path | None, typer.Option(help="The BOOT drive (default: the R36S drive labelled BOOT)")]
pause, clock = time.sleep, time.monotonic


class ConsoleError(Exception):
    pass


class EjectError(Exception):
    def __init__(self, message: str, denied: bool = False) -> None:
        super().__init__(message)
        self.denied = denied


class Console:
    """The helper's console: a command is one line, "@@<id> <length> <command>"; its output comes back between "@@B <id>"
    and a new line with "@@E <id> <exit code>". Commands that aren't printable ASCII are sent base64-encoded."""

    def __init__(self, port: Any) -> None:
        self.port = port

    def run(self, command: str, timeout: float = 30) -> tuple[int, bytes]:
        if not (command.isascii() and command.isprintable()):
            command = f'eval "$(echo {base64.b64encode(command.encode()).decode()} | $XB base64 -d)"'
        token = secrets.token_hex(4)
        self.port.write(f"\n@@{token} {len(command)} {command}\n".encode())
        begin, end = re.compile(rf"@@B {token}\r?\n".encode()), re.compile(rf"\r?\n@@E {token} (\d+)\r?\n".encode())
        data, deadline = b"", time.monotonic() + timeout
        while time.monotonic() < deadline:
            data += self.port.read(self.port.in_waiting or 1)
            if f"@@X {token} ".encode() in data:
                raise ConsoleError("The R36S got the command garbled. Try again.")
            if (b := begin.search(data)) and (e := end.search(data, b.end())):
                return int(e[1]), data[b.end() : e.start()]
        raise ConsoleError(f"No answer from the R36S within {timeout:g} s.")


def fail(message: str) -> NoReturn:
    typer.echo(message, err=True)
    raise typer.Exit(1)


def text(data: bytes) -> str:
    return data.decode(errors="replace")


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def out_dir(name: str) -> Path:
    folder = WORK / name if WORK.is_dir() else Path.cwd()
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def wait_for(found: Callable[[], Any], seconds: float) -> Any:
    """found()'s first true result, trying about once a second for seconds (fewer tries when each one takes a while)."""
    deadline = clock() + seconds
    for _ in range(int(seconds)):
        if (result := found()) or clock() >= deadline:
            return result
        pause(1)
    return found()


def find_port(device: str | None = None) -> str | None:
    """The R36S's COM port, by its USB ID (or device, if that port is there)."""
    from serial.tools import list_ports

    return next((p.device for p in list_ports.comports() if (p.device == device if device else (p.vid, p.pid) == (VID, PID))), None)


def listed(device: str | None = None) -> str | None:
    """find_port, but a port list that fails while a device comes or goes counts as no port for now."""
    try:
        return find_port(device)
    except OSError:
        return None


def open_port(device: str) -> Any:
    import serial

    return serial.Serial(device, 115200, timeout=0.2, write_timeout=5)


def parse_status(data: bytes) -> dict[str, Any]:
    status: dict[str, Any] = {"text": text(data), "shares": {}}
    for line in text(data).splitlines():
        key, _, value = line.partition(" ")
        if key == "share":
            lun, _, file = value.partition(" ")
            status["shares"][lun] = file.strip()
        elif key:
            status[key] = value.strip()
    return status


def boot_id(console: Console) -> str:
    """The kernel's ID for this start of the R36S ("" if it can't be read)."""
    rc, out = console.run(BOOT_ID, timeout=5)
    return text(out).strip() if rc == 0 else ""


def await_console(port: str | None, seconds: float, old_boot: str = "") -> tuple[Console, dict[str, Any]]:
    """The R36S's console and its status, once it answers; with old_boot, only from a new start of the R36S. Until then a
    missing port is normal: it's gone while the R36S restarts, Windows lists it again a moment before it can be opened
    (pyserial's FileNotFoundError), and the R36S only reads it once card mode is up (writes time out). Raises
    ConsoleError with the last problem if that doesn't happen within seconds."""
    problem = ""

    def attempt() -> tuple[Console, dict[str, Any]] | None:
        nonlocal problem
        if not (device := listed(port)):
            problem = f"no R36S serial port ({port or 'USB 1d6b:0106'})"
            return None
        try:
            console = Console(open_port(device))
        except OSError as e:
            problem = str(e)
            return None
        try:
            status = parse_status(console.run("r36s_status", timeout=3)[1])
            if not old_boot or boot_id(console) != old_boot:
                return console, status
            problem = f"{device} still answers from the R36S's previous start"
        except (ConsoleError, OSError) as e:
            problem = f"{device}: {e}"
        console.port.close()
        return None

    if found := wait_for(attempt, seconds):
        return found
    raise ConsoleError(problem.rstrip("."))


def connect(port: str | None = None, seconds: float = 15) -> tuple[Console, dict[str, Any]]:
    """The R36S's console and its status. Fails at once if there's no R36S port; a port that's there gets seconds to open
    and answer (it may have just come back)."""
    if not listed(port):
        fail(f"No R36S serial port ({port or 'USB 1d6b:0106'}). Start USB Card Mode (v7.2 or later) with the cable plugged in, or run: wait")
    try:
        return await_console(port, seconds)
    except ConsoleError as e:
        fail(f"The R36S's console didn't answer within {seconds:g} s: {e}.")


def screen_name(frame: bytes) -> str | None:
    """Which card-mode screen (screens.tar next to this tool) frame is exactly, if any."""
    if (HERE / "screens.tar").is_file():
        with tarfile.open(HERE / "screens.tar") as tar:
            for member in tar.getmembers():
                if member.name.endswith(".gz") and gzip.decompress(tar.extractfile(member).read()) == frame:
                    return Path(member.name).name.removesuffix(".gz")
    return None


def new_part(shown: str, current: str) -> str:
    """What current adds to shown; the kernel log can drop old lines, so then it's what follows shown's last line."""
    if current.startswith(shown):
        return current[len(shown) :]
    last = shown.rstrip("\n").rpartition("\n")[2] + "\n"
    return current[current.rfind(last) + len(last) :] if last != "\n" and last in current else current


def follow_log(console: Console, dmesg: bool, rounds: int | None = None) -> None:
    shown, done = "", 0
    while rounds is None or done < rounds:
        current = text(console.run(LOG_COMMANDS[dmesg])[1])
        typer.echo(new_part(shown, current), nl=False)
        shown, done = current, done + 1
        pause(2)


def save_logs(console: Console) -> Path:
    """The helper's status, its live log and the kernel log, in one file on the PC."""
    parts = [(title, console.run(command)[1]) for title, command in (("status", "r36s_status"), ("card-mode log", LOG_COMMANDS[False]), ("kernel log", LOG_COMMANDS[True]))]
    path = out_dir("cardmode-logs") / f"r36s-usb-{stamp()}-live.log"
    path.write_bytes(b"".join(f"--- {title} ---\n".encode() + data for title, data in parts))
    return path


def lines_of(data: bytes) -> list[bytes]:
    """Lines with their line ends, as sed sees them."""
    lines = data.split(b"\n")
    return [line + b"\n" for line in lines[:-1]] + ([lines[-1]] if lines[-1] else [])


def is_darkos_ini(data: bytes | None) -> bool:
    """dArkOS's boot.ini: U-Boot's header on line 1, a booti command, no card-mode marker (as in "USB Card Mode.sh")."""
    lines = (data or b"").split(b"\n")
    return lines[0].replace(b"\r", b"").lower() == b"odroidgoa-uboot-config" and any(line.startswith(b"booti ") for line in lines) and MARK not in data


def read(path: Path) -> bytes | None:
    return path.read_bytes() if path.is_file() else None


def write_synced(path: Path, content: bytes) -> None:
    with path.open("wb") as f:
        f.write(content)
        f.flush()
        os.fsync(f.fileno())


def write_ini(boot: Path, content: bytes) -> bool:
    """boot.ini.new, renamed over boot.ini (as "USB Card Mode.sh" does); True if boot.ini reads back as content."""
    write_synced(boot / "boot.ini.new", content)
    os.replace(boot / "boot.ini.new", boot / "boot.ini")
    return read(boot / "boot.ini") == content


def saved_ini(boot: Path) -> tuple[str, bytes]:
    for name in ("boot.ini.darkos", "boot.ini.darkos-original"):
        if is_darkos_ini(data := read(boot / name)):
            return name, data
    fail("No saved dArkOS boot.ini (boot.ini.darkos) on BOOT to come back to. Nothing was changed.")


def find_boot(boot: Path | None) -> Path:
    if boot:
        root = Path(str(boot).rstrip("\\/") + "\\")
    elif letters := [v["letter"] for v in r36s_volumes() if v["label"] == "BOOT"]:
        root = Path(letters[0] + "\\")
    else:
        fail("No BOOT drive from the R36S (the dArkOS card in slot TF1). Name it with --boot.")
    if not (root / "boot.ini.usbmode").is_file():
        fail(f"{root} has no boot.ini.usbmode: it isn't a dArkOS card with USB card mode.")
    return root


def arm_boot(boot: Path) -> str:
    """What "USB Card Mode.sh" does on the R36S, done on the PC's BOOT drive: the next start is card mode."""
    manifest = boot / "usbmode" / "usbmode.md5"
    if not all(p.is_file() and p.stat().st_size for p in (manifest, boot / "boot.ini.usbmode", boot / "post-flash.sh")):
        fail(f"USB card mode isn't installed on {boot} (usbmode\\usbmode.md5 is missing).")
    for digest, name in (line.split("  ", 1) for line in manifest.read_text(encoding="ascii").splitlines()):
        if (data := read(boot / "usbmode" / name)) is None or hashlib.md5(data).hexdigest() != digest:
            fail(f"USB card mode files on {boot} are damaged or missing ({name}, usbmode.md5). Nothing was changed.")
    ini, note = read(boot / "boot.ini"), ""
    if is_darkos_ini(ini):
        write_synced(boot / "boot.ini.darkos", ini)
    elif ini and MARK in ini:
        note = "boot.ini was armed already. "
    else:
        fail(f"{boot / 'boot.ini'} isn't dArkOS's boot.ini. Nothing was changed.")
    name, saved = saved_ini(boot)
    template = lines_of((boot / "boot.ini.usbmode").read_bytes())
    end = next((i for i, line in enumerate(template) if i and line.startswith(FALLBACK)), len(template) - 1)
    new = b"".join(template[: end + 1] + lines_of(saved)[1:])
    lines = lines_of(new)
    if (
        sum(line.startswith(FALLBACK) for line in lines) != 1
        or b"booti 0x02000000 - 0x01f00000" not in new
        or not any(line.startswith(b"booti ") for line in lines)
        or lines[0].rstrip(b"\n").lower() != b"odroidgoa-uboot-config"
    ):
        fail("The card-mode boot.ini came out wrong. Nothing was changed.")
    if not write_ini(boot, new):
        write_ini(boot, saved)
        fail(f"Could not write {boot / 'boot.ini'}; dArkOS's boot.ini was put back.")
    return f"{note}boot.ini armed: the next start is USB card mode (falling back to dArkOS's boot.ini from {name})."


def disarm_boot(boot: Path) -> str:
    """What card mode's post-flash.sh does at start, done on the PC's BOOT drive: the next start is dArkOS."""
    ini = read(boot / "boot.ini")
    if is_darkos_ini(ini):
        return "boot.ini is dArkOS's: the next start is dArkOS."
    if not ini or MARK not in ini:
        fail(f"{boot / 'boot.ini'} is neither dArkOS's nor card mode's. Nothing was changed: see USB-CARD-MODE-RECOVERY.txt.")
    name, saved = saved_ini(boot)
    if not write_ini(boot, saved):
        fail(f"Could not write {boot / 'boot.ini'}: see USB-CARD-MODE-RECOVERY.txt on BOOT.")
    return f"dArkOS's boot.ini is back (from {name}): the next start is dArkOS."


def r36s_volumes() -> list[dict[str, Any]]:
    """The R36S's drive letters: disk number, letter and label of each volume on its USB disks."""
    shell = shutil.which("pwsh") or "powershell"
    result = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-Command", PS_VOLUMES], check=False, capture_output=True, text=True, timeout=120)
    if result.returncode:
        fail(f"Couldn't list the R36S's drives: {result.stderr.strip()}")
    volumes = json.loads(result.stdout or "[]")
    return [v for v in (volumes if isinstance(volumes, list) else [volumes]) if v and v.get("letter")]


def eject_disk(letters: list[str]) -> None:
    """Locks and dismounts every volume of one card (Windows writes out what it has cached), then ejects the card's medium:
    SCSI START STOP UNIT with eject, after which the R36S closes that card. The USB device and its COM port stay."""
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    k32.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD, wintypes.LPDWORD, wintypes.LPVOID]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]

    def control(handle: int, code: int, allow: ctypes.c_ubyte | None = None) -> bool:
        size = wintypes.DWORD()
        inbuf, length = (ctypes.byref(allow), 1) if allow is not None else (None, 0)
        return bool(k32.DeviceIoControl(handle, code, inbuf, length, None, 0, ctypes.byref(size), None))

    handles: list[int] = []
    try:
        for letter in letters:
            handle = k32.CreateFileW("\\\\.\\" + letter, GENERIC_READ_WRITE, FILE_SHARE_READ_WRITE, None, OPEN_EXISTING, 0, None)
            if handle in (None, ctypes.c_void_p(-1).value):
                error = ctypes.get_last_error()
                raise EjectError(f"Can't open {letter}: {ctypes.FormatError(error).strip()}", denied=error == ERROR_ACCESS_DENIED)
            handles.append(handle)
            if not wait_for(lambda handle=handle: control(handle, FSCTL_LOCK_VOLUME), 10):
                raise EjectError(f"{letter} is in use (an Explorer window or a program has files open on it). Close them and try again.")
            if not control(handle, FSCTL_DISMOUNT_VOLUME):
                raise EjectError(f"Windows didn't dismount {letter}: {ctypes.FormatError(ctypes.get_last_error()).strip()}")
        control(handles[0], IOCTL_STORAGE_MEDIA_REMOVAL, ctypes.c_ubyte(0))
        if not control(handles[0], IOCTL_STORAGE_EJECT_MEDIA):
            raise EjectError(f"Windows didn't eject {', '.join(letters)}: {ctypes.FormatError(ctypes.get_last_error()).strip()}")
    finally:
        for handle in handles:
            k32.CloseHandle(handle)


def explorer_eject(letters: list[str]) -> None:
    """Explorer's Eject, for when Windows doesn't let this tool eject the cards itself."""
    names = ",".join(f"'{letter}'" for letter in letters if re.fullmatch(r"[A-Z]:", letter))
    command = f"$s = New-Object -ComObject Shell.Application; foreach ($d in @({names})) {{ $s.Namespace(17).ParseName($d).InvokeVerb('Eject') }}"
    subprocess.run([shutil.which("pwsh") or "powershell", "-NoProfile", "-NonInteractive", "-Command", command], check=False, capture_output=True, timeout=120)


def eject_cards() -> list[str]:
    """Ejects both R36S cards in Windows, all of a card's volumes at once. Returns the drive letters."""
    disks: dict[int, list[str]] = {}
    for volume in r36s_volumes():
        disks.setdefault(volume["disk"], []).append(volume["letter"])
    for letters in disks.values():
        try:
            eject_disk(sorted(letters))
        except EjectError as e:
            if not e.denied:
                fail(str(e))
            typer.echo(f"{e}; using Explorer's Eject instead.")
            explorer_eject(sorted(letters))
    return sorted(letter for letters in disks.values() for letter in letters)


def open_cards(console: Console) -> list[str]:
    return [file for file in parse_status(console.run("r36s_status")[1])["shares"].values() if file]


def restart(console: Console, when: str, port: str | None = None) -> str:
    """Saves the live log, ejects both cards, waits until the R36S has closed them, then has it restart: at once ("now",
    into card mode), or once the cable is unplugged ("unplug", into dArkOS, which powers the OTG port itself). For "now"
    it returns once the R36S's port is gone, with the boot ID of the start that ended ("" if it couldn't be read)."""
    try:
        typer.echo(f"Log saved: {save_logs(console)}")
        typer.echo(f"Ejected: {', '.join(eject_cards()) or 'no R36S drives were mounted'}")
        if not wait_for(lambda: not open_cards(console), 30):
            fail(f"The R36S still has {', '.join(open_cards(console))} open, so it wasn't restarted. Use Safely Remove on the PC, then unplug the cable.")
        old_boot = boot_id(console) if when == "now" else ""
        rc, out = console.run("r36s_reboot" if when == "now" else "r36s_reboot unplug")
    except (ConsoleError, OSError) as e:
        fail(f"Lost the R36S's console ({e}). If Windows removed the whole R36S, unplug the cable: it powers off after 10 s; then turn it on.")
    if rc:
        fail(text(out).strip())
    typer.echo(text(out).strip())
    console.port.close()
    if when == "now" and not wait_for(lambda: not listed(port), 30):
        fail(
            "The R36S hasn't restarted: its COM port is still there 30 s later. Unplug the cable (if the R36S doesn't power off"
            " within 15 s, hold its power button for 10 s), then turn it on: boot.ini is armed, so it starts card mode again."
        )
    return old_boot


@app.command()
def wait(timeout: Annotated[float, typer.Option(help="Seconds to wait")] = 180, port: PortOption = None) -> None:
    """Wait for the R36S's console (card mode starting, or after a restart), then show the status."""
    try:
        console, state = await_console(port, timeout)
    except ConsoleError as e:
        fail(f"No answer from the R36S's console within {timeout:g} s: {e}. Is card mode (v7.2 or later) running, with the cable plugged in?")
    console.port.close()
    typer.echo(state["text"], nl=False)


@app.command()
def status(port: PortOption = None) -> None:
    """Show the helper's state: version, USB state, serial port, screens, boot.ini, shared cards."""
    typer.echo(connect(port)[1]["text"], nl=False)


@app.command()
def shot(out: Annotated[Path | None, typer.Option(help=r"PNG file (default: cardmode-shots\r36s-<time>.png)")] = None, port: PortOption = None) -> None:
    """Save the R36S's screen (an exact copy of its framebuffer) as a PNG and name the card-mode screen it shows."""
    from PIL import Image

    rc, data = connect(port)[0].run("r36s_shot", timeout=120)
    meta, found, payload = data.partition(b"\ndata\n")
    info = text(meta).splitlines()
    geometry = next((line.split()[1:] for line in info if line.startswith("geometry ")), [])
    if rc or not found or len(geometry) != 3:
        fail(f"r36s_shot failed ({rc}): {text(data).strip()}")
    size, bpp, stride = geometry
    width, height = map(int, size.split(","))
    raw = gzip.decompress(base64.b64decode(payload))
    if hashlib.md5(raw).hexdigest() != next((line.split()[1] for line in info if line.startswith("md5 ")), ""):
        fail("The screenshot arrived damaged (MD5 mismatch). Try again.")
    if bpp != "32" or len(raw) < int(stride) * height:
        fail(f"fb0 is {size}, {bpp} bits per pixel, stride {stride}: not a screen this tool can read.")
    frame = raw[: int(stride) * height]
    path = out or out_dir("cardmode-shots") / f"r36s-{stamp()}.png"
    Image.frombuffer("RGB", (width, height), frame, "raw", "BGRX", int(stride), 1).save(path)
    typer.echo(f"Saved {path} ({width}x{height})")
    name = screen_name(frame)
    typer.echo(f"Screen: {name}" if name else "Screen: none of the card-mode screens (blank, partly drawn, or something else)")
    for line in info:
        if line.startswith("display "):
            typer.echo(f"Display: {line.removeprefix('display ')}")


@app.command()
def log(
    dmesg: Annotated[bool, typer.Option("--dmesg", help="The kernel log instead")] = False,
    follow: Annotated[bool, typer.Option("--follow", help="Keep showing new lines (Ctrl+C stops)")] = False,
    save: Annotated[bool, typer.Option("--save", help=r"Save the status and both logs to a file (cardmode-logs)")] = False,
    port: PortOption = None,
) -> None:
    """Show the card-mode log as it is now (the R36S writes it to BOOT only before the PC gets the cards)."""
    console = connect(port)[0]
    if save:
        typer.echo(f"Saved {save_logs(console)}")
    elif follow:
        try:
            follow_log(console, dmesg)
        except (ConsoleError, OSError):
            typer.echo("The R36S's console went away (it restarted, powered off or was unplugged).")
        except KeyboardInterrupt:
            pass
    else:
        typer.echo(text(console.run(LOG_COMMANDS[dmesg])[1]), nl=False)


@app.command()
def run(command: str, timeout: Annotated[float, typer.Option(help="Seconds to wait for it to end")] = 30, port: PortOption = None) -> None:
    """Run a shell command as root on the R36S, print its output and exit with its exit code."""
    rc, out = connect(port)[0].run(command, timeout)
    typer.echo(text(out), nl=False)
    raise typer.Exit(rc)


@app.command()
def arm(boot: BootOption = None) -> None:
    """Arm boot.ini on the PC's BOOT drive: the next start is card mode (what Tools > USB Card Mode does)."""
    typer.echo(arm_boot(find_boot(boot)))


@app.command()
def disarm(boot: BootOption = None) -> None:
    """Put dArkOS's boot.ini back on the PC's BOOT drive: the next start is dArkOS."""
    typer.echo(disarm_boot(find_boot(boot)))


@app.command()
def eject(port: PortOption = None) -> None:
    """Eject both R36S drives in Windows (the cards only; the COM port stays) and check that the R36S closed them."""
    typer.echo(f"Ejected: {', '.join(eject_cards()) or 'no R36S drives were mounted'}")
    if listed(port):
        console = connect(port)[0]
        if not wait_for(lambda: not open_cards(console), 15):
            fail(f"The R36S still has {', '.join(open_cards(console))} open.")
        typer.echo("The R36S has closed both cards.")


@app.command()
def rearm(
    wait_back: Annotated[bool, typer.Option("--wait/--no-wait", help="Wait for card mode to be back")] = True,
    timeout: Annotated[float, typer.Option(help="Seconds to wait for card mode to be back")] = 180,
    boot: BootOption = None,
    port: PortOption = None,
) -> None:
    """Restart into card mode: arm boot.ini on BOOT, save the log, eject both drives, restart, wait for card mode."""
    console = connect(port)[0]
    typer.echo(arm_boot(find_boot(boot)))
    old_boot = restart(console, "now", port)
    if not wait_back:
        return
    typer.echo(f"The R36S is restarting. Waiting up to {timeout:g} s for card mode...")
    try:
        console, state = await_console(port, timeout, old_boot)
    except ConsoleError as e:
        fail(
            f"Card mode isn't back after {timeout:g} s: {e}. If the R36S shows card mode, it's the card reader alone (no COM"
            " port): use Safely Remove, then unplug. If its screen stays black, hold the power button for 10 s, then turn it on."
        )
    console.port.close()
    typer.echo(state["text"], nl=False)


@app.command("exit")
def exit_(boot: BootOption = None, port: PortOption = None) -> None:
    """Back to dArkOS: make sure boot.ini on BOOT is dArkOS's, save the log, eject both drives. Unplugging the cable then
    restarts the R36S into dArkOS (dArkOS powers the OTG port itself, so it mustn't start while the PC is connected)."""
    console = connect(port)[0]
    typer.echo(disarm_boot(find_boot(boot)))
    restart(console, "unplug", port)
    typer.echo("It starts dArkOS once the cable is out.")


def main() -> None:
    try:
        app()
    except (ConsoleError, OSError) as e:
        typer.echo(e, err=True)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
