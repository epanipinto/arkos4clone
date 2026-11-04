#!/usr/bin/python3
"""R36S PC Link agent: the dArkOS side of r36s-remote.py, run as root over SSH:

    sudo -n /usr/bin/python3 /run/pclink/bin/pclink_agent.py <command> [options]

Screenshots (the DRM framebuffer on screen, else fb0), button presses (written into the built-in "GO-Super Gamepad"
input device, so EmulationStation and RetroArch see player 1's own pad), EmulationStation settings (with ES stopped,
because ES rewrites es_settings.cfg), launching and quitting games the way ES does, logs and status.
Commands print JSON, except screenshot (PNG, or base64 text with --base64). Errors go to stderr with exit code 1.
Only the standard library is used (dArkOS4Clone has Python 3.13). /run/pclink/agent.json (root-only) can override
the paths in DEFAULTS; the off-device tests use it.
"""

import argparse
import base64
import ctypes
import fcntl
import json
import mmap
import os
import pwd
import re
import signal
import struct
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
import zlib
from pathlib import Path
from xml.sax.saxutils import escape, unescape

CONFIG_FILE = Path("/run/pclink/agent.json")
DEFAULTS = {
    "sys": "/sys",
    "dev": "/dev",
    "proc": "/proc",
    "dri_card": "/dev/dri/card0",
    "es_settings": "/home/ark/.emulationstation/es_settings.cfg",
    "es_systems": "/etc/emulationstation/es_systems.cfg",
    "es_log": "/home/ark/.emulationstation/es_log.txt",
    "es_unit": "emulationstation",
    "game_unit": "pclink-game",
    "game_log": "/tmp/pclink-game.log",
    "run": "/run/pclink",
    "user": "ark",
    "pad_name": "GO-Super Gamepad",
    "devmem": "busybox devmem",
}

# Button names as printed on the R36S (A right, B bottom, X top, Y left) -> key codes from dArkOS4Clone's board file
# (odroidgo3-joypad sw1..sw22). The volume buttons are a separate device; FN + Left/Right changes the volume.
BUTTONS = {
    "up": 0x220,
    "down": 0x221,
    "left": 0x222,
    "right": 0x223,
    "a": 0x131,
    "b": 0x130,
    "x": 0x133,
    "y": 0x134,
    "l1": 0x136,
    "r1": 0x137,
    "l2": 0x138,
    "r2": 0x139,
    "select": 0x2C0,
    "start": 0x2C1,
    "l3": 0x2C2,
    "r3": 0x2C3,
    "fn": 0x2C4,
}
EV_SYN, EV_KEY = 0x00, 0x01

GPIO3_DR = 0xFF270000  # bit 4: GPIO3_A4, dArkOS's vbus_host (OTG 5 V) switch
GRF_OTG_CON = 0xFF2C0100  # USB2PHY GRF: bit 9 iddig_en, bit 10 iddig_output
GRF_OTG_ST = 0xFF2C0120  # bit 9 utmi_bvalid (5 V seen on the OTG port), bit 6 utmi_iddig


class AgentError(Exception):
    pass


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    if CONFIG_FILE.is_file():
        cfg.update(json.loads(CONFIG_FILE.read_text()))
    return cfg


CFG = load_config()


def out(obj) -> None:
    sys.stdout.write(json.dumps(obj, indent=1) + "\n")


def run(cmd: list[str], check: bool = False, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, check=check, **kw)


# ---------------------------------------------------------------------------------------------------------- screen


def _ioc(direction: int, nr: int, size: int, kind: str = "d") -> int:
    return (direction << 30) | (size << 16) | (ord(kind) << 8) | nr


IOC_WRITE, IOC_READ = 1, 2
DRM_IOCTL_GEM_CLOSE = _ioc(IOC_WRITE, 0x09, 8)
DRM_IOCTL_PRIME_HANDLE_TO_FD = _ioc(IOC_READ | IOC_WRITE, 0x2D, 12)
DRM_IOCTL_MODE_GETRESOURCES = _ioc(IOC_READ | IOC_WRITE, 0xA0, 64)
DRM_IOCTL_MODE_GETCRTC = _ioc(IOC_READ | IOC_WRITE, 0xA1, 104)
DRM_IOCTL_MODE_GETFB = _ioc(IOC_READ | IOC_WRITE, 0xAD, 28)
DRM_IOCTL_MODE_MAP_DUMB = _ioc(IOC_READ | IOC_WRITE, 0xB3, 16)
DRM_CLOEXEC = os.O_CLOEXEC  # drm.h: #define DRM_CLOEXEC O_CLOEXEC (the PRIME export flag)
EVIOCGBIT_KEY = _ioc(IOC_READ, 0x20 + EV_KEY, 96, "E")


def ioctl(fd: int, request: int, buf: bytearray) -> bytearray:
    fcntl.ioctl(fd, request, buf, True)
    return buf


def drm_active_crtcs(fd: int) -> list[dict]:
    """CRTCs showing a framebuffer: id, fb, x, y and the mode's width, height and refresh rate."""
    res = ioctl(fd, DRM_IOCTL_MODE_GETRESOURCES, bytearray(64))
    count = struct.unpack_from("<I", res, 36)[0]
    ids = (ctypes.c_uint32 * max(count, 1))()
    res = bytearray(struct.pack("<QQQQ8I", 0, ctypes.addressof(ids), 0, 0, 0, count, 0, 0, 0, 0, 0, 0))
    ioctl(fd, DRM_IOCTL_MODE_GETRESOURCES, res)
    crtcs = []
    for crtc_id in ids[:count]:
        buf = bytearray(104)
        struct.pack_into("<I", buf, 12, crtc_id)
        ioctl(fd, DRM_IOCTL_MODE_GETCRTC, buf)
        fb_id, x, y, _gamma, mode_valid = struct.unpack_from("<5I", buf, 16)
        if fb_id and mode_valid:
            width, height = struct.unpack_from("<H", buf, 40)[0], struct.unpack_from("<H", buf, 50)[0]
            crtcs.append({"crtc": crtc_id, "fb": fb_id, "x": x, "y": y, "width": width, "height": height, "refresh": struct.unpack_from("<I", buf, 60)[0]})
    return crtcs


def drm_read_fb(fd: int, crtc: dict) -> dict:
    """Pixels of the framebuffer on crtc. Needs root: GETFB only returns a buffer handle with CAP_SYS_ADMIN."""
    fb = ioctl(fd, DRM_IOCTL_MODE_GETFB, bytearray(struct.pack("<7I", crtc["fb"], 0, 0, 0, 0, 0, 0)))
    _fb_id, width, height, pitch, bpp, depth, handle = struct.unpack("<7I", fb)
    if not handle:
        raise AgentError("DRM gave no buffer handle for the framebuffer on screen (the agent must run as root)")
    size = pitch * height
    try:
        try:
            dumb = ioctl(fd, DRM_IOCTL_MODE_MAP_DUMB, bytearray(struct.pack("<IIQ", handle, 0, 0)))
            with mmap.mmap(fd, size, mmap.MAP_SHARED, mmap.PROT_READ, offset=struct.unpack_from("<Q", dumb, 8)[0]) as m:
                data = m[:size]
        except OSError:
            prime = ioctl(fd, DRM_IOCTL_PRIME_HANDLE_TO_FD, bytearray(struct.pack("<IIi", handle, DRM_CLOEXEC, -1)))
            buf_fd = struct.unpack_from("<i", prime, 8)[0]
            try:
                with mmap.mmap(buf_fd, size, mmap.MAP_SHARED, mmap.PROT_READ) as m:
                    data = m[:size]
            finally:
                os.close(buf_fd)
    finally:
        fcntl.ioctl(fd, DRM_IOCTL_GEM_CLOSE, struct.pack("<II", handle, 0))
    # The part of the framebuffer the CRTC scans out.
    x, y = crtc["x"], crtc["y"]
    vis_w, vis_h = min(crtc["width"] or width, width - x), min(crtc["height"] or height, height - y)
    start = y * pitch + x * (bpp // 8)
    return {"data": data[start:], "width": vis_w, "height": vis_h, "pitch": pitch, "bpp": bpp, "depth": depth, "source": "drm"}


def fb0_frame() -> dict:
    base = Path(CFG["sys"], "class/graphics/fb0")
    match = re.match(r"\w:(\d+)x(\d+)", (base / "modes").read_text().splitlines()[0])
    if not match:
        raise AgentError("fb0 has no mode")
    width, height = int(match[1]), int(match[2])
    bpp, pitch = int((base / "bits_per_pixel").read_text()), int((base / "stride").read_text())
    pan_x, pan_y = (int(v) for v in (base / "pan").read_text().strip().split(","))
    with open(Path(CFG["dev"], "fb0"), "rb") as f:
        f.seek(pan_y * pitch + pan_x * (bpp // 8))
        data = f.read(pitch * height)
    return {"data": data, "width": width, "height": height, "pitch": pitch, "bpp": bpp, "depth": 24 if bpp == 32 else bpp, "source": "fb0"}


_RGB565 = None


def to_rgb_rows(frame: dict) -> list[bytes]:
    width, height, pitch, bpp, data = frame["width"], frame["height"], frame["pitch"], frame["bpp"], frame["data"]
    rows = []
    if bpp == 32:  # XRGB8888 / ARGB8888, little endian: B G R X in memory
        for y in range(height):
            src = data[y * pitch : y * pitch + width * 4]
            rgb = bytearray(width * 3)
            rgb[0::3], rgb[1::3], rgb[2::3] = src[2::4], src[1::4], src[0::4]
            rows.append(bytes(rgb))
    elif bpp == 16:  # RGB565
        global _RGB565
        if _RGB565 is None:
            _RGB565 = [bytes(((v >> 11 & 31) * 255 // 31, (v >> 5 & 63) * 255 // 63, (v & 31) * 255 // 31)) for v in range(65536)]
        for y in range(height):
            words = struct.unpack_from(f"<{width}H", data, y * pitch)
            rows.append(b"".join(_RGB565[v] for v in words))
    else:
        raise AgentError(f"unsupported framebuffer format: {bpp} bits per pixel, depth {frame['depth']}")
    return rows


def png(width: int, height: int, rows: list[bytes]) -> bytes:
    def chunk(kind: bytes, payload: bytes) -> bytes:
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + row for row in rows)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b"")


def grab_frame(source: str) -> dict:
    errors = []
    if source in ("auto", "drm"):
        try:
            fd = os.open(CFG["dri_card"], os.O_RDWR | os.O_CLOEXEC)
            try:
                crtcs = drm_active_crtcs(fd)
                if not crtcs:
                    raise AgentError("no CRTC is showing a framebuffer (screen off?)")
                return drm_read_fb(fd, crtcs[0])
            finally:
                os.close(fd)
        except (OSError, AgentError) as e:
            errors.append(f"drm: {e}")
            if source == "drm":
                raise AgentError("; ".join(errors)) from e
    try:
        return fb0_frame()
    except (OSError, AgentError, ValueError, IndexError) as e:
        errors.append(f"fb0: {e}")
    raise AgentError("; ".join(errors))


def cmd_screenshot(args: argparse.Namespace) -> None:
    frame = grab_frame(args.source)
    data = png(frame["width"], frame["height"], to_rgb_rows(frame))
    if args.base64:
        sys.stdout.write(base64.b64encode(data).decode() + "\n")
    else:
        sys.stdout.buffer.write(data)
    sys.stderr.write(f"{frame['source']} {frame['width']}x{frame['height']} {frame['bpp']}bpp\n")


# ---------------------------------------------------------------------------------------------------------- buttons


def find_pad() -> Path:
    for event in sorted(Path(CFG["sys"], "class/input").glob("event*")):
        try:
            name = (event / "device/name").read_text().strip()
        except OSError:
            continue
        if name == CFG["pad_name"]:
            return Path(CFG["dev"], "input", event.name)
    raise AgentError(f'no input device named "{CFG["pad_name"]}"')


def parse_chords(specs: list[str]) -> list[list[str]]:
    chords = []
    for spec in specs:
        names = [n.strip().lower() for n in spec.split("+")]
        if not all(name in BUTTONS for name in names):
            raise AgentError(f"unknown button in {spec!r}; buttons: {', '.join(BUTTONS)}")
        chords.append(names)
    return chords


def event(kind: int, code: int, value: int) -> bytes:
    return struct.pack("<qqHHi", 0, 0, kind, code, value)


class Pad:
    """The built-in pad's event node, opened for writing: events written to it reach every reader of that device."""

    def __init__(self) -> None:
        self.path = find_pad()
        self.fd = os.open(self.path, os.O_RDWR | os.O_CLOEXEC)
        self.down: list[int] = []
        try:
            caps = ioctl(self.fd, EVIOCGBIT_KEY, bytearray(96))
            self.supported = {name for name, code in BUTTONS.items() if caps[code // 8] >> (code % 8) & 1}
        except OSError:  # the kernel always answers this; an emulator may not
            self.supported = set(BUTTONS)

    def key(self, code: int, value: int) -> None:
        if value:
            self.down.append(code)  # first, so an interrupt right after the write still releases it
        os.write(self.fd, event(EV_KEY, code, value) + event(EV_SYN, 0, 0))
        if not value and code in self.down:
            self.down.remove(code)

    def release_all(self, codes=None) -> None:
        failed = None
        for code in reversed(list(codes if codes is not None else self.down)):
            try:
                self.key(code, 0)
            except OSError as e:
                failed = failed or e
        if failed:
            raise failed

    def close(self) -> None:
        try:
            self.release_all()
        finally:
            os.close(self.fd)


class Interrupted(Exception):
    pass


def _interrupt(signum, frame):
    raise Interrupted(signum)


def press_chords(chords: list[list[str]], hold_ms: int, gap_ms: int) -> list[str]:
    for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT, signal.SIGPIPE):
        signal.signal(sig, _interrupt)
    pad = Pad()
    try:
        if missing := sorted({n for chord in chords for n in chord} - pad.supported):
            raise AgentError(f"{pad.path} doesn't report these buttons: {', '.join(missing)}")
        done = []
        for i, chord in enumerate(chords):
            if i:
                time.sleep(gap_ms / 1000)
            for name in chord:
                pad.key(BUTTONS[name], 1)
            time.sleep(hold_ms / 1000)
            pad.release_all([BUTTONS[n] for n in chord])
            done.append("+".join(chord))
        return done
    finally:
        pad.close()


def cmd_buttons(args: argparse.Namespace) -> None:
    try:
        pad = Pad()
        supported, path = pad.supported, str(pad.path)
        pad.close()
    except (OSError, AgentError) as e:
        supported, path = set(), f"error: {e}"
    out({"device": path, "buttons": {n: {"code": c, "supported": n in supported} for n, c in BUTTONS.items()}})


def cmd_press(args: argparse.Namespace) -> None:
    out({"pressed": press_chords(parse_chords(args.buttons), args.hold_ms, args.gap_ms)})


def cmd_release_all(args: argparse.Namespace) -> None:
    pad = Pad()
    try:
        pad.release_all([BUTTONS[n] for n in BUTTONS if n in pad.supported])
    finally:
        os.close(pad.fd)
    out({"released": sorted(pad.supported)})


# ---------------------------------------------------------------------------------------------- EmulationStation

SETTING = re.compile(r'^(\s*)<(bool|int|float|string) name="([^"]*)" value="([^"]*)"\s*/>\s*$')


def es_parse(text: str) -> dict[str, tuple[str, str]]:
    found = {}
    for line in text.splitlines():
        if m := SETTING.match(line):
            found[unescape(m[3], {"&quot;": '"'})] = (m[2], unescape(m[4], {"&quot;": '"'}))
    return found


def es_kind(value: str) -> str:
    if value in ("true", "false"):
        return "bool"
    if re.fullmatch(r"-?\d+", value):
        return "int"
    if re.fullmatch(r"-?\d+\.\d*", value):
        return "float"
    return "string"


def es_update(text: str, name: str, value: str, kind: str | None = None) -> tuple[str, str]:
    """es_settings.cfg with name set to value, keeping every other line as it was."""
    old = es_parse(text).get(name)
    kind = kind or (old[0] if old else es_kind(value))
    checks = {"bool": r"true|false", "int": r"-?\d+", "float": r"-?\d+(\.\d*)?"}
    if kind in checks and not re.fullmatch(checks[kind], value):
        raise AgentError(f"{name} is a {kind} setting; {value!r} isn't a valid {kind}")
    if kind not in ("bool", "int", "float", "string"):
        raise AgentError(f"unknown setting type {kind!r}")
    line = f'<{kind} name="{escape(name, {chr(34): "&quot;"})}" value="{escape(value, {chr(34): "&quot;"})}" />'
    lines = text.splitlines()
    for i, current in enumerate(lines):
        m = SETTING.match(current)
        if m and unescape(m[3], {"&quot;": '"'}) == name:
            lines[i] = m[1] + line
            break
    else:
        if not lines:
            lines = ['<?xml version="1.0"?>']
        lines.append(line)
    return "\n".join(lines) + "\n", kind


def unit_active(unit: str) -> bool:
    return run(["systemctl", "is-active", "--quiet", unit]).returncode == 0


def unit_state(unit: str) -> str:
    return run(["systemctl", "show", "-p", "ActiveState", "--value", unit]).stdout.strip()


def proc_info(pid: int) -> dict:
    base = Path(CFG["proc"], str(pid))
    try:
        cmdline = (base / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace").strip()
        comm = (base / "comm").read_text().strip()
    except OSError:
        return {}
    try:
        exe = os.readlink(base / "exe")
    except OSError:
        exe = ""
    return {"pid": pid, "comm": comm, "exe": exe, "cmdline": cmdline}


def unit_pids(unit: str) -> list[int]:
    """The unit's main process and every descendant, from /proc (works with the cgroup v1 of kernel 4.4)."""
    try:
        main = int(run(["systemctl", "show", "-p", "MainPID", "--value", unit]).stdout.strip() or 0)
    except ValueError:
        return []
    if main <= 0:
        return []
    children: dict[int, list[int]] = {}
    for stat in Path(CFG["proc"]).glob("[0-9]*/stat"):
        try:
            text = stat.read_text()
            ppid = int(text[text.rindex(")") + 2 :].split()[1])
        except (OSError, ValueError, IndexError):
            continue
        children.setdefault(ppid, []).append(int(stat.parent.name))
    pids, todo = [], [main]
    while todo:
        pid = todo.pop()
        pids.append(pid)
        todo.extend(children.get(pid, []))
    return pids


def game_processes() -> list[dict]:
    """Processes of a running game: whatever ES (or a PC Link launch) runs besides ES itself and our wrapper."""
    procs = []
    for unit in (CFG["es_unit"], CFG["game_unit"]):
        for pid in unit_pids(unit):
            info = proc_info(pid)
            if not info or info["exe"].endswith("/emulationstation/emulationstation") or "emulationstation.sh" in info["cmdline"]:
                continue
            if "pclink_agent.py _game" in info["cmdline"]:
                continue
            procs.append(info)
    return procs


def clear_games(force: bool) -> None:
    """No game may be running: refuse, or with force stop it first (SIGTERM, SIGKILL after 10 s)."""
    if games := game_processes():
        if not force:
            names = ", ".join(sorted({g["comm"] for g in games}))
            raise AgentError(f"a game is running ({names}); quit it first, or add --force to stop it without saving")
        quit_games(force=True)


def es_stop() -> bool:
    was = unit_active(CFG["es_unit"])
    if was:
        run(["systemctl", "stop", CFG["es_unit"]], check=True)
    return was


def es_start() -> None:
    run(["systemctl", "start", CFG["es_unit"]], check=True)


def cmd_es_list(args: argparse.Namespace) -> None:
    out({name: value for name, (kind, value) in sorted(es_parse(Path(CFG["es_settings"]).read_text()).items())})


def cmd_es_get(args: argparse.Namespace) -> None:
    found = es_parse(Path(CFG["es_settings"]).read_text())
    if args.name not in found:
        raise AgentError(f"{args.name} isn't in es_settings.cfg (ES then uses its built-in default)")
    out({"name": args.name, "type": found[args.name][0], "value": found[args.name][1]})


def cmd_es_set(args: argparse.Namespace) -> None:
    path = Path(CFG["es_settings"])
    clear_games(args.force)
    was_running = es_stop()  # ES writes es_settings.cfg when it quits, so edit only once it has
    try:
        text = path.read_text()
        new, kind = es_update(text, args.name, args.value, args.type)
        backup = path.with_name(f"{path.name}.pclink-{time.strftime('%Y%m%d-%H%M%S')}")
        backup.write_text(text)
        stat = path.stat()
        tmp = path.with_name(path.name + ".pclink-new")
        tmp.write_text(new)
        os.chown(tmp, stat.st_uid, stat.st_gid)
        os.chmod(tmp, stat.st_mode & 0o7777)
        os.replace(tmp, path)
    finally:
        if was_running or args.start:
            es_start()
    out({"name": args.name, "type": kind, "value": args.value, "backup": str(backup), "es_restarted": was_running or args.start})


def cmd_es_restart(args: argparse.Namespace) -> None:
    clear_games(args.force)
    run(["systemctl", "restart", CFG["es_unit"]], check=True)
    out({"es": "restarted"})


# ---------------------------------------------------------------------------------------------------------- games


def es_escape(path: str) -> str:
    """ES-fcamod's Utils::FileSystem::getEscapedPath: a backslash before each special character not already escaped."""
    for ch in "\\ '\"!$^&*(){}[]?;<>":
        start = 0
        while (offset := path.find(ch, start)) != -1:
            start = offset + 1
            if offset == 0 or path[offset - 1] != "\\":
                path = path[:offset] + "\\" + path[offset:]
                start += 1
    return path


def user_home() -> str:
    return pwd.getpwnam(CFG["user"]).pw_dir


def es_systems_file() -> Path:
    """ES-fcamod's SystemData::getConfigPath: ~/.emulationstation/es_systems.cfg if there is one, else /etc's."""
    user_cfg = Path(user_home(), ".emulationstation/es_systems.cfg")
    return user_cfg if user_cfg.is_file() else Path(CFG["es_systems"])


def load_systems() -> list[dict]:
    systems = []
    for node in ET.parse(es_systems_file()).getroot().iter("system"):
        emulators = []
        for emu in node.findall("emulators/emulator"):
            governors = ["performance", "ondemand", "powersave"] + [g.text or "" for g in emu.findall("governors/governor")]
            emulators.append({"name": emu.get("name", ""), "command": emu.get("command", ""), "cores": [c.text or "" for c in emu.findall("cores/core")], "governors": governors})
        path = node.findtext("path", "")
        if path.startswith("~"):  # ES expands ~ to ark's home; the agent runs as root
            path = user_home() + path[1:]
        systems.append(
            {
                "name": node.findtext("name", ""),
                "path": path.rstrip("/"),
                "extensions": (node.findtext("extension") or "").lower().split(),
                "command": node.findtext("command", ""),
                "emulators": emulators,
            }
        )
    return systems


def game_metadata(system: dict, rom: str) -> dict:
    """The game's entry in the gamelist ES reads (SystemData::getGamelistPath): the first that exists of the system
    folder's, ~/.emulationstation/gamelists/<system>/ and /etc/emulationstation/gamelists/<system>/."""
    for gamelist in (
        Path(system["path"], "gamelist.xml"),
        Path(user_home(), ".emulationstation/gamelists", system["name"], "gamelist.xml"),
        Path("/etc/emulationstation/gamelists", system["name"], "gamelist.xml"),
    ):
        if gamelist.is_file():
            rel = "./" + os.path.relpath(rom, system["path"])
            for game in ET.parse(gamelist).getroot().iter("game"):
                if game.findtext("path") in (rel, rom):
                    return {k: game.findtext(k) or "" for k in ("name", "emulator", "core", "governor")}
            return {}
    return {}


def resolve_launch(rom: str, system: str | None = None, emulator: str | None = None, core: str | None = None) -> dict:
    """The command ES-fcamod's FileData::launchGame would run for rom (same defaults and %...% replacements)."""
    rom = os.path.abspath(rom)
    if not os.path.isfile(rom):
        raise AgentError(f"{rom} isn't a file on the R36S")
    candidates = [s for s in load_systems() if (system is None or s["name"] == system) and rom.startswith(s["path"] + "/")]
    if not candidates:
        raise AgentError(f"no {'system ' + repr(system) if system else 'system'} in es_systems.cfg has {rom} under its path")
    sysd = max(candidates, key=lambda s: len(s["path"]))
    settings = {k: v for k, (_, v) in es_parse(Path(CFG["es_settings"]).read_text()).items()} if Path(CFG["es_settings"]).is_file() else {}
    meta = game_metadata(sysd, rom)
    emus = sysd["emulators"]
    names = [e["name"] for e in emus]

    def default_emulator() -> str:
        wanted = settings.get(f"{sysd['name']}.emulator", "")
        return wanted if wanted in names else (names[0] if names else "")

    def emu_data(name: str) -> dict:
        return next((e for e in emus if e["name"] == name), {"name": name, "command": "", "cores": [], "governors": []})

    def default_core(name: str) -> str:
        cores, wanted = emu_data(name)["cores"], settings.get(f"{sysd['name']}.core", "")
        return wanted if wanted in cores else (cores[0] if cores else "")

    def default_governor(name: str) -> str:
        wanted = settings.get(f"{sysd['name']}.governor", "")
        return wanted if name in names and wanted in emu_data(name)["governors"] else ""

    emu = emulator or meta.get("emulator") or default_emulator()
    if emulator and names and emulator not in names:
        raise AgentError(f"{sysd['name']} has no emulator {emulator!r}; it has {', '.join(names)}")
    chosen_core = core or meta.get("core") or default_core(emu)
    governor = (
        meta.get("governor")
        or default_governor(emu)
        or settings.get(f"{meta.get('name') or Path(rom).stem}.governor", "")
        or settings.get(f"{sysd['name']}.governor", "")
        or default_governor(default_emulator())
        or settings.get("GlobalPerformanceGovernor", "")
    )
    command = emu_data(emu)["command"] or sysd["command"]
    for key, value in (
        ("%GOVERNOR%", governor),
        ("%EMULATOR%", emu),
        ("%CORE%", chosen_core),
        ("%ROM%", es_escape(rom)),
        ("%BASENAME%", Path(rom).stem),
        ("%ROM_RAW%", rom),
        ("%SYSTEM%", sysd["name"]),
        ("%HOME%", user_home()),
    ):
        command = command.replace(key, value)
    plan = {"system": sysd["name"], "system_path": sysd["path"], "emulator": emu, "core": chosen_core, "governor": governor, "rom": rom, "command": command}
    if sysd["extensions"] and not rom.lower().endswith(tuple(sysd["extensions"])):
        plan["warning"] = f"ES wouldn't list this file: {sysd['name']} takes {' '.join(sysd['extensions'])}"
    return plan


def cmd_launch(args: argparse.Namespace) -> None:
    if args.dry_run:
        out(resolve_launch(args.rom, args.system, args.emulator, args.core) | {"dry_run": True})
        return
    if unit_active(CFG["game_unit"]) or game_processes():
        if not args.force:
            raise AgentError("a game is already running; quit it first (r36s-remote quit-game), or add --force")
        quit_games(force=True)
    run_dir = Path(CFG["run"])
    run_dir.mkdir(parents=True, exist_ok=True)
    marker = run_dir / "game.pid"
    marker.unlink(missing_ok=True)
    unit_started = False
    es_stop()  # ES saves its game lists when it quits; resolve the launch after that, as ES would see it
    try:
        plan = resolve_launch(args.rom, args.system, args.emulator, args.core)
        cmd_file = run_dir / "game-command"
        cmd_file.write_text(plan["command"])
        os.chmod(cmd_file, 0o600)
        agent = os.path.abspath(__file__)
        started = run(
            [
                "systemd-run",
                f"--unit={CFG['game_unit']}",
                "--collect",
                "--quiet",
                "--description=Game started by R36S PC Link",
                "-p",
                f"ExecStopPost=/usr/bin/systemctl start {CFG['es_unit']}",  # ES back even if the wrapper is killed
                "/usr/bin/python3",
                agent,
                "_game",
                str(cmd_file),
            ]
        )
        if started.returncode:
            raise AgentError(f"systemd-run failed: {started.stderr.strip()}")
        unit_started = True
        for _ in range(40):
            try:
                pid = int(marker.read_text())
                break
            except (OSError, ValueError):
                time.sleep(0.25)
        else:
            raise AgentError(f"the game didn't start (see {CFG['game_log']}: r36s-remote logs game)")
        time.sleep(0.5)
        if not pid_alive(pid):
            raise AgentError(f"the game ended right away (see {CFG['game_log']}: r36s-remote logs game)")
    except BaseException as e:
        if unit_started:  # its wrapper could still start the game later: stop it before ES comes back
            unit = CFG["game_unit"]
            if run(["systemctl", "stop", unit]).returncode or unit_state(unit) not in ("inactive", "failed"):
                raise AgentError(f"{e}; {unit} didn't stop either, so ES comes back only once it ends") from e
        es_start()
        raise
    out(plan | {"started": True, "pid": pid, "log": CFG["game_log"]})


def cmd_game_wrapper(args: argparse.Namespace) -> None:
    """Run as root by systemd-run: the game as ark with ES's environment, then EmulationStation again, whatever
    happens (the unit's ExecStopPost also starts ES if this wrapper is killed)."""
    try:
        command = Path(args.command_file).read_text()
        user = pwd.getpwnam(CFG["user"])
        env = {
            "HOME": user.pw_dir,
            "USER": user.pw_name,
            "LOGNAME": user.pw_name,
            "SHELL": user.pw_shell,
            "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin",
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "TERM": "linux",
            "XDG_RUNTIME_DIR": f"/run/user/{user.pw_uid}/",
            "SDL_VIDEO_EGL_DRIVER": "libEGL.so",
            "SDL_ASSERT": "always_ignore",
        }
        with open(CFG["game_log"], "wb") as log:
            game = subprocess.Popen(
                ["/bin/sh", "-c", command],
                user=user.pw_uid,
                group=user.pw_gid,
                extra_groups=os.getgrouplist(user.pw_name, user.pw_gid),
                env=env,
                cwd=user.pw_dir,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            Path(CFG["run"], "game.pid").write_text(str(game.pid))
            for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
                signal.signal(sig, lambda signum, frame: game.send_signal(signal.SIGTERM))
            code = game.wait()
            log.write(f"\n[pclink] game exited with code {code}\n".encode())
    finally:
        es_start()


def pid_alive(pid: int) -> bool:
    """True while pid runs; a zombie (killed, not yet reaped by its parent) counts as gone."""
    try:
        stat = Path(CFG["proc"], str(pid), "stat").read_text()
    except OSError:
        return False
    return stat[stat.rindex(")") + 2 :][:1] not in ("Z", "X")


def quit_games(force: bool, timeout: float = 10) -> list[dict]:
    targets = [p for p in game_processes() if p["comm"] not in ("sh", "bash", "sudo")]
    for p in targets:
        try:
            os.kill(p["pid"], signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and any(pid_alive(p["pid"]) for p in targets):
        time.sleep(0.25)
    left = [p for p in targets if pid_alive(p["pid"])]
    if left and force:
        for p in left:
            try:
                os.kill(p["pid"], signal.SIGKILL)
            except ProcessLookupError:
                pass
    elif left:
        raise AgentError(f"still running after {timeout:.0f} s: {', '.join(p['comm'] for p in left)}; add --force to kill")
    return targets


def cmd_quit_game(args: argparse.Namespace) -> None:
    targets = quit_games(args.force)
    out({"stopped": [{"pid": p["pid"], "comm": p["comm"]} for p in targets]})


# ---------------------------------------------------------------------------------------------------- logs, status


def tail(path: Path, lines: int) -> str:
    try:
        return "\n".join(path.read_text(errors="replace").splitlines()[-lines:])
    except OSError as e:
        return f"({path}: {e.strerror})"


def cmd_logs(args: argparse.Namespace) -> None:
    n = args.lines
    if args.kind == "es":
        text = tail(Path(CFG["es_log"]), n)
    elif args.kind == "game":
        text = tail(Path(CFG["game_log"]), n)
    elif args.kind == "pclink":
        text = "\n".join(f"== {name}\n{tail(Path(CFG['run'], name), n)}" for name in ("pclinkd.log", "sshd.log", "dnsmasq.log"))
    elif args.kind == "journal":
        text = run(["journalctl", "-b", "--no-pager", "-n", str(n)]).stdout
    else:
        text = "\n".join(run(["dmesg"]).stdout.splitlines()[-n:])
    sys.stdout.write(text.rstrip("\n") + "\n")


def read_reg(addr: int) -> int | None:
    try:
        result = run([*CFG["devmem"].split(), f"0x{addr:08x}", "32"])
        return int(result.stdout.strip(), 16) if result.returncode == 0 else None
    except (OSError, ValueError):
        return None


def board_ok() -> bool:
    """dArkOS's R36S Type2 layout: OTG 5 V on GPIO3_A4, dwc2 in otg mode (registers are read only if so)."""
    dt = Path(CFG["proc"], "device-tree")
    try:
        gpio3 = (dt / "pinctrl/gpio3@ff270000/phandle").read_bytes()
        return (dt / "vbus_host/gpio").read_bytes() == gpio3 + struct.pack(">II", 4, 0) and (dt / "usb@ff300000/dr_mode").read_bytes().rstrip(b"\0") == b"otg"
    except OSError:
        return False


def cmd_status(args: argparse.Namespace) -> None:
    state = {}
    status_file = Path(CFG["run"], "status")
    if status_file.is_file():
        state = dict(line.split("=", 1) for line in status_file.read_text().splitlines() if "=" in line)
    link = state if state and unit_active("pclink") else {"state": "off"} | ({"last": state.get("state", "")} if state else {})
    otg = {}
    if board_ok():
        dr, st = read_reg(GPIO3_DR), read_reg(GRF_OTG_ST)
        if dr is not None:
            otg["power"] = "on" if dr >> 4 & 1 else "off"
        if st is not None:
            otg["5v_seen"] = bool(st >> 9 & 1)
    display = {}
    try:
        fd = os.open(CFG["dri_card"], os.O_RDWR | os.O_CLOEXEC)
        try:
            if crtcs := drm_active_crtcs(fd):
                display = {"mode": f"{crtcs[0]['width']}x{crtcs[0]['height']}@{crtcs[0]['refresh']}"}
        finally:
            os.close(fd)
    except OSError as e:
        display = {"error": e.strerror}
    battery = {}
    for key in ("capacity", "status"):
        try:
            battery[key] = Path(CFG["sys"], "class/power_supply/battery", key).read_text().strip()
        except OSError:
            pass
    out(
        {
            "link": link,
            "otg": otg,
            "es": "running" if unit_active(CFG["es_unit"]) else "stopped",
            "game": [{"pid": p["pid"], "comm": p["comm"], "cmdline": p["cmdline"][:300]} for p in game_processes()],
            "display": display,
            "battery": battery,
            "uptime_s": round(float(Path(CFG["proc"], "uptime").read_text().split()[0])),
            "time": time.strftime("%Y-%m-%d %H:%M:%S %Z"),
        }
    )


def cmd_ping(args: argparse.Namespace) -> None:
    out({"ok": True, "uid": os.geteuid(), "agent": os.path.abspath(__file__)})


def main(argv: list[str] | None = None) -> int:
    # dArkOS's sudo keeps the caller's PATH (sudoers.d/ark-no-secure-path), which over SSH lacks the sbin folders.
    os.environ["PATH"] = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    parser = argparse.ArgumentParser(prog="pclink_agent.py", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("ping").set_defaults(func=cmd_ping)
    sub.add_parser("status").set_defaults(func=cmd_status)
    p = sub.add_parser("screenshot")
    p.add_argument("--source", choices=("auto", "drm", "fb0"), default="auto")
    p.add_argument("--base64", action="store_true")
    p.set_defaults(func=cmd_screenshot)
    sub.add_parser("buttons").set_defaults(func=cmd_buttons)
    p = sub.add_parser("press")
    p.add_argument("buttons", nargs="+", help="names, or names joined by + to press together (fn+start)")
    p.add_argument("--hold-ms", type=int, default=120)
    p.add_argument("--gap-ms", type=int, default=250)
    p.set_defaults(func=cmd_press)
    sub.add_parser("release-all").set_defaults(func=cmd_release_all)
    sub.add_parser("es-list").set_defaults(func=cmd_es_list)
    p = sub.add_parser("es-get")
    p.add_argument("name")
    p.set_defaults(func=cmd_es_get)
    p = sub.add_parser("es-set")
    p.add_argument("name")
    p.add_argument("value")
    p.add_argument("--type", choices=("bool", "int", "float", "string"))
    p.add_argument("--force", action="store_true")
    p.add_argument("--start", action="store_true", help="start ES afterwards even if it wasn't running")
    p.set_defaults(func=cmd_es_set)
    p = sub.add_parser("es-restart")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_es_restart)
    p = sub.add_parser("launch")
    p.add_argument("rom")
    p.add_argument("--system")
    p.add_argument("--emulator")
    p.add_argument("--core")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_launch)
    p = sub.add_parser("_game")
    p.add_argument("command_file")
    p.set_defaults(func=cmd_game_wrapper)
    p = sub.add_parser("quit-game")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_quit_game)
    p = sub.add_parser("logs")
    p.add_argument("kind", choices=("es", "game", "pclink", "journal", "dmesg"))
    p.add_argument("--lines", type=int, default=60)
    p.set_defaults(func=cmd_logs)
    args = parser.parse_args(argv)
    try:
        args.func(args)
    except (AgentError, OSError, subprocess.CalledProcessError, ET.ParseError) as e:
        detail = e.stderr.strip() if isinstance(e, subprocess.CalledProcessError) and e.stderr else str(e)
        sys.stderr.write(f"error: {detail}\n")
        return 1
    except Interrupted:
        sys.stderr.write("error: interrupted; the buttons were released\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
