import ctypes
import secrets
import struct
import time
from ctypes import wintypes
from datetime import UTC, datetime

import typer

app = typer.Typer()

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
k32.CreateFileW.restype = wintypes.HANDLE
k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
k32.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
k32.SetFilePointerEx.argtypes = [wintypes.HANDLE, ctypes.c_longlong, ctypes.POINTER(ctypes.c_longlong), wintypes.DWORD]
k32.VirtualAlloc.restype = ctypes.c_void_p
k32.VirtualAlloc.argtypes = [ctypes.c_void_p, ctypes.c_size_t, wintypes.DWORD, wintypes.DWORD]
INVALID = wintypes.HANDLE(-1).value
GENERIC_READ, GENERIC_WRITE = 0x80000000, 0x40000000
NO_BUFFERING_WRITE_THROUGH = 0x20000000 | 0x80000000
SECTOR = 512
CLK_IN, CLK_ACK = 48, 49  # unused sectors between the MBR and the Rockchip idbloader at sector 64
TF1_PRODUCT = "TF1 boot card"

buf = ctypes.cast(k32.VirtualAlloc(None, 4096, 0x3000, 0x04), ctypes.POINTER(ctypes.c_char * SECTOR)).contents


def open_disk(number: int, access: int) -> int:
    h = k32.CreateFileW(rf"\\.\PhysicalDrive{number}", access, 3, None, 3, NO_BUFFERING_WRITE_THROUGH if access else 0, None)
    if h == INVALID:
        raise OSError(ctypes.get_last_error(), f"PhysicalDrive{number}")
    return h


def product(h: int) -> str:
    out = ctypes.create_string_buffer(1024)
    got = wintypes.DWORD()
    query = ctypes.create_string_buffer(struct.pack("<II4x", 0, 0))
    if not k32.DeviceIoControl(h, 0x002D1400, query, 12, out, 1024, ctypes.byref(got), None):
        raise OSError(ctypes.get_last_error(), "IOCTL_STORAGE_QUERY_PROPERTY")
    vendor, prod = struct.unpack_from("<II", out.raw, 12)

    def text(offset: int) -> str:
        return out.raw[offset : out.raw.index(b"\0", offset)].decode(errors="replace").strip() if offset else ""

    return f"{text(vendor)} {text(prod)}"


def find_tf1() -> int:
    for number in range(32):
        try:
            h = open_disk(number, 0)
        except OSError:
            continue
        try:
            if TF1_PRODUCT in product(h):
                return number
        finally:
            k32.CloseHandle(h)
    raise typer.BadParameter("No 'R36S TF1 boot card' disk found. Is the R36S on in USB card mode?")


def seek(h: int, lba: int) -> None:
    if not k32.SetFilePointerEx(h, lba * SECTOR, None, 0):
        raise OSError(ctypes.get_last_error(), "seek")


def read_sector(h: int, lba: int) -> bytes:
    seek(h, lba)
    got = wintypes.DWORD()
    if not k32.ReadFile(h, buf, SECTOR, ctypes.byref(got), None) or got.value != SECTOR:
        raise OSError(ctypes.get_last_error(), f"read sector {lba}")
    return bytes(buf)


def write_sector(h: int, lba: int, data: bytes) -> None:
    ctypes.memmove(buf, data.ljust(SECTOR, b"\0"), SECTOR)
    seek(h, lba)
    got = wintypes.DWORD()
    if not k32.WriteFile(h, buf, SECTOR, ctypes.byref(got), None) or got.value != SECTOR:
        raise OSError(ctypes.get_last_error(), f"write sector {lba}")


def utc(seconds: str) -> str:
    return datetime.fromtimestamp(int(seconds), UTC).strftime("%Y-%m-%d %H:%M:%S UTC") if seconds.isdigit() else seconds


# The RK817 RTC counts November with 31 days. Mainline Linux (and so dArkOS) corrects for that, counting one
# extra day per year since 2016 (drivers/rtc/rtc-rk808.c); the EmuELEC kernel that runs USB card mode does not.
# So the card-mode side has to write the RTC in the "Rockchip calendar" that dArkOS expects to read.
def nov2dec_transitions(moment: datetime) -> int:
    return moment.year - 2016 + (1 if moment.month > 11 else 0)


def gregorian_to_rockchip(seconds: int) -> int:
    extra = nov2dec_transitions(datetime.fromtimestamp(seconds, UTC))
    raw = seconds - extra * 86400
    raw_moment = datetime.fromtimestamp(raw, UTC)
    if nov2dec_transitions(raw_moment) < extra:
        if raw_moment.month == 11:
            raise typer.BadParameter("Today maps to the RTC's 'November 31', which can't be set from card mode. Try again after mid-December.")
        raw = seconds - (extra - 1) * 86400
    return raw


def rockchip_to_gregorian(raw: int) -> int:
    return raw + nov2dec_transitions(datetime.fromtimestamp(raw, UTC)) * 86400


@app.command()
def main(
    disk: int = typer.Option(-1, help="PhysicalDrive number of the R36S TF1 boot card; found automatically when omitted"),
    timeout: float = typer.Option(15.0, help="Seconds to wait for the R36S to answer"),
    rockchip_calendar: bool = typer.Option(True, help="Write the RTC the way dArkOS's kernel reads it (see gregorian_to_rockchip)"),
) -> None:
    """Set the R36S clock and RTC from this PC through the v6 USB card mode mailbox."""
    number = disk if disk >= 0 else find_tf1()
    h = open_disk(number, GENERIC_READ | GENERIC_WRITE)
    try:
        if TF1_PRODUCT not in product(h):
            raise typer.BadParameter(f"PhysicalDrive{number} is not the R36S TF1 boot card")
        for lba, magic in ((CLK_IN, b"R36SCLK1 "), (CLK_ACK, b"R36SACK1 ")):
            current = read_sector(h, lba)
            if any(current) and not current.startswith(magic):
                raise typer.BadParameter(f"Sector {lba} holds unexpected data; refusing to write the clock mailbox")

        nonce = secrets.token_hex(4)
        # The R36S polls every 0.5 s and sets whole seconds: send the next second shortly before it starts.
        time.sleep((0.6 - time.time() % 1) % 1)
        seconds = int(time.time()) + 1
        sent = gregorian_to_rockchip(seconds) if rockchip_calendar else seconds
        write_sector(h, CLK_IN, f"R36SCLK1 {sent} {nonce}".ljust(63).encode() + b"\n")

        deadline = time.time() + timeout
        ack = b""
        while time.time() < deadline:
            ack = read_sector(h, CLK_ACK)
            if ack.startswith(f"R36SACK1 {nonce} ".encode()):
                break
            time.sleep(0.3)
        write_sector(h, CLK_IN, b"")
        if not ack.startswith(f"R36SACK1 {nonce} ".encode()):
            typer.echo("No answer from the R36S. It needs USB card mode (r36s-usb.v6.sh or v7) running on the TF1 boot card.", err=True)
            raise typer.Exit(1)
        write_sector(h, CLK_ACK, b"")
    finally:
        k32.CloseHandle(h)

    fields = dict(part.split("=", 1) for part in ack.decode().split() if "=" in part)
    to_real = rockchip_to_gregorian if rockchip_calendar else (lambda raw: raw)
    typer.echo(f"R36S clock set to {utc(str(seconds))} as dArkOS will read it (system clock: {fields.get('date')}, RTC write: {fields.get('rtc')})")
    if rockchip_calendar:
        typer.echo(f"RTC registers hold {utc(str(sent))}: the Rockchip calendar, {(seconds - sent) // 86400} days behind")
    before = fields.get("before", "none")
    if before.isdigit():
        real_before = to_real(int(before))
        typer.echo(f"RTC before, as dArkOS reads it: {utc(str(real_before))} ({real_before - seconds:+d} s from the PC)")
    else:
        typer.echo("The R36S reported no RTC device, so the time is lost when it powers off.")


if __name__ == "__main__":
    app()
