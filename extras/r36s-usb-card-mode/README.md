# R36S USB card mode

**Options > Tools > USB Card Mode** restarts the R36S once into a small
card-reader system. The PC then sees both microSD cards as USB drives, so
nothing has to be swapped or taken out. **Safely Remove** on the PC and unplug
the cable: the R36S powers off, and the next power-on starts dArkOS again.

- **Device:** R36S clone with the "clone type2 panel1 amp" board file, running
  dArkOS4Clone. The installer refuses other board files unless you add
  `--force`.
- **Status:** used on that device with dArkOS4Clone 20261008. Tested there:
  sharing both cards, the status screens, the USB serial console and restarting
  into card mode from the console.
- **Not part of the image.** Card mode boots the kernel and board file from the
  R36S's original EmuELEC card, which this repository can't ship. The installer
  takes them from your own card.

## How it works

1. **USB Card Mode.sh** (Tools menu) checks the card-mode files against
   `usbmode/usbmode.md5`, saves dArkOS's `boot.ini` as `boot.ini.darkos`, and
   writes a `boot.ini` for one boot: the card-mode part of `boot.ini.usbmode`,
   then dArkOS's own boot as U-Boot's fallback. Then it restarts.
2. **U-Boot** loads EmuELEC 4.7's `KERNEL` (its initramfs is built in) and the
   card-mode board file from `usbmode/`. It checks both by MD5 in memory first;
   if either is missing or different, the dArkOS boot below runs instead.
3. **EmuELEC's initramfs** mounts BOOT read-only on `/flash` and sources
   `/flash/post-flash.sh`. That copies `boot.ini.darkos` back over `boot.ini`
   first, so the next power-on starts dArkOS whatever happens next, then runs
   the helper.
4. **The helper** (`usbmode/r36s-usb.v7.sh`) copies itself to RAM, shows the
   pre-rendered screens on the panel, and shares both cards over USB (configfs
   mass storage, read-write). With BusyBox (below) it also offers a USB serial
   console and sets the clock and the RTC when the PC asks. It waits up to 90
   seconds for the PC and powers off when the cable is unplugged.

The card-mode board file is the original one with its OTG port's `dr_mode` set
to `peripheral`, so the port can be a USB device. The installer makes it from
the original file and checks the result against `usbmode.md5`.

If the R36S ever stays on a black screen, `USB-CARD-MODE-RECOVERY.txt` on BOOT
explains how to put dArkOS's `boot.ini` back from a PC.

## What you need

- A Windows PC with Python 3.11 or later and [uv][uv] (or `pip install typer`).
- From the R36S's original EmuELEC card (its `EMUELEC` partition), or a copy of
  it:

  | File                           | Size             | MD5                                |
  | ------------------------------ | ---------------- | ---------------------------------- |
  | `KERNEL` (EmuELEC 4.7)         | 20,662,280 bytes | `a0e4b524c41147f7abef3f274d7d9981` |
  | `rk3326-evb-lp3-v12-linux.dtb` | 106,929 bytes    | `78614fa399cdd178a8d83eaa13f04c68` |

  Other EmuELEC builds or board files don't match, and the installer stops.

- Optional, for the serial console and the clock: `busybox-r36s`, the static
  arm64 BusyBox 1.38.0 from Docker's official image (MD5
  `977d128677b916a106e796473a750c26`):

  ```powershell
  docker create --platform linux/arm64 --name bb busybox:1.38.0-musl
  docker cp bb:/bin/busybox busybox-r36s
  docker rm bb
  ```

## Install

On a freshly flashed card, pick **clone type2 panel1 amp** with the DTB selector
and boot dArkOS once first. Then put the dArkOS card (and the EmuELEC card, if
you use it) in the PC and check the drive letters in Explorer: here `D:` is
BOOT, `E:` is EASYROMS.

```powershell
uv run --no-project --with typer install-usb-card-mode.py --boot D: --tools E:\tools --dry-run
uv run --no-project --with typer install-usb-card-mode.py --boot D: --tools E:\tools --busybox .\busybox-r36s
```

- The installer finds `KERNEL` and the board file on a drive labelled `EMUELEC`;
  `--kernel` and `--dtb` point at copies instead.
- If dArkOS reads its ROMs from the second card (**Switch to SD2 for Roms**),
  add `--tools` for that card's `tools` folder too.
- Running it again only rewrites files that changed, for example after
  re-flashing dArkOS4Clone.
- To remove card mode, follow `USB-CARD-MODE-RECOVERY.txt`, "To remove USB card
  mode".

## Use

1. On the R36S, open **Options > Tools > USB Card Mode**. It restarts.
2. When the screen asks, plug the OTG port straight into the PC, not into a hub.
   It waits 90 seconds, then powers off.
3. Use the drives. When you're done, **Safely Remove** both R36S drives and
   unplug the cable. The R36S powers off; turn it on to play.

**Plug in only when asked.** While dArkOS runs, it powers the OTG port.

### PC tools

Both need the BusyBox above on BOOT. Run them with
`uv run --no-project --with typer --with pillow --with pyserial <tool> --help`.

- `r36s-console.py`: the USB serial console. Screenshots of the R36S's screen,
  the card-mode and kernel logs, commands as root, and restarting into card mode
  (`rearm`) or back to dArkOS (`exit`). An empty `usbmode\no-serial` file on
  BOOT turns the serial port off.
- `r36s-clock.py`: sets the R36S's clock and RTC from the PC while card mode
  runs.

## Files

| File                         | Goes to                               | Role                                                  |
| ---------------------------- | ------------------------------------- | ----------------------------------------------------- |
| `USB Card Mode.sh`           | `tools` on EASYROMS (Options > Tools) | Arms card mode for one boot and restarts              |
| `boot.ini.usbmode`           | BOOT                                  | Card-mode boot, then dArkOS's as the fallback         |
| `post-flash.boot.sh`         | BOOT, as `post-flash.sh`              | Puts dArkOS's `boot.ini` back, runs the helper        |
| `r36s-usb.v7.sh`             | BOOT `usbmode\`                       | The card reader, screens, serial console, clock       |
| `r36s-link.tar`              | BOOT `usbmode\`                       | The configfs link (the initramfs BusyBox has no `ln`) |
| `screens.tar`                | BOOT `usbmode\`                       | The six status screens, 640x480 XRGB8888, gzipped     |
| `usbmode.md5`                | BOOT `usbmode\` (as installed)        | What the Tools script and U-Boot check                |
| `USB-CARD-MODE-RECOVERY.txt` | BOOT                                  | What to do if dArkOS doesn't start                    |
| `install-usb-card-mode.py`   | PC                                    | Installs or updates everything above                  |
| `r36s-console.py`            | PC                                    | Serial console client                                 |
| `r36s-clock.py`              | PC                                    | Sets the R36S's clock                                 |
| `render-screens.py`          | PC                                    | Draws `screens.tar` (needs the Segoe UI fonts)        |

## Third-party files and licences

- `KERNEL` (EmuELEC 4.7, Linux 5.10) and the board file come from your own
  EmuELEC card; nothing from them is in this repository.
- BusyBox is GPL-2.0. It isn't committed: you take it from Docker's official
  image, whose sources are [busybox.net][busybox] 1.38.0 and
  [docker-library/busybox][docker-busybox].
- `screens.tar` holds images drawn with Pillow and Windows' Segoe UI fonts; the
  fonts aren't included.

## Tests

The Python tests run on Windows. The installer tests need the EmuELEC files;
without them they're skipped:

```powershell
$env:R36S_KERNEL = 'I:\KERNEL'
$env:R36S_DTB = 'I:\rk3326-evb-lp3-v12-linux.dtb'
$env:R36S_BUSYBOX = '.\busybox-r36s'
uv run --no-project --with pytest --with typer --with pillow --with pyserial pytest tests
```

The shell tests run in Docker. Each one's header gives its command and the
fixtures it needs in `/k`: the EmuELEC files, the card-mode board file the
installer makes, `busybox-r36s`, dArkOS's `boot.ini` for clone type2 panel1 amp,
the initramfs extracted from `KERNEL`, and the first 16 MiB of a BOOT and an
EMUELEC partition.

| Test                         | Runs in                    | Covers                                                                                      |
| ---------------------------- | -------------------------- | ------------------------------------------------------------------------------------------- |
| `test-usb-card-mode-tool.sh` | `debian:trixie-slim`       | The Tools script: arming, retries, damaged or missing files, a changed board file           |
| `test-v7.sh`                 | arm64 `busybox`, initramfs | The helper and `post-flash.sh` in EmuELEC's initramfs BusyBox, U-Boot's checks, the console |
| `test-screens.sh`            | arm64 `busybox`, initramfs | Which screen shows when, and only on a 640x480 XRGB8888 framebuffer                         |
| `test-console-detect.sh`     | arm64 `busybox`            | dArkOS's `console_detect` still reads the right board file from a card-mode `boot.ini`      |

[uv]: https://docs.astral.sh/uv/
[busybox]: https://busybox.net/
[docker-busybox]: https://github.com/docker-library/busybox
