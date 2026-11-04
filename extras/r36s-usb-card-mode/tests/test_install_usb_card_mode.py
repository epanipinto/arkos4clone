"""Tests for install-usb-card-mode.py. The files this repository can't ship come from the R36S's original EmuELEC card:
set R36S_KERNEL to EmuELEC 4.7's KERNEL and R36S_DTB to the original rk3326-evb-lp3-v12-linux.dtb, and optionally
R36S_BUSYBOX to busybox-r36s (see README.md). Without them, the tests that need them are skipped.
"""

import hashlib
import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

spec = importlib.util.spec_from_file_location("install_usb_card_mode", Path(__file__).resolve().parents[1] / "install-usb-card-mode.py")
inst = importlib.util.module_from_spec(spec)
spec.loader.exec_module(inst)
SAVED = inst.SAVED_INI.read_bytes()
NEWER = SAVED.replace(b"max_cpufreq=1296", b"max_cpufreq=1416")
KERNEL = Path(os.environ.get("R36S_KERNEL", "missing"))
DTB = Path(os.environ.get("R36S_DTB", "missing"))
BUSYBOX = Path(os.environ.get("R36S_BUSYBOX", "missing"))
MANIFEST = {name: digest for digest, name in (line.split("  ", 1) for line in (inst.HERE / "usbmode.md5").read_text().splitlines())}
needs_card = pytest.mark.skipif(not (KERNEL.is_file() and DTB.is_file()), reason="needs R36S_KERNEL and R36S_DTB from the EmuELEC card")
needs_busybox = pytest.mark.skipif(not BUSYBOX.is_file(), reason="needs R36S_BUSYBOX")


def md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


@pytest.fixture
def card(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A dArkOS4Clone BOOT partition after the DTB selector picked clone type2 panel1 amp, an empty Tools folder, and
    the EmuELEC card's EMUELEC partition."""
    boot = tmp_path / "BOOT"
    (boot / inst.SHIPPED_INI.parent).mkdir(parents=True)
    for name in ("boot.ini", inst.SHIPPED_INI):
        (boot / name).write_bytes(SAVED)
    for name in ("Image", "uInitrd", "rk3326-r36s-type2-panel1-with-amp-linux.dtb"):
        (boot / name).write_bytes(b"x")
    (tmp_path / "tools").mkdir()
    emuelec = tmp_path / "EMUELEC"
    emuelec.mkdir()
    if KERNEL.is_file() and DTB.is_file():
        (emuelec / "KERNEL").write_bytes(KERNEL.read_bytes())
        (emuelec / inst.DTB).write_bytes(DTB.read_bytes())
    monkeypatch.setattr(inst, "volume_info", lambda root: ("BOOT", "FAT32", 0xF162CB2C))
    monkeypatch.setattr(inst, "emuelec_drives", lambda: [emuelec])
    return boot


def install(boot: Path, *extra: str) -> tuple[int, str]:
    result = CliRunner().invoke(inst.app, ["--boot", str(boot), "--tools", str(boot.parent / "tools"), *extra])
    return result.exit_code, result.output


def snapshot(root: Path) -> dict[Path, tuple[int, int]]:
    return {p: (p.stat().st_size, p.stat().st_mtime_ns) for top in (root / "BOOT", root / "tools") for p in top.rglob("*") if p.is_file()}


def md5sum_c(boot: Path) -> list[str]:
    """What "USB Card Mode.sh" checks on the R36S before it switches to card mode; returns the names it checked."""
    names = []
    for line in (boot / "usbmode" / "usbmode.md5").read_text().splitlines():
        digest, name = line.split("  ", 1)
        assert md5((boot / "usbmode" / name).read_bytes()) == digest, name
        names.append(name)
    return names


@needs_card
def test_original_board_file_becomes_the_tested_card_mode_one() -> None:
    stock = DTB.read_bytes()
    assert md5(stock) == inst.STOCK_DTB_MD5
    patched = inst.set_string_prop(stock, "/usb@ff300000", "dr_mode", "peripheral")
    assert md5(patched) == MANIFEST[inst.DTB]
    assert inst.set_string_prop(patched, "/usb@ff300000", "dr_mode", "peripheral") == patched
    assert inst.set_string_prop(patched, "/usb@ff300000", "dr_mode", "host") == stock
    with pytest.raises(ValueError, match="no dr_mode property in /usb@ff300001"):
        inst.set_string_prop(stock, "/usb@ff300001", "dr_mode", "peripheral")


def test_set_string_prop_refuses_what_it_cant_change() -> None:
    with pytest.raises(ValueError, match="not a device tree"):
        inst.set_string_prop(b"\0" * 64, "/usb@ff300000", "dr_mode", "peripheral")


@needs_card
def test_dry_run_writes_nothing(card: Path) -> None:
    before = snapshot(card.parent)
    code, out = install(card, "--dry-run")
    assert code == 0, out
    assert "Dry run: nothing written (12 of 12 files would be)" in out
    assert "BusyBox: none (no clock sync, no serial console)" in out
    assert snapshot(card.parent) == before


@needs_card
def test_fresh_card(card: Path) -> None:
    code, out = install(card)
    assert code == 0, out
    assert f"KERNEL: {card.parent / 'EMUELEC' / 'KERNEL'}" in out
    assert "(OTG port set to USB device mode)" in out
    assert (card / "usbmode" / "KERNEL").read_bytes() == KERNEL.read_bytes()
    assert md5((card / "usbmode" / inst.DTB).read_bytes()) == MANIFEST[inst.DTB]
    for dst, src in inst.FILES.items():
        if src:
            assert (card / dst).read_bytes() == (inst.HERE / src).read_bytes(), dst
    assert not (card / "usbmode" / "busybox-r36s").exists()
    assert sorted(md5sum_c(card)) == sorted(name for name in MANIFEST if name != "busybox-r36s")
    for name in ("boot.ini", "boot.ini.darkos", "boot.ini.darkos-original"):
        assert (card / name).read_bytes() == SAVED, name
    assert (card.parent / "tools" / inst.TOOL.name).read_bytes() == inst.TOOL.read_bytes()


@needs_card
@needs_busybox
def test_with_busybox_the_card_matches_the_tested_files(card: Path) -> None:
    code, out = install(card, "--busybox", str(BUSYBOX))
    assert code == 0, out
    assert (card / "usbmode" / "busybox-r36s").read_bytes() == BUSYBOX.read_bytes()
    assert (card / "usbmode" / "usbmode.md5").read_bytes() == (inst.HERE / "usbmode.md5").read_bytes()
    md5sum_c(card)


@needs_card
def test_wrong_busybox(card: Path, tmp_path: Path) -> None:
    (tmp_path / "bb").write_bytes(b"some other busybox")
    code, out = install(card, "--busybox", str(tmp_path / "bb"))
    assert code == 1 and "isn't the static arm64 BusyBox 1.38.0" in out, out
    assert not (card / "usbmode").exists()


@needs_card
def test_card_mode_board_file_and_orig_name_are_accepted(card: Path) -> None:
    emuelec = card.parent / "EMUELEC"
    (emuelec / f"{inst.DTB}.orig").write_bytes((emuelec / inst.DTB).read_bytes())
    (emuelec / inst.DTB).write_bytes(inst.set_string_prop(DTB.read_bytes(), "/usb@ff300000", "dr_mode", "peripheral"))
    code, out = install(card)
    assert code == 0, out
    assert "(OTG port set to USB device mode)" not in out
    (emuelec / inst.DTB).unlink()
    code, out = install(card)
    assert code == 0 and f"Board file: {emuelec / (inst.DTB + '.orig')}" in out, out
    assert md5((card / "usbmode" / inst.DTB).read_bytes()) == MANIFEST[inst.DTB]


@needs_card
def test_wrong_board_file(card: Path, tmp_path: Path) -> None:
    (tmp_path / "other.dtb").write_bytes(b"another board file")
    code, out = install(card, "--dtb", str(tmp_path / "other.dtb"))
    assert code == 1 and f"No original {inst.DTB}" in out, out
    assert not (card / "usbmode").exists()


@needs_card
def test_no_emuelec_card(card: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(inst, "emuelec_drives", list)
    code, out = install(card)
    assert code == 1 and "no EMUELEC drive" in out and "--kernel" in out, out
    code, out = install(card, "--kernel", str(KERNEL))
    assert code == 1 and "--dtb" in out, out
    code, out = install(card, "--kernel", str(KERNEL), "--dtb", str(DTB))
    assert code == 0, out


@needs_card
def test_reinstall_rewrites_nothing(card: Path) -> None:
    assert install(card)[0] == 0
    before = snapshot(card.parent)
    code, out = install(card)
    assert code == 0, out
    assert "Installed: 0 files written, 11 already up to date" in out
    assert "keep" in out
    assert snapshot(card.parent) == before


@needs_card
def test_other_card_uuid(card: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(inst, "volume_info", lambda root: ("BOOT", "FAT32", 0x1234ABCD))
    code, out = install(card)
    assert code == 0, out
    source = (inst.HERE / "boot.ini.usbmode").read_bytes()
    assert (card / "boot.ini.usbmode").read_bytes() == source.replace(b"boot=UUID=F162-CB2C", b"boot=UUID=1234-ABCD")
    md5sum_c(card)


@needs_card
def test_newer_dtb_selector_boot_ini(card: Path) -> None:
    for name in ("boot.ini", inst.SHIPPED_INI):
        (card / name).write_bytes(NEWER)
    code, out = install(card)
    assert code == 0, out
    assert (card / "boot.ini.darkos").read_bytes() == NEWER
    assert (card / "boot.ini.darkos-original").read_bytes() == NEWER


@needs_card
def test_changed_boot_ini_needs_force(card: Path) -> None:
    (card / "boot.ini").write_bytes(NEWER)
    code, out = install(card)
    assert code == 1 and "--force" in out, out
    assert not (card / "usbmode").exists()
    code, out = install(card, "--force")
    assert code == 0 and "a dArkOS boot.ini (--force)" in out, out


@needs_card
def test_keeps_a_valid_original(card: Path) -> None:
    (card / "boot.ini.darkos-original").write_bytes(NEWER)
    assert install(card)[0] == 0
    assert (card / "boot.ini.darkos-original").read_bytes() == NEWER


REFUSED = {
    "no boot.ini": (lambda boot: (boot / "boot.ini").unlink(), "DTB selector"),
    "card-mode boot.ini": (lambda boot: (boot / "boot.ini").write_bytes((inst.HERE / "boot.ini.usbmode").read_bytes()), "is the card-mode one"),
    "not a dArkOS boot.ini": (lambda boot: (boot / "boot.ini").write_bytes(b"hello\n"), "isn't a dArkOS boot.ini"),
    "fallback file missing": (lambda boot: (boot / "uInitrd").unlink(), "aren't on the card: uInitrd"),
    "missing Tools folder": (lambda boot: (boot.parent / "tools").rmdir(), "Not a folder"),
}


@needs_card
@pytest.mark.parametrize("case", REFUSED)
def test_refused_even_with_force(card: Path, case: str) -> None:
    change, message = REFUSED[case]
    change(card)
    before = snapshot(card.parent)
    code, out = install(card, "--force")
    assert code == 1 and message in out, out
    assert snapshot(card.parent) == before


def test_not_the_boot_partition(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(inst, "volume_info", lambda root: ("EMUELEC", "FAT", 0x12103527))
    code, out = install(tmp_path / "BOOT")
    assert code == 1 and "not the dArkOS card's BOOT partition" in out, out


@needs_card
def test_wrong_kernel(card: Path, tmp_path: Path) -> None:
    (tmp_path / "KERNEL").write_bytes(b"not EmuELEC's kernel")
    code, out = install(card, "--kernel", str(tmp_path / "KERNEL"))
    assert code == 1 and "No KERNEL matching usbmode.md5" in out, out
    assert not (card / "usbmode").exists()


@needs_card
def test_not_enough_space(card: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(inst.shutil, "disk_usage", lambda path: SimpleNamespace(free=80_000_000))
    code, out = install(card)
    assert code == 1 and "Nothing was written" in out, out
    assert not (card / "usbmode").exists()


def test_saved_ini_is_the_repository_one() -> None:
    assert inst.SAVED_INI.is_file()
    assert b"rk3326-r36s-type2-panel1-with-amp-linux.dtb" in SAVED
