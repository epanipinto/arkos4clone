r"""Install or reinstall R36S USB card mode on the dArkOS card, for example after re-flashing dArkOS4Clone.

Copies the card-mode files to the dArkOS card's BOOT partition and "USB Card Mode.sh" (Options > Tools) to the Tools
folders, then reads them back. A reinstall only rewrites files that changed. Everything comes from this folder, except
the files this repository can't ship, which come from the R36S's original EmuELEC card (or a copy of its EMUELEC
partition):
- KERNEL (EmuELEC 4.7's kernel, 20,662,280 bytes): --kernel, else KERNEL on a drive labelled EMUELEC;
- rk3326-evb-lp3-v12-linux.dtb (the R36S's original board file): --dtb, else that file (or its .orig copy) on a drive
  labelled EMUELEC. Card mode needs its OTG port in USB device mode, so the copy on BOOT gets dr_mode = "peripheral";
- busybox-r36s (optional): --busybox, a static arm64 BusyBox 1.38.0 (see README.md). Without it, card mode still shares
  the cards, but has no clock sync and no serial console.
Every file must match usbmode.md5. BOOT's boot.ini must be dArkOS4Clone's own for "clone type2 panel1 amp" (the copy in
BOOT\consoles\clone type2 panel1 amp, or this repository's boot/dArkOS/consoles/clone type2 panel1 amp/boot.ini);
--force accepts any other dArkOS boot.ini. On a freshly flashed card, pick the console with the DTB selector first.
EASYROMS only appears after dArkOS's first boot.

Usage (check the drive letters in Explorer first; D: = BOOT, E: = EASYROMS, I: = the EmuELEC card's EMUELEC):
  python install-usb-card-mode.py --boot D: --tools E:\tools --dry-run   # check and show the plan; writes nothing
  python install-usb-card-mode.py --boot D: --tools E:\tools             # install, then Safely Remove the drives
  Repeat --tools for the games card's tools folder, which is the Tools menu once dArkOS is switched to SD2.
"""

import ctypes
import hashlib
import os
import re
import shutil
import string
import struct
from ctypes import wintypes
from pathlib import Path
from typing import Annotated, NoReturn

import typer

app = typer.Typer(add_completion=False, pretty_exceptions_show_locals=False)

HERE = Path(__file__).resolve().parent
TOOL = HERE / "USB Card Mode.sh"
SAVED_INI = HERE.parents[1] / "boot" / "dArkOS" / "consoles" / "clone type2 panel1 amp" / "boot.ini"
SHIPPED_INI = Path("consoles", "clone type2 panel1 amp", "boot.ini")
DTB = "rk3326-evb-lp3-v12-linux.dtb"
STOCK_DTB_MD5 = "78614fa399cdd178a8d83eaa13f04c68"  # the original board file, OTG port dr_mode = "host"
MARK = b"R36S-USB-CARD-MODE"
MIN_FREE = 64_000_000  # dArkOS updates rewrite Image and uInitrd (about 24 MB) on BOOT
# File on BOOT -> file in this folder ("" = from the EmuELEC card or --busybox). usbmode/usbmode.md5, boot.ini.darkos
# and boot.ini.darkos-original are made from what gets installed.
FILES = {
    "usbmode/KERNEL": "",
    f"usbmode/{DTB}": "",
    "usbmode/r36s-usb.v7.sh": "r36s-usb.v7.sh",
    "usbmode/busybox-r36s": "",
    "usbmode/r36s-link.tar": "r36s-link.tar",
    "usbmode/screens.tar": "screens.tar",
    "post-flash.sh": "post-flash.boot.sh",
    "boot.ini.usbmode": "boot.ini.usbmode",
    "USB-CARD-MODE-RECOVERY.txt": "USB-CARD-MODE-RECOVERY.txt",
}
OPTIONAL = {"usbmode/busybox-r36s"}
FDT_MAGIC, FDT_BEGIN_NODE, FDT_END_NODE, FDT_PROP, FDT_NOP, FDT_END = 0xD00DFEED, 1, 2, 3, 4, 9


def fail(message: str) -> NoReturn:
    typer.echo(message, err=True)
    raise typer.Exit(1)


def md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def volume_info(root: str) -> tuple[str, str, int]:
    """Label, file system and serial number of the volume at root; empty if there is none."""
    label, fs, serial = ctypes.create_unicode_buffer(261), ctypes.create_unicode_buffer(261), wintypes.DWORD()
    if not ctypes.windll.kernel32.GetVolumeInformationW(root, label, 261, ctypes.byref(serial), None, None, fs, 261):
        return "", "", 0
    return label.value, fs.value, serial.value


def emuelec_drives() -> list[Path]:
    """Roots of the drives labelled EMUELEC: the original EmuELEC card's boot partition."""
    k32 = ctypes.windll.kernel32
    k32.SetErrorMode(1)  # SEM_FAILCRITICALERRORS: no "insert a disk" prompts from empty card readers
    mask = k32.GetLogicalDrives()
    drives = [f"{c}:\\" for i, c in enumerate(string.ascii_uppercase) if mask >> i & 1]
    return [Path(d) for d in drives if k32.GetDriveTypeW(d) in (2, 3) and volume_info(d)[0] == "EMUELEC"]


def set_string_prop(blob: bytes, node: str, prop: str, value: str) -> bytes:
    """The flattened device tree blob with string property prop of node (/a/b) set to value, packed as dtc does."""
    magic, total, off_struct, off_strings, off_rsv, version, _, _, size_strings, size_struct = struct.unpack_from(">10I", blob)
    if magic != FDT_MAGIC or version < 17 or total != len(blob) or not off_rsv < off_struct < off_strings:
        raise ValueError("not a device tree blob this can change")
    strings = blob[off_strings : off_strings + size_strings]
    want, path = [part for part in node.split("/") if part], []
    pos, end = off_struct, off_struct + size_struct
    while pos < end:
        (token,) = struct.unpack_from(">I", blob, pos)
        if token == FDT_BEGIN_NODE:
            name_end = blob.index(b"\0", pos + 4)
            path.append(blob[pos + 4 : name_end].decode())
            pos = (name_end + 4) & ~3
        elif token == FDT_END_NODE:
            path.pop()
            pos += 4
        elif token == FDT_PROP:
            length, nameoff = struct.unpack_from(">II", blob, pos + 4)
            data_end = pos + 12 + ((length + 3) & ~3)
            if path[1:] == want and strings[nameoff : strings.index(b"\0", nameoff)].decode() == prop:
                data = value.encode() + b"\0"
                new = struct.pack(">III", FDT_PROP, len(data), nameoff) + data + bytes(-len(data) % 4)
                delta = len(new) - (data_end - pos)
                out = bytearray(blob[:pos] + new + blob[data_end:])
                struct.pack_into(">I", out, 4, total + delta)
                struct.pack_into(">I", out, 12, off_strings + delta)
                struct.pack_into(">I", out, 36, size_struct + delta)
                return bytes(out)
            pos = data_end
        elif token == FDT_NOP:
            pos += 4
        elif token == FDT_END:
            break
        else:
            raise ValueError(f"unexpected device tree token {token:#x} at {pos:#x}")
    raise ValueError(f"no {prop} property in {node}")


def find_kernel(kernel: Path | None, want: str) -> tuple[Path, bytes]:
    candidates = [kernel] if kernel else [d / "KERNEL" for d in emuelec_drives()]
    for path in candidates:
        if path.is_file() and md5(data := path.read_bytes()) == want:
            return path, data
    fail(f"No KERNEL matching usbmode.md5 ({want}) in {', '.join(map(str, candidates)) or 'no EMUELEC drive'}. Point --kernel at EmuELEC 4.7's KERNEL.")


def find_dtb(dtb: Path | None, want: str) -> tuple[Path, bytes]:
    """The card-mode board file: the original one with its OTG port's dr_mode set to "peripheral" (md5 want)."""
    candidates = [dtb] if dtb else [d / name for d in emuelec_drives() for name in (DTB, f"{DTB}.orig")]
    for path in candidates:
        if not path.is_file():
            continue
        data = path.read_bytes()
        if md5(data) == STOCK_DTB_MD5:
            data = set_string_prop(data, "/usb@ff300000", "dr_mode", "peripheral")
        if md5(data) == want:
            return path, data
    fail(f"No original {DTB} (md5 {STOCK_DTB_MD5}) in {', '.join(map(str, candidates)) or 'no EMUELEC drive'}. Point --dtb at the one from the R36S's EmuELEC card.")


def darkos_ini_problem(ini: bytes, boot: Path) -> str:
    """Why ini isn't a working dArkOS boot.ini (the same test as "USB Card Mode.sh", plus the files it loads), or ""."""
    lines = ini.decode("ascii", "replace").replace("\r", "").split("\n")
    if lines[0].strip().lower() != "odroidgoa-uboot-config" or not any(line.startswith("booti ") for line in lines):
        return "isn't a dArkOS boot.ini"
    if MARK in ini:
        return "is the card-mode one (card mode is pending, or U-Boot fell back to dArkOS). Copy boot.ini.darkos over boot.ini first"
    missing = [m[1] for line in lines if (m := re.fullmatch(r"load mmc 1:1 \S+ (\S+)\s*", line)) and not (boot / m[1]).is_file()]
    return f"loads files that aren't on the card: {', '.join(missing)}" if missing else ""


@app.command()
def main(
    boot: Annotated[Path, typer.Option(help="The dArkOS card's BOOT partition, for example D:")],
    tools: Annotated[list[Path] | None, typer.Option(help='A Tools folder to put "USB Card Mode.sh" in; repeat it for more')] = None,
    kernel: Annotated[Path | None, typer.Option(help="EmuELEC 4.7's KERNEL (default: the one on an EMUELEC drive)")] = None,
    dtb: Annotated[Path | None, typer.Option(help=f"The R36S's original {DTB} (default: the one on an EMUELEC drive)")] = None,
    busybox: Annotated[Path | None, typer.Option(help="Static arm64 BusyBox 1.38.0 for clock sync and the serial console (optional)")] = None,
    force: Annotated[bool, typer.Option("--force", help="Accept a dArkOS boot.ini that isn't dArkOS4Clone's clone type2 panel1 amp one")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Check everything and show the plan; write nothing")] = False,
) -> None:
    """Install or reinstall R36S USB card mode on the dArkOS card (usage at the top of install-usb-card-mode.py)."""
    boot = Path(str(boot).rstrip("\\/") + "\\")
    label, fs, serial = volume_info(str(boot))
    if (label, fs) != ("BOOT", "FAT32"):
        fail(f"{boot} is {label or 'no volume'}{f' ({fs})' if fs else ''}, not the dArkOS card's BOOT partition (FAT32, label BOOT).")
    ini_file = boot / "boot.ini"
    if not ini_file.is_file():
        fail(f"{ini_file} is missing. Set up dArkOS first (DTB selector: clone type2 panel1 amp), then run this again.")
    ini = ini_file.read_bytes()
    if problem := darkos_ini_problem(ini, boot):
        fail(f"{ini_file} {problem}.")
    known = [p for p in (boot / SHIPPED_INI, SAVED_INI) if p.is_file() and p.read_bytes() == ini]
    if not known and not force:
        fail(f"{ini_file} differs from dArkOS4Clone's boot.ini for clone type2 panel1 amp ({boot / SHIPPED_INI}, {SAVED_INI}). If you changed it on purpose, add --force.")

    manifest = {name: digest for digest, name in (line.split("  ", 1) for line in (HERE / "usbmode.md5").read_text().splitlines())}
    card_of = {name: name[3:] if name.startswith("../") else f"usbmode/{name}" for name in manifest}
    if unknown := sorted(set(card_of.values()) - FILES.keys()):
        fail(f"usbmode.md5 lists files this installer doesn't install: {', '.join(unknown)}")
    kernel_file, kernel_data = find_kernel(kernel, manifest["KERNEL"])
    dtb_file, dtb_data = find_dtb(dtb, manifest[DTB])
    found = {"usbmode/KERNEL": kernel_data, f"usbmode/{DTB}": dtb_data}
    if busybox:
        found["usbmode/busybox-r36s"] = busybox.read_bytes() if busybox.is_file() else b""
        if md5(found["usbmode/busybox-r36s"]) != manifest["busybox-r36s"]:
            fail(f"{busybox} isn't the static arm64 BusyBox 1.38.0 in usbmode.md5 ({manifest['busybox-r36s']}); see README.md.")
    else:
        card_of = {name: card for name, card in card_of.items() if card not in OPTIONAL}
    data = {card: (HERE / src).read_bytes() if src else found[card] for card, src in FILES.items() if src or card in found}
    for name, card in card_of.items():
        if md5(data[card]) != manifest[name]:
            fail(f"{HERE / FILES[card]} doesn't match usbmode.md5. Regenerate usbmode.md5 after changing card-mode files.")
    # U-Boot hands card mode boot=UUID=<BOOT's FAT serial>; another dArkOS4Clone image can have another serial.
    uuid = f"{serial >> 16:04X}-{serial & 0xFFFF:04X}"
    data["boot.ini.usbmode"], count = re.subn(rb"boot=UUID=[0-9A-F]{4}-[0-9A-F]{4}", f"boot=UUID={uuid}".encode(), data["boot.ini.usbmode"])
    if count != 1:
        fail("boot.ini.usbmode must contain boot=UUID= exactly once.")
    data["usbmode/usbmode.md5"] = "".join(f"{md5(data[card])}  {name}\n" for name, card in card_of.items()).encode()
    data["boot.ini.darkos"] = ini
    original = boot / "boot.ini.darkos-original"
    keep_original = original.is_file() and not darkos_ini_problem(original.read_bytes(), boot)
    if not keep_original:
        data["boot.ini.darkos-original"] = ini

    tool = TOOL.read_bytes()
    if not tool.startswith(b"#!/bin/bash\n") or b"\r" in tool or not tool.isascii():
        fail(f"{TOOL} must be an ASCII bash script with LF line endings.")
    if missing := [str(folder) for folder in tools or [] if not folder.is_dir()]:
        fail(f"Not a folder: {', '.join(missing)}. The Tools folders are EASYROMS\\tools and, once dArkOS uses SD2, the games card's tools.")
    plan = [(boot / card, content) for card, content in data.items()] + [(folder / TOOL.name, tool) for folder in tools or []]

    typer.echo(f"BOOT: {boot} (FAT32, UUID {uuid})")
    typer.echo(f"boot.ini: {'same as ' + ' and '.join(map(str, known)) if known else 'a dArkOS boot.ini (--force)'}")
    typer.echo(f"KERNEL: {kernel_file}")
    typer.echo(f"Board file: {dtb_file}{' (OTG port set to USB device mode)' if md5(dtb_file.read_bytes()) == STOCK_DTB_MD5 else ''}")
    typer.echo(f"BusyBox: {busybox or 'none (no clock sync, no serial console)'}")
    todo, growth = [], 0
    for dst, content in plan:
        old = dst.read_bytes() if dst.is_file() else None
        state = "new" if old is None else "same" if old == content else "replace"
        typer.echo(f"  {state:<8}{len(content):>12,}  {dst}")
        if state != "same":
            todo.append((dst, content))
            if dst.is_relative_to(boot):
                growth += len(content) - len(old or b"")
    if keep_original:
        typer.echo(f"  {'keep':<8}{original.stat().st_size:>12,}  {original}")
    free = shutil.disk_usage(boot).free
    typer.echo(f"BOOT free space: {free / 1e6:.1f} MB now, {(free - growth) / 1e6:.1f} MB after")
    if free - growth < MIN_FREE:
        fail(f"That leaves less than {MIN_FREE / 1e6:.0f} MB free on BOOT, which dArkOS updates need. Nothing was written.")
    if dry_run:
        typer.echo(f"Dry run: nothing written ({len(todo)} of {len(plan)} files would be).")
        return

    for dst, content in todo:
        dst.parent.mkdir(exist_ok=True)
        with dst.open("wb") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
    if bad := [str(dst) for dst, content in plan if dst.read_bytes() != content]:
        fail(f"Read-back failed: {', '.join(bad)}")
    typer.echo(f"Installed: {len(todo)} files written, {len(plan) - len(todo)} already up to date. Use Safely Remove before taking the card out.")


if __name__ == "__main__":
    app()
