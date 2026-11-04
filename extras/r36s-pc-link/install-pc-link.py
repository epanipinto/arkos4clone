# /// script
# requires-python = ">=3.11"
# dependencies = ["typer>=0.12"]
# ///
r"""Install, update or remove R36S PC Link on the R36S's cards.

Copies "PC Link.sh" (Options > Tools) and its .pclink folder (pclinkd.sh, pclink_agent.py) into the Tools folders you
name, with this PC's public key as .pclink/authorized_keys. Nothing goes on BOOT and no boot file changes. Run
`r36s-remote.py setup-key` first. Reach the cards with USB card mode or the SD slot and check the drive letters in
Explorer: EASYROMS is the dArkOS card's games partition and R36S-GAMES the games card. While dArkOS uses the games card
(/roms2), the Tools menu shows R36S-GAMES\tools; install into both so PC Link survives switching back.

  python install-pc-link.py --tools I:\tools --tools E:\tools --dry-run   # check and show the plan; writes nothing
  python install-pc-link.py --tools I:\tools --tools E:\tools             # install, then Safely Remove the drives
  python install-pc-link.py --tools I:\tools --tools E:\tools --remove    # take PC Link off again
(or uv run install-pc-link.py ..., which brings Typer along)
"""

import ctypes
import os
import re
from pathlib import Path
from typing import Annotated, NoReturn

import typer

app = typer.Typer(add_completion=False, pretty_exceptions_show_locals=False)

HERE = Path(__file__).resolve().parent
DEVICE = HERE / "device"
DEFAULT_PUBKEY = Path.home() / ".ssh" / "r36s_pclink_ed25519.pub"
LABELS = ("EASYROMS", "R36S-GAMES")
# File in a Tools folder -> file in device\ ("" = the public key).
FILES = {
    "PC Link.sh": "PC Link.sh",
    ".pclink/pclinkd.sh": ".pclink/pclinkd.sh",
    ".pclink/pclink_agent.py": ".pclink/pclink_agent.py",
    ".pclink/authorized_keys": "",
}
KEY_LINE = re.compile(rb"(ssh-ed25519|ecdsa-sha2-nistp(256|384|521)|ssh-rsa|sk-ssh-ed25519@openssh\.com|sk-ecdsa-sha2-nistp256@openssh\.com) [A-Za-z0-9+/]+={0,3}( [ -~]*)?")


def fail(message: str) -> NoReturn:
    typer.echo(message, err=True)
    raise typer.Exit(1)


def volume_label(path: Path) -> str:
    """Label of the volume holding path (Windows), or "" if there's none."""
    root = Path(path.resolve().anchor)
    label = ctypes.create_unicode_buffer(261)
    if not ctypes.windll.kernel32.GetVolumeInformationW(str(root), label, 261, None, None, None, None, 0):
        return ""
    return label.value


def device_file(rel: str) -> bytes:
    data = (DEVICE / rel).read_bytes()
    if b"\r" in data or not data.isascii():
        fail(f"{DEVICE / rel} must be ASCII with LF line endings: dArkOS runs it as is.")
    first = data.split(b"\n", 1)[0]
    if (rel.endswith(".sh") and first != b"#!/bin/bash") or (rel.endswith(".py") and first != b"#!/usr/bin/python3"):
        fail(f"{DEVICE / rel} starts with {first!r}, not the expected #! line.")
    return data


def public_key(path: Path) -> bytes:
    if not path.is_file():
        fail(f"No public key at {path}. Make one with: uv run r36s-remote.py setup-key")
    data = path.read_bytes()
    if b"PRIVATE KEY" in data:
        fail(f"{path} is a private key. Pass the .pub file: the private key never leaves this PC.")
    lines = [line.strip() for line in data.replace(b"\r", b"").split(b"\n") if line.strip()]
    if len(lines) != 1 or not KEY_LINE.fullmatch(lines[0]):
        fail(f"{path} isn't a single OpenSSH public key line.")
    return lines[0] + b"\n"


def conf_file(subnet: str | None, port: int | None) -> bytes | None:
    if subnet is None and port is None:
        return None
    lines = []
    if subnet is not None:
        if not re.fullmatch(r"(\d{1,3}\.){2}\d{1,3}", subnet) or any(int(p) > 255 for p in subnet.split(".")):
            fail("--subnet is the first three parts of an IPv4 address, e.g. 10.36.36 (the R36S is .1).")
        lines.append(f"subnet={subnet}")
    if port is not None:
        if not 1024 <= port <= 65535:
            fail("--port must be 1024-65535.")
        lines.append(f"port={port}")
    return ("\n".join(lines) + "\n").encode()


@app.command()
def main(
    tools: Annotated[list[Path], typer.Option(help=r"A Tools folder (R36S-GAMES\tools, EASYROMS\tools); repeat for more")],
    pubkey: Annotated[Path, typer.Option(help="This PC's PC Link public key (made by r36s-remote.py setup-key)")] = DEFAULT_PUBKEY,
    subnet: Annotated[str | None, typer.Option(help="USB network instead of 10.36.36 (writes .pclink/pclink.conf)")] = None,
    port: Annotated[int | None, typer.Option(help="SSH port instead of 2222 (writes .pclink/pclink.conf)")] = None,
    remove: Annotated[bool, typer.Option("--remove", help="Delete PC Link from these Tools folders")] = False,
    force: Annotated[bool, typer.Option("--force", help="Accept a folder that isn't named tools on EASYROMS or R36S-GAMES")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Check everything and show the plan; write nothing")] = False,
) -> None:
    """Install, update or remove R36S PC Link (usage at the top of install-pc-link.py)."""
    for folder in tools:
        if not folder.is_dir():
            fail(f"Not a folder: {folder}. The Tools folders are EASYROMS\\tools and R36S-GAMES\\tools.")
        label = volume_label(folder)
        if not force and (folder.name.lower() != "tools" or label not in LABELS):
            fail(f"{folder} is on {label or 'an unlabelled drive'}, not a tools folder on {' or '.join(LABELS)}. Add --force if it's right.")

    if remove:
        names = [*FILES, ".pclink/pclink.conf"]
        plan = [(folder / name, None) for folder in tools for name in names if (folder / name).is_file()]
        for dst, _ in plan:
            typer.echo(f"  delete  {dst}")
        if dry_run:
            typer.echo(f"Dry run: nothing deleted ({len(plan)} files would be).")
            return
        for dst, _ in plan:
            dst.unlink()
        for folder in tools:
            pclink = folder / ".pclink"
            if pclink.is_dir() and not any(pclink.iterdir()):
                pclink.rmdir()
        typer.echo(f"Removed: {len(plan)} files. OTG power comes back with the next restart if PC Link left it off.")
        return

    content = {name: device_file(src) if src else public_key(pubkey) for name, src in FILES.items()}
    if (conf := conf_file(subnet, port)) is not None:
        content[".pclink/pclink.conf"] = conf
    plan = [(folder / name, data) for folder in tools for name, data in content.items()]
    typer.echo(f"Public key: {pubkey} ({content['.pclink/authorized_keys'].split()[0].decode()})")
    todo = []
    for dst, data in plan:
        old = dst.read_bytes() if dst.is_file() else None
        state = "new" if old is None else "same" if old == data else "replace"
        typer.echo(f"  {state:<8}{len(data):>8,}  {dst}")
        if state != "same":
            todo.append((dst, data))
    if dry_run:
        typer.echo(f"Dry run: nothing written ({len(todo)} of {len(plan)} files would be).")
        return
    for dst, data in todo:
        dst.parent.mkdir(exist_ok=True)
        with dst.open("wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
    if bad := [str(dst) for dst, data in plan if dst.read_bytes() != data]:
        fail(f"Read-back failed: {', '.join(bad)}")
    typer.echo(f"Installed: {len(todo)} files written, {len(plan) - len(todo)} already up to date. Use Safely Remove before unplugging.")


if __name__ == "__main__":
    app()
