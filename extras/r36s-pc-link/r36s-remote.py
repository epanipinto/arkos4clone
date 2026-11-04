# /// script
# requires-python = ">=3.11"
# dependencies = ["typer>=0.12"]
# ///
r"""Control and view the R36S's dArkOS from this PC through R36S PC Link (Options > Tools > PC Link on the R36S).

Over the OTG USB cable the R36S is 10.36.36.1; over Wi-Fi, pass its address with --host (or set R36S_HOST).
Everything goes through Windows' own OpenSSH client with the key `setup-key` makes; nothing else is installed.
On the R36S, the commands run as pclink_agent.py (root, through sudo).

  uv run r36s-remote.py setup-key          once: make the key, then run install-pc-link.py
  uv run r36s-remote.py wait               after starting PC Link on the R36S and plugging the cable in
  uv run r36s-remote.py status
  uv run r36s-remote.py screenshot         saves r36s-<date>-<time>.png here (--open to view it)
  uv run r36s-remote.py press a            also: press up up a, press fn+start, hold right --ms 1500
  uv run r36s-remote.py launch "/roms2/snes/Super Metroid (Japan, USA) (En,Ja).zip"
  uv run r36s-remote.py quit-game
  uv run r36s-remote.py es list            es get ThemeSet; es set PowerSaverMode default
  uv run r36s-remote.py logs es            or game, pclink, journal, dmesg
  uv run r36s-remote.py run "ls /roms2/snes | head"      a shell command as ark (--root: with sudo)
  uv run r36s-remote.py shell
"""

import base64
import json
import os
import shlex
import shutil
import socket
import struct
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Annotated, NoReturn

import typer

app = typer.Typer(add_completion=False, pretty_exceptions_show_locals=False, no_args_is_help=True, help=__doc__.split("\n\n")[0])
es_app = typer.Typer(no_args_is_help=True, help="EmulationStation settings. Changing one stops ES, edits es_settings.cfg and starts ES again.")
app.add_typer(es_app, name="es")

DEFAULT_HOST = "10.36.36.1"
DEFAULT_PORT = 2222
USER = "ark"
SSH_DIR = Path.home() / ".ssh"
DEFAULT_KEY = SSH_DIR / "r36s_pclink_ed25519"
KNOWN_HOSTS = SSH_DIR / "r36s_pclink_known_hosts"
HOST_ALIAS = "r36s-pclink"  # one pinned host key for the USB and the Wi-Fi address
AGENT = "/run/pclink/bin/pclink_agent.py"
LINK = "/run/pclink/bin/pclinkd.sh"
BUTTONS = ("up", "down", "left", "right", "a", "b", "x", "y", "l1", "r1", "l2", "r2", "select", "start", "l3", "r3", "fn")
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


@dataclass
class Target:
    host: str
    port: int
    key: Path
    known_hosts: Path = KNOWN_HOSTS


def fail(message: str) -> NoReturn:
    typer.echo(message, err=True)
    raise typer.Exit(1)


def ssh_command(t: Target, remote_command: str | None, tty: bool = False) -> list[str]:
    """ssh with the PC Link key only, no password prompts, and the R36S's host key pinned under HOST_ALIAS."""
    ssh = shutil.which("ssh") or fail("OpenSSH's ssh isn't installed (Windows: Settings > System > Optional features > OpenSSH Client).")
    cmd = [
        ssh,
        "-p",
        str(t.port),
        "-i",
        str(t.key),
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=6",
        "-o",
        "ServerAliveInterval=5",
        "-o",
        "ServerAliveCountMax=3",
        "-o",
        f"UserKnownHostsFile={t.known_hosts}",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        f"HostKeyAlias={HOST_ALIAS}",
        "-o",
        "LogLevel=ERROR",
        "-t" if tty else "-T",
        f"{USER}@{t.host}",
    ]
    return cmd + ([remote_command] if remote_command else [])


def remote_line(argv: list[str], root: bool = False) -> str:
    return shlex.join((["sudo", "-n"] if root else []) + argv)


def key_fingerprint(key: Path) -> str:
    keygen, pub = shutil.which("ssh-keygen"), Path(f"{key}.pub")
    if not keygen or not pub.is_file():
        return "fingerprint unknown"
    words = subprocess.run([keygen, "-l", "-f", str(pub)], capture_output=True, text=True, check=False).stdout.split()
    return words[1] if len(words) > 1 else "fingerprint unknown"


def ssh_hint(t: Target, stderr: str) -> str:
    text = stderr.strip() or "no answer"
    if "Permission denied" in text:
        return (
            f"{text}\nThe R36S refused this PC's key ({key_fingerprint(t.key)}). On the R36S, Options > Tools > PC Link > Show the log"
            " says why (SSH server) and which PC key it takes; if that's another key, run install-pc-link.py again."
        )
    if "kex_exchange_identification" in text:
        return f"{text}\nThe R36S closed the connection straight away: Options > Tools > PC Link > Show the log on the R36S says why."
    if "REMOTE HOST IDENTIFICATION HAS CHANGED" in text or "Host key verification failed" in text:
        return f"{text}\nThe R36S's PC Link host key changed (re-flashed?). If that's expected: r36s-remote forget-host"
    if not t.key.is_file():
        return f"{text}\nNo key at {t.key}: run r36s-remote setup-key first."
    return f"{text}\nIs PC Link on (Options > Tools > PC Link) and the cable in? Try: r36s-remote wait"


def run_ssh(t: Target, argv: list[str], root: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(ssh_command(t, remote_line(argv, root)), check=False, capture_output=True, text=True, encoding="utf-8", errors="replace")


def agent(ctx: typer.Context, *args: str) -> subprocess.CompletedProcess:
    t: Target = ctx.obj
    result = run_ssh(t, ["/usr/bin/python3", AGENT, *args], root=True)
    if result.returncode == 255:
        fail(f"SSH to {t.host}:{t.port} failed: {ssh_hint(t, result.stderr)}")
    if result.returncode:
        fail(result.stderr.strip().removeprefix("error: ") or f"the R36S agent failed (exit {result.returncode})")
    return result


def agent_json(ctx: typer.Context, *args: str):
    return json.loads(agent(ctx, *args).stdout)


def check_buttons(specs: list[str]) -> None:
    for spec in specs:
        if bad := [n for n in spec.lower().split("+") if n not in BUTTONS]:
            fail(f"Unknown button {', '.join(map(repr, bad))} in {spec!r}. Buttons: {', '.join(BUTTONS)}; join with + to press together.")


def local_address_towards(host: str) -> str | None:
    """This PC's address on the way to host (no packet is sent), or None without a route."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect((host, 9))
            return s.getsockname()[0]
    except OSError:
        return None


def png_size(data: bytes) -> tuple[int, int]:
    if not data.startswith(PNG_SIGNATURE) or data[12:16] != b"IHDR":
        fail("The R36S didn't send a PNG.")
    return struct.unpack(">II", data[16:24])


@app.callback()
def main(
    ctx: typer.Context,
    host: Annotated[str, typer.Option(envvar="R36S_HOST", help="The R36S: 10.36.36.1 over USB, its Wi-Fi address over Wi-Fi")] = DEFAULT_HOST,
    port: Annotated[int, typer.Option(envvar="R36S_PORT", help="PC Link's SSH port")] = DEFAULT_PORT,
    key: Annotated[Path, typer.Option(envvar="R36S_KEY", help="Private key made by setup-key")] = DEFAULT_KEY,
    known_hosts: Annotated[Path, typer.Option(envvar="R36S_KNOWN_HOSTS", hidden=True)] = KNOWN_HOSTS,
) -> None:
    ctx.obj = Target(host, port, key, known_hosts)


@app.command("setup-key")
def setup_key(ctx: typer.Context, force: Annotated[bool, typer.Option("--force", help="Replace an existing key")] = False) -> None:
    """Make this PC's PC Link key (ed25519, no passphrase) in ~/.ssh. Prints its fingerprint, never the key."""
    t: Target = ctx.obj
    keygen = shutil.which("ssh-keygen") or fail("OpenSSH's ssh-keygen isn't installed.")
    pub = t.key.with_name(t.key.name + ".pub")
    if t.key.exists() and not force:
        typer.echo(f"Key already there: {t.key}")
    else:
        t.key.parent.mkdir(parents=True, exist_ok=True)
        for old in (t.key, pub):
            old.unlink(missing_ok=True)
        subprocess.run([keygen, "-q", "-t", "ed25519", "-N", "", "-C", f"r36s-pclink@{socket.gethostname()}", "-f", str(t.key)], check=True)
        typer.echo(f"Made {t.key}")
    fingerprint = subprocess.run([keygen, "-l", "-f", str(pub)], capture_output=True, text=True, check=True).stdout.strip()
    typer.echo(f"Fingerprint: {fingerprint}")
    typer.echo(f"Next: put the public key on the dArkOS card with install-pc-link.py (it reads {pub}).")


@app.command()
def wait(ctx: typer.Context, timeout: Annotated[float, typer.Option(help="Seconds to wait")] = 180) -> None:
    """Wait until PC Link answers (the USB network, then SSH with this PC's key)."""
    t: Target = ctx.obj
    deadline = time.monotonic() + timeout
    last = ""
    while True:
        here = local_address_towards(t.host)
        try:
            with socket.create_connection((t.host, t.port), timeout=2):
                pass
            result = run_ssh(t, ["/usr/bin/python3", AGENT, "ping"], root=True)
            if result.returncode == 0:
                typer.echo(f"PC Link is up: {USER}@{t.host}:{t.port}" + (f" (this PC is {here})" if here else ""))
                return
            note = f"SSH: {ssh_hint(t, result.stderr)}" if result.returncode == 255 else f"agent: {result.stderr.strip()}"
        except OSError as e:
            if t.host == DEFAULT_HOST and not (here or "").startswith(DEFAULT_HOST.rsplit(".", 1)[0] + "."):
                note = "no USB network yet (cable in? Device Manager > Network adapters should show a Remote NDIS device)"
            else:
                note = f"{t.host}:{t.port} doesn't answer ({e.strerror or e})"
        if note != last:
            typer.echo(f"Waiting: {note}")
            last = note
        if time.monotonic() >= deadline:
            fail(f"No PC Link at {t.host}:{t.port} after {timeout:.0f} s.")
        time.sleep(1)


@app.command()
def status(ctx: typer.Context, as_json: Annotated[bool, typer.Option("--json", help="Print the raw JSON")] = False) -> None:
    """Link, OTG power, EmulationStation, the running game, display, battery."""
    data = agent_json(ctx, "status")
    if as_json:
        typer.echo(json.dumps(data, indent=2))
        return
    link = data["link"]
    if link.get("state") == "off":
        typer.echo("Link: off")
    else:
        where = f"PC {link['pc']}" if link.get("pc") else link.get("ip", "")
        typer.echo(f"Link: {link.get('mode', '?')}, {link.get('state', '?')}{f' ({where})' if where else ''}; USB: {link.get('udc') or '-'}")
        for key in ("warning", "message"):
            if link.get(key):
                typer.echo(f"  {link[key]}")
    if otg := data.get("otg"):
        typer.echo(f"OTG power: {otg.get('power', '?')}; 5 V seen on the port: {'yes' if otg.get('5v_seen') else 'no'}")
    typer.echo(f"EmulationStation: {data['es']}")
    for game in data["game"]:
        typer.echo(f"Game: {game['comm']} (pid {game['pid']}): {game['cmdline']}")
    display = data.get("display", {})
    typer.echo(f"Display: {display.get('mode') or display.get('error', '?')}")
    battery = data.get("battery", {})
    typer.echo(f"Battery: {battery.get('capacity', '?')}% ({battery.get('status', '?')})")
    hours, rest = divmod(data["uptime_s"], 3600)
    typer.echo(f"Up {hours}:{rest // 60:02d}; R36S time: {data['time']}")


@app.command()
def screenshot(
    ctx: typer.Context,
    path: Annotated[Path | None, typer.Argument(help="File or folder (default: r36s-<date>-<time>.png here)")] = None,
    source: Annotated[str, typer.Option(help="auto (the screen's DRM framebuffer, else fb0), drm or fb0")] = "auto",
    open_: Annotated[bool, typer.Option("--open", help="Open it afterwards")] = False,
) -> None:
    """Save what's on the R36S's screen (EmulationStation, a game, a menu) as a PNG."""
    if source not in ("auto", "drm", "fb0"):
        fail("--source is auto, drm or fb0.")
    result = agent(ctx, "screenshot", "--base64", "--source", source)
    data = base64.b64decode(result.stdout)
    width, height = png_size(data)
    name = f"r36s-{datetime.now():%Y%m%d-%H%M%S}.png"
    path = Path.cwd() / name if path is None else path / name if path.is_dir() else path
    path.write_bytes(data)
    typer.echo(f"{path} ({width}x{height}, {result.stderr.strip() or 'from the R36S'})")
    if open_:
        os.startfile(path)


@app.command()
def buttons(ctx: typer.Context) -> None:
    """The button names, and whether the R36S's pad reports each one."""
    data = agent_json(ctx, "buttons")
    typer.echo(f"Pad: {data['device']}")
    for name, info in data["buttons"].items():
        typer.echo(f"  {name:<7} code {info['code']:#05x}{'' if info['supported'] else '  (not on this pad)'}")


@app.command()
def press(
    ctx: typer.Context,
    buttons: Annotated[list[str], typer.Argument(help="Buttons in order; a+b presses them together (fn+start)")],
    hold_ms: Annotated[int | None, typer.Option(help="How long each press lasts; default 50 ms in a Tools menu, 120 ms in ES or a game", min=1, max=60000)] = None,
    gap_ms: Annotated[int, typer.Option(help="Pause between presses", min=0, max=60000)] = 250,
    trace: Annotated[bool, typer.Option("--trace", help="Show every key event the press sets off, per input device")] = False,
) -> None:
    """Press buttons on the R36S's own pad (player 1), as if pressed by hand."""
    check_buttons(buttons)
    args = ["press", *buttons, *(["--hold-ms", str(hold_ms)] if hold_ms is not None else []), "--gap-ms", str(gap_ms), *(["--trace"] if trace else [])]
    result = agent_json(ctx, *args)
    held = f" ({result['hold_ms']} ms: {result['hold_why']})" if "hold_ms" in result else ""
    typer.echo(f"Pressed: {' '.join(result['pressed'])}{held}")
    for dev in result.get("trace", []):
        repeat = f"kernel key repeat after {dev['repeat_ms'][0]} ms" if dev.get("repeat_ms") else "no kernel key repeat"
        events = ", ".join(f"+{e['t_ms']} ms {e['key']} {e['value']}" for e in dev["events"]) or "nothing"
        typer.echo(f"  {dev['device']} ({dev['node']}, {repeat}): {events}")


@app.command()
def hold(
    ctx: typer.Context,
    buttons: Annotated[str, typer.Argument(help="A button, or buttons joined with + (right+b)")],
    ms: Annotated[int, typer.Option(help="How long to hold")] = 1000,
) -> None:
    """Hold a button (or several together) down for a while, then release."""
    check_buttons([buttons])
    agent_json(ctx, "press", buttons, "--hold-ms", str(ms))
    typer.echo(f"Held {buttons} for {ms} ms")


@app.command("release-all")
def release_all(ctx: typer.Context) -> None:
    """Release every pad button (if an interrupted press left one held)."""
    agent_json(ctx, "release-all")
    typer.echo("Released all buttons.")


@app.command()
def launch(
    ctx: typer.Context,
    rom: Annotated[str, typer.Argument(help="The game's path on the R36S, e.g. /roms2/snes/<file>")],
    system: Annotated[str | None, typer.Option(help="ES system name, if the folder alone doesn't say")] = None,
    emulator: Annotated[str | None, typer.Option(help="Instead of ES's choice (retroarch, retroarch32, ...)")] = None,
    core: Annotated[str | None, typer.Option(help="Instead of ES's choice")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Show the command ES would run; start nothing")] = False,
    force: Annotated[bool, typer.Option("--force", help="Stop a running game first (without saving)")] = False,
) -> None:
    """Start a game with the same command, emulator and core EmulationStation would use. ES comes back when it ends."""
    args = [rom] + [f"--{k}={v}" for k, v in (("system", system), ("emulator", emulator), ("core", core)) if v]
    plan = agent_json(ctx, "launch", *args, *(["--dry-run"] if dry_run else []), *(["--force"] if force else []))
    governor = f" (governor {plan['governor']})" if plan.get("governor") else ""
    typer.echo(f"{plan['system']}: {plan['emulator']} / {plan['core'] or '-'}{governor}")
    typer.echo(f"  {plan['command']}")
    if plan.get("warning"):
        typer.echo(f"  Warning: {plan['warning']}")
    typer.echo("Dry run: nothing started." if dry_run else f"Started. Its output goes to {plan['log']} (r36s-remote logs game).")


@app.command("quit-game")
def quit_game(ctx: typer.Context, force: Annotated[bool, typer.Option("--force", help="Kill it if it doesn't quit in 10 s")] = False) -> None:
    """Quit the running game the way closing it does (SIGTERM: RetroArch saves SRAM); ES comes back."""
    stopped = agent_json(ctx, "quit-game", *(["--force"] if force else []))["stopped"]
    names = ", ".join(f"{p['comm']} ({p['pid']})" for p in stopped)
    typer.echo(f"Stopped: {names}" if stopped else "No game was running.")


@es_app.command("list")
def es_list(ctx: typer.Context) -> None:
    """Every setting in es_settings.cfg."""
    for name, value in agent_json(ctx, "es-list").items():
        typer.echo(f"{name} = {value}")


@es_app.command("get")
def es_get(ctx: typer.Context, name: str) -> None:
    """One setting."""
    data = agent_json(ctx, "es-get", name)
    typer.echo(f"{data['name']} = {data['value']} ({data['type']})")


@es_app.command("set")
def es_set(
    ctx: typer.Context,
    name: str,
    value: str,
    kind: Annotated[str | None, typer.Option("--type", help="bool, int, float or string, for a setting not in the file yet")] = None,
    force: Annotated[bool, typer.Option("--force", help="Even if a game is running (it's stopped without saving)")] = False,
) -> None:
    """Change a setting: stops ES, backs up and edits es_settings.cfg, starts ES."""
    args = [name, value] + (["--type", kind] if kind else []) + (["--force"] if force else [])
    data = agent_json(ctx, "es-set", *args)
    typer.echo(f"{data['name']} = {data['value']} ({data['type']}); backup {data['backup']}; ES {'restarted' if data['es_restarted'] else 'was not running'}")


@es_app.command("restart")
def es_restart(ctx: typer.Context, force: Annotated[bool, typer.Option("--force")] = False) -> None:
    """Restart EmulationStation (it rereads its settings, systems and game lists)."""
    agent_json(ctx, "es-restart", *(["--force"] if force else []))
    typer.echo("EmulationStation restarted.")


@app.command()
def logs(
    ctx: typer.Context,
    kind: Annotated[str, typer.Argument(help="es, game (games started with launch), pclink, journal or dmesg")] = "pclink",
    lines: Annotated[int, typer.Option("--lines", "-n")] = 60,
) -> None:
    """Show the end of a log from the R36S."""
    if kind not in ("es", "game", "pclink", "journal", "dmesg"):
        fail("Logs: es, game, pclink, journal or dmesg.")
    typer.echo(agent(ctx, "logs", kind, "--lines", str(lines)).stdout.rstrip("\n"))


@app.command(context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def run(
    ctx: typer.Context,
    command: Annotated[list[str], typer.Argument(help="One argument: a shell command line. Several: a command and its arguments")],
    root: Annotated[bool, typer.Option("--root", help="Run it with sudo")] = False,
) -> None:
    """Run a command on the R36S as ark and show its output; exits with its exit code."""
    t: Target = ctx.obj
    line = command[0] if len(command) == 1 else shlex.join(command)
    if root:
        line = remote_line(["/bin/bash", "-c", line], root=True)
    code = subprocess.call(ssh_command(t, line))
    if code == 255:
        fail(f"SSH to {t.host}:{t.port} failed. {ssh_hint(t, '')}")
    raise typer.Exit(code)


@app.command()
def shell(ctx: typer.Context) -> None:
    """An interactive shell on the R36S (as ark; sudo works without a password)."""
    raise typer.Exit(subprocess.call(ssh_command(ctx.obj, None, tty=True)))


@app.command("stop-link")
def stop_link(ctx: typer.Context) -> None:
    """Stop PC Link from here. Over USB the R36S then keeps its OTG power off until "OTG power on" or a restart."""
    t: Target = ctx.obj
    result = run_ssh(t, ["/bin/bash", LINK, "stop", "--no-block"], root=True)
    # The R36S says "stop sent" just before stopping; this SSH session can then end with the link (exit 255).
    sent = "stop sent" in result.stdout.splitlines()
    if result.returncode == 255 and not sent:
        fail(f"SSH to {t.host}:{t.port} failed: {ssh_hint(t, result.stderr)}")
    if result.returncode not in (0, 255):
        fail(result.stderr.strip() or result.stdout.strip() or f"stop failed (exit {result.returncode})")
    if not sent:
        typer.echo(result.stdout.strip())
        return
    typer.echo("PC Link is stopping on the R36S. After a USB link its OTG power stays off until OTG power on, or a restart.")


@app.command("forget-host")
def forget_host(ctx: typer.Context) -> None:
    """Forget the pinned PC Link host key (after re-flashing dArkOS, which makes a new one)."""
    keygen = shutil.which("ssh-keygen") or fail("OpenSSH's ssh-keygen isn't installed.")
    known: Path = ctx.obj.known_hosts
    if not known.is_file():
        typer.echo("No PC Link host key was pinned.")
        return
    subprocess.run([keygen, "-R", HOST_ALIAS, "-f", str(known)], capture_output=True, check=True)
    typer.echo(f"Forgot the R36S's host key ({known}); the next connection pins the new one.")


if __name__ == "__main__":
    app()
