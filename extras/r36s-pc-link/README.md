# R36S PC Link

View and control dArkOS on the R36S from the PC while it runs: screenshots,
button presses, launching and quitting games, EmulationStation settings, logs
and a shell. The link uses the OTG USB cable, or Wi-Fi with a dongle.

- **No reboot and no boot-file changes.** Everything happens at runtime, and a
  normal restart puts every setting back.
- **The R36S never powers the cable.** It switches its OTG 5 V off before it
  asks for the PC, and keeps it off until you turn it back on or restart.
- **Status: experimental.** Tested on an R36S clone ("clone type2 panel1 amp",
  dArkOS4Clone 20261008) on 2026-11-03; see
  [Device test results](#device-test-results). Known bug: a press of the default
  120 ms can register twice in PC Link's own dialog menus.
- **Device:** RK3326 boards laid out like the R36S clone: `pclinkd.sh check`
  reads the board file at runtime and refuses anything else (the OTG port must
  be `otg`, its 5 V `vbus_host` on GPIO3_A4, the PHY's OTG port disabled).
  dArkOS4Clone's "clone type2 panel1 amp" and "clone type1 panel5" board files
  pass.

## Contents

- [How it works](#how-it-works)
- [Why runtime, not a boot mode](#why-runtime-not-a-boot-mode)
- [Setup](#setup)
- [Using it](#using-it)
- [Device test results](#device-test-results)
- [First device test](#first-device-test)
- [Risks and unknowns](#risks-and-unknowns)
- [Tests](#tests)
- [Files](#files)

## How it works

**On the R36S**, **Options > Tools > PC Link** starts `pclinkd.sh` as the
systemd service `pclink`, then returns to the games; the link stays up. Over USB
it:

1. **Switches the OTG 5 V off.** dArkOS's board file keeps it always on through
   `vbus_host`, GPIO3_A4. The service clears that pin, reads it back, and checks
   it four times a second while the link runs. If the pin can't be confirmed
   off, the USB link drops at once; if that lasts 2 seconds, PC Link powers the
   R36S off, which cuts the 5 V for certain.
2. **Makes the OTG port a USB device.** It forces the USB PHY's ID input to
   "device" in its GRF register. These are the same two writes the PHY driver's
   own `otg_mode=peripheral` makes; that switch refuses here because the board
   file disables the PHY's OTG port. dwc2 (`dr_mode = "otg"`) then changes role
   by itself. If dwc2 still reports host mode after 6 seconds, the start fails
   and everything is put back.
3. **Adds a USB network.** A configfs RNDIS gadget with Microsoft OS
   descriptors, so Windows uses its own driver. The R36S is `10.36.36.1`;
   dnsmasq gives the PC `10.36.36.10`–`.20`, with no gateway or DNS.
4. **Starts its own sshd** on port 2222: keys only, user `ark`, its own host key
   (`/var/lib/pclink`). It is separate from dArkOS's ssh service. Its
   `StrictModes` check is off, because dArkOS4Clone's `/` is world-writable and
   the check would refuse every key (see
   [Why runtime](#why-runtime-not-a-boot-mode)).

Stopping the link undoes steps 2–4 and checks the result. If the gadget stays
bound, the ID input won't go back or the OTG 5 V reads on, the link shows
`failed` and asks you to unplug and restart.

**While the link runs**, PC Link writes what it sees to its log, which **Show
the log** shows on the R36S:

- every change of the USB state, with the packet counts and the kernel's USB
  messages;
- an ARP check of the PC every 30 seconds, which Windows answers even with its
  firewall on. Three misses in a row while USB says connected are logged as a
  stall;
- whether `usb0` keeps its address and dnsmasq and sshd keep running. Anything
  that goes missing is logged with what was found, then put back;
- how the service ended. If it was killed, systemd's clean-up step undoes the
  link, as **Stop the link** would.

The OTG 5 V stays off until a restart, or **OTG power on** in the same menu.
That refuses while 5 V comes in from outside. It also refuses until PC Link has
seen the PC's 5 V on the port since the last restart: until then it can't trust
the 5 V detection to tell whether a cable is in.

**Over Wi-Fi**, only step 4 runs, on every address, and the USB port is left
alone. One link runs at a time: stop the Wi-Fi link before starting the USB one.

**Commands from the PC** run `pclink_agent.py` on the R36S, as root through
`sudo`:

- **Screenshots:** read the DRM framebuffer on screen (EmulationStation, games,
  menus), with `/dev/fb0` as the fallback.
- **Buttons:** written into the built-in pad's own input device, "GO-Super
  Gamepad", so they count as player 1's presses.
- **Settings:** stop ES, back up and edit `es_settings.cfg`, then start ES
  again.
- **Games:** launch with ES's own command, emulator and core, and quit with
  SIGTERM so RetroArch saves. ES stops first, so its game lists are saved, and
  comes back when the game ends, even if the launch fails.
- **Logs and status.**

**On the PC**, `r36s-remote.py` (Typer) runs everything through Windows' own
`ssh.exe`:

- **Key:** `~\.ssh\r36s_pclink_ed25519`, made by `setup-key`. Only its
  fingerprint is ever printed.
- **Host key:** pinned in `~\.ssh\r36s_pclink_known_hosts` under the alias
  `r36s-pclink`, so the USB and Wi-Fi addresses share one pin.

## Why runtime, not a boot mode

The alternative was a one-shot boot into a peripheral-mode board file, as USB
card mode does. The evidence showed the role can be switched safely at runtime
instead, which needs no boot-file change at all. Sources: the dArkOS4Clone
20261008 image, read only, plus its kernel source.

| Question                     | Evidence                                                                                                                                                                                                                                                       |
| ---------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Register access from Linux?  | `CONFIG_DEVMEM=y`; `STRICT_DEVMEM` only blocks RAM on arm64. The image has busybox `devmem`.                                                                                                                                                                   |
| Which ID bits?               | The PHY binds as `rockchip,rk3328-usb2phy`: `iddig_en` and `iddig_output` are GRF `0x0100` bits 9 and 10, status at `0x0120`.                                                                                                                                  |
| Does sysfs offer a switch?   | `otg_mode` exists but answers "No support otg" while the board file disables the PHY's OTG port. It's safe, but no use.                                                                                                                                        |
| Will dwc2 follow the ID?     | dwc2 is dual-role and `dwc2.ko` contains `dwc2_conn_id_status_change` ("Connection id status change timed out").                                                                                                                                               |
| How to cut the 5 V?          | `vbus_host` is a fixed regulator on GPIO3_A4, active high, `regulator-always-on`; the kernel can't turn it off, so the pin is set.                                                                                                                             |
| Which gadget?                | No mass storage and no serial gadget. Configfs RNDIS with OS descriptors, matching Windows' `rndiscmp.inf` and `wceisvista.inf`.                                                                                                                               |
| Inject buttons or add a pad? | RetroArch has `input_max_users = "1"`, so a second pad would be ignored. The joypad driver reports buttons only when they change.                                                                                                                              |
| Why its own sshd?            | dArkOS4Clone's `/` and `/home/ark` are mode 777 (arkos4clone's build copies its files in with `--chmod=D0777`), so StrictModes, which checks every folder up to `/`, refuses every key. PC Link's sshd turns it off; its keys are root-owned in `/run/pclink`. |
| Which screen buffer?         | ES uses SDL KMSDRM and RetroArch `video_driver = "gl"` (KMS), so `/dev/fb0` only shows the console; the agent reads the DRM framebuffer.                                                                                                                       |

The one-shot boot mode stays the fallback for a board where dwc2 won't change
role at runtime.

## Setup

Once, on the PC, from this folder:

1. Make the key:

   ```powershell
   uv run r36s-remote.py setup-key
   ```

2. Reach the cards with **USB card mode** or the laptop's SD slot. In Explorer,
   `EASYROMS` is the dArkOS card's games partition; a second games card has its
   own label.
3. Check the plan, then install. `I:` and `E:` are examples: use the letters
   Explorer shows.

   ```powershell
   uv run install-pc-link.py --tools I:\tools --tools E:\tools --dry-run
   uv run install-pc-link.py --tools I:\tools --tools E:\tools
   ```

   This copies `PC Link.sh` and `.pclink\` into both Tools folders, with the
   public key as `.pclink\authorized_keys`. Install into both: while dArkOS uses
   the second card (**Switch to SD2 for Roms**), the Tools menu shows that
   card's `tools`. The installer accepts `tools` folders on `EASYROMS` or
   `R36S-GAMES`; add `--force` for another label.

4. Use **Safely Remove**, then go back to dArkOS.

- **Other subnet:** if `10.36.36.x` clashes with a network the PC uses (a VPN,
  say), add `--subnet 192.168.236`, then use `--host 192.168.236.1` on the PC.
  `--port` changes 2222.
- **Remove:** run the installer with `--remove`. The OTG 5 V comes back with the
  next restart.
- **Update:** stop the link, open **USB card mode**, and run the same two
  installer commands. They replace the files, and the next **Start** uses them.

## Using it

1. **Start the link.** On the R36S, with nothing in the OTG port, open
   **Options > Tools > PC Link > Start over the USB cable** and answer **Yes**.
2. **Connect.** When it says **Connect the OTG port to the PC now**, plug the
   OTG port straight into one of the laptop's USB-C ports: not a hub or a dock.
   Press **A** to go back to the games; the link stays on.
3. **Use it from the PC:**

   ```powershell
   uv run r36s-remote.py wait
   uv run r36s-remote.py status
   uv run r36s-remote.py screenshot --open
   uv run r36s-remote.py press down down a
   uv run r36s-remote.py press fn+start
   uv run r36s-remote.py hold right --ms 1500
   uv run r36s-remote.py launch "/roms2/snes/Super Metroid (Japan, USA) (En,Ja).zip"
   uv run r36s-remote.py quit-game
   uv run r36s-remote.py es get ScreenSaverBehavior
   uv run r36s-remote.py es set ScreenSaverBehavior black
   uv run r36s-remote.py logs game
   uv run r36s-remote.py run "ls /roms2/snes | head"
   uv run r36s-remote.py shell
   ```

4. **Stop.** Use **Options > Tools > PC Link > Stop the link** (or
   `r36s-remote.py stop-link`), then unplug. The OTG port stays unpowered until
   **OTG power on** in the same menu, or the next restart. Only a Wi-Fi dongle,
   USB drive or controller on that port needs it.

**Over Wi-Fi:** connect Wi-Fi in dArkOS, choose **Start over Wi-Fi**, and pass
the address it shows: `r36s-remote.py --host 192.168.1.50 status`. Set
`R36S_HOST` to skip typing it.

**When something goes wrong**, open **Options > Tools > PC Link > Show the log**
on the R36S. It shows the link right now, PC Link's log and the SSH server's log
(`sshd.log`), which says why a key was refused. While SSH works,
`r36s-remote.py logs pclink` shows the same logs on the PC.

**Rules:**

- **Plug in only when asked.** Never plug the PC into the OTG port before PC
  Link asks for it. While dArkOS runs normally, it powers that port.
- **Unplug before restarting.** A normal restart turns the 5 V on again.

### Commands

| Command                                 | What it does                                                                 |
| --------------------------------------- | ---------------------------------------------------------------------------- |
| `setup-key`                             | Makes this PC's key; prints its fingerprint                                  |
| `wait`                                  | Waits for the USB network, then for SSH with the key                         |
| `status`                                | Link, OTG power, ES, the running game, display, battery                      |
| `screenshot [file]`                     | Saves the screen as a PNG (`--open` shows it)                                |
| `press b1 b2+b3 ...`                    | Presses buttons in order; `+` presses them together                          |
| `hold b --ms N`, `release-all`          | Holds a button for a while; releases everything                              |
| `buttons`                               | Names: up down left right a b x y l1 r1 l2 r2 select start l3 r3 fn          |
| `launch <rom> [--dry-run]`              | Starts a game like ES would (`--system`, `--emulator`, `--core` to override) |
| `quit-game [--force]`                   | Quits the game (SIGTERM; `--force` kills it after 10 s); ES comes back       |
| `es list`, `es get`, `es set`           | Reads or changes `es_settings.cfg`; `set` restarts ES and keeps a backup     |
| `es restart`                            | Restarts ES                                                                  |
| `logs es\|game\|pclink\|journal\|dmesg` | The end of a log                                                             |
| `run "<cmd>" [--root]`, `shell`         | A command or shell as `ark` (`--root`: with sudo)                            |
| `stop-link`, `forget-host`              | Stops the link from the PC; forgets the pinned host key after a re-flash     |

- **No game in the way:** settings and launches refuse while a game runs, unless
  you add `--force`, which stops it without saving.
- **Not in ES's history:** games started with `launch` run outside ES, so they
  don't appear in ES's last-played list.

## Device test results

On an R36S clone ("clone type2 panel1 amp", dArkOS4Clone 20261008, kernel
4.4.189), 2026-11-03, over USB to a Windows laptop:

- **Passed:**
  - the link: dwc2 took the device role, Windows bound RNDIS by itself, and the
    PC got its address by DHCP;
  - SSH with PC Link's own sshd;
  - `status`;
  - screenshots from the DRM framebuffer in the menus, in ES and in a game;
  - button presses;
  - `launch` and `quit-game`;
  - `es set`;
  - 7 minutes connected without a stall.
- **Known bug:** a press of the default 120 ms can register twice in PC Link's
  own dialog menus on the R36S.
- **Not yet tested:** stopping the link with the cable in, and **OTG power on**
  afterwards (the 5 V interlock).

## First device test

**Before you start:**

- dArkOS is running and PC Link is installed.
- The OTG port is empty and the laptop's charger is connected.
- Optional: an inline USB-C power meter between the cable and the laptop.

Note what each step shows. Wherever a stop condition says "stop the link", use
the PC Link menu.

1. **Read-only check.** Open **PC Link**: it should show `Link: off` and
   `OTG power: on`.
2. **Start.** Choose **Start over the USB cable**, then **Yes**. Within about 10
   seconds it should show **Waiting for the PC**.
   - **Stop if** it shows **PC Link didn't start**. A message starting
     `Not started:` means nothing changed. Note it, and don't retry.
   - **The OTG port stayed in host mode** means dwc2 won't change role at
     runtime; everything was put back. Report it: this is when the one-shot boot
     mode is needed.
   - **Stop if** the R36S freezes. Hold the power button for 10 seconds and turn
     it on again: a normal start undoes everything.
3. **Optional power check.** With an OTG adapter, plug a USB device that has a
   light (or the Wi-Fi dongle) into the OTG port. It must stay dark. Unplug it.
   - **Stop if** it lights up: the 5 V didn't switch off. Don't connect the PC;
     stop the link and report.
4. **Connect.** Plug the OTG port into a laptop USB-C port. Within about 10
   seconds:
   - Device Manager shows a "Remote NDIS Compatible Device" (or a similar RNDIS
     adapter) under Network adapters.
   - The R36S shows **Connected**.
   - `uv run r36s-remote.py wait` reports `PC Link is up`.

   Stop conditions:
   - **USB stays `not attached` for 30 seconds:** dwc2 switched role, but the PC
     doesn't see it. Note what **Show the log** says, stop the link, unplug and
     report.
   - **Windows shows an unknown device, Code 10 or "USB device not
     recognized":** note what Device Manager says. Try **Update driver > Let me
     pick > Network adapters > Microsoft > Remote NDIS Compatible Device**. If
     that fails, stop the link and report.
   - **Windows warns of a power surge, the laptop's port shuts off, or the R36S
     resets:** unplug at once and report.
   - **The R36S powers itself off:** PC Link couldn't keep its OTG 5 V off.
     Unplug, and report it.
   - **`Permission denied (publickey)`:** open **Show the log**. Under SSH
     server it says why the key was refused, and under Now it shows the PC key
     it takes; compare that with the fingerprint `setup-key` printed.

5. **Status.** Run `r36s-remote.py status`. It should show
   `Link: usb, connected (PC 10.36.36.1x)`, `OTG power: off` and
   `5 V seen on the port: yes` (that 5 V is the PC's).
   - If `5 V seen` says `no` while connected, the PHY doesn't report outside 5
     V. **OTG power on** then stays refused, so restart the R36S with the cable
     out to get OTG power back. Report it.
   - Leave the link idle for 5 minutes, then run `status` again. If it fails,
     note the time and open **Show the log**. It records whether the PC put the
     USB link to sleep, the network lost its address, the PC stopped answering
     or the service ended.
6. **Screen.** Run `screenshot --open` in ES and again in a game. The image
   should match the screen, the right way up and in the right colours.
   - **Report if** it's black, garbled or shows the text console. Include what
     `screenshot --source fb0` and `status` (the Display line) show.
7. **Buttons.** Run `press down` and `press a` in ES, then `press start` in a
   game. Check that the real buttons still work afterwards.
   - **If a button seems stuck:** run `release-all`.
8. **Games.** Compare `launch <rom> --dry-run` with what ES runs (`logs es` or
   the R36S settings notes). Then launch for real, and run `quit-game`. ES
   should come back in 10–30 seconds.
9. **Settings.** Run `es set DrawClock true`: ES restarts with a clock. Then run
   `es set DrawClock false`.
10. **Stop.**
    - With the cable still in, **Stop the link**, then try **OTG power on**: it
      must refuse with "5 V is coming in".
    - Unplug, then **OTG power on**: "OTG power is back on".
    - If it refuses with "hasn't seen a PC's 5 V", the 5 V detection didn't work
      (see step 5): restart the R36S with the cable out instead.
11. **Restart.** Restart the R36S. dArkOS starts as usual, the link is off and
    the OTG port is powered.

**Keep for the report:** the output of `logs pclink` and `logs dmesg` (or, if
SSH doesn't work, a photo of each page of **Show the log**), and a screenshot of
Device Manager.

## Risks and unknowns

**Only a device test can settle:**

- **The role switch.** dwc2 followed the forced ID on the device, and Windows
  bound RNDIS by itself.
- **Link stalls.** In an earlier test the R36S stopped answering within minutes
  while Windows still showed the adapter as up; the last test ran 7 minutes
  without one. PC Link logs the likely causes: the USB state, the network
  address, an ARP check of the PC, and how the service ended.
- **5 V detection.** With the PHY's OTG port unmanaged, does the status bit
  really show a PC's 5 V? **OTG power on** stays refused until PC Link has seen
  it do so, and the start's "5 V was already coming in" warning depends on it.
- **Windows binding.** RNDIS bound by itself on the test PC. Microsoft has
  deprecated RNDIS, but it's still inbox in build 26300.
- **Screen capture.** Screenshots worked in ES, the menus and a game. Only the
  primary plane is captured, so a video overlay could be missing.
- **ES timing.** How long does ES take to stop and start with a large game
  library?

**Hazards:**

- **Plugging in at the wrong time.** Plugging the PC into the OTG port while
  dArkOS runs normally, or restarting with the cable in, back-feeds 5 V; no
  software can sense the cable then. Card mode has the same rule.
- **Read-modify-write on GPIO3.** The kernel also drives the speaker amplifier
  and panel enable on GPIO3. Its writes keep the 5 V bit as they find it, so
  only PC Link's own writes are a risk, and it writes only to switch the 5 V
  off: at the start, or if it finds it on. A write at the same moment as the
  kernel's could:
  - turn the 5 V back on: PC Link checks again just before it asks for the
    cable, then four times a second;
  - undo a speaker or panel change, until that bit next changes.

  Both are very unlikely: the window is milliseconds.

- **Charging current.** With the PC's 5 V on the OTG port, the charger may draw
  up to dArkOS's board-file limit (1,500 mA input) rather than card mode's 500
  mA. That's why the cable goes straight into a laptop USB-C port, never a hub
  or dock. It's still unknown whether that port feeds the charger under dArkOS.
- **OTG power stays off after a session.** A Wi-Fi dongle or controller won't
  work until **OTG power on** or a restart. This is deliberate: it's the safe
  side.
- **Fail-safe power-off.** If the OTG 5 V can't be confirmed off for 2 seconds
  during a USB link, PC Link powers the R36S off, and unsaved game progress is
  lost. If the normal power-off hasn't happened 2 seconds later, it forces one.
  It needs the pin to ignore writes, so it should never happen.
- **Buttons.**
  - Injected presses also trigger dArkOS's hotkey daemon, exactly like real
    ones.
  - The analog sticks can't be injected: the driver re-reads them every 10 ms.
  - If the SSH connection drops mid-press, the agent releases the buttons; if
    something still sticks, run `release-all`.
- **Network clash.** `10.36.36.0/24` could clash with a VPN route; change it
  with `--subnet`.
- **Security.**
  - PC Link's sshd takes keys only and has its own host key.
  - Its StrictModes check is off. Its key file and folders are root's, in
    `/run/pclink`, and the only other user, `ark`, has passwordless sudo anyway,
    so the check would add nothing here.
  - dArkOS's own Remote Services still use `ark` / `ark`, and every dArkOS4Clone
    image ships the same stock SSH host keys.
  - The agent runs as root, as `ark` can anyway through passwordless sudo.

## Tests

Everything below runs off the device, against the dArkOS4Clone image (read only,
through an overlay). Set `R36S_IMAGE` to the image file (for example
`D:\dArkOS4Clone-20261008.img`). Without it, the service suite runs in a Debian
13 stand-in (the same OpenSSH, dnsmasq and sudo, and the same `ark` user), and
the suites that need dArkOS's files are skipped. Run it all with:

```powershell
uv run --no-project --with pytest --with typer pytest tests -v
```

The board-file fixtures are copies of this repository's
`boot/dArkOS/consoles/clone type2 panel1 amp` and `clone type1 panel5` board
files. The negative case, EmuELEC's card-mode board file, isn't in the
repository: copy the one USB card mode's installer makes to
`tests/device/fixtures/emuelec-card-mode.dtb` to run it; without it those checks
are skipped.

| Suite                                                  | Where                                                                                                     | Covers                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                |
| ------------------------------------------------------ | --------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `tests/device/test_pclinkd.sh` (177 checks)            | arm64 chroot of dArkOS's rootfs, or the Debian 13 stand-in; fake registers; real `ip`, dnsmasq, sshd, ssh | Real board files (Type2 and Type1 pass, EmuELEC's is refused), 5 V off before the role switch, gadget and OS descriptors, key-only SSH on a world-writable `/` as on the R36S (a StrictModes sshd refuses the key there; PC Link's takes it), the logs (key fingerprints, USB state with kernel messages and traffic), watchdogs (OTG power, network address, dnsmasq, sshd, the stall check), verified stop and restore, OTG power on safety, failure paths (dwc2 stuck in host mode, OTG power that won't stay off, a killed service and its clean-up), one link at a time, the lock, `pclink.conf` |
| `tests/device/test_agent.py` (27 tests)                | the same chroot                                                                                           | ES settings on the real `es_settings.cfg`, launch commands against the real `es_systems.cfg` (ES's escaping, defaults and file precedence), ES stopped for a launch and back after it or after a failure, game wrapper as `ark`, quit and `--force`, fb0 PNG, button map against the board file                                                                                                                                                                                                                                                                                                       |
| `run-native-tests.sh` (amd64 and arm64)                | Debian with the kernel's uapi headers                                                                     | 29 of 29 DRM and evdev ABI values; button injection into a real uinput "GO-Super Gamepad" (press, chord, release on interrupt)                                                                                                                                                                                                                                                                                                                                                                                                                                                                        |
| `tests/test_device_container.py`                       | Docker from Windows                                                                                       | Runs the suites above (the service suite in the dArkOS chroot and in the Debian 13 stand-in) and an end-to-end session: Windows `ssh.exe` and `r36s-remote.py` → dArkOS's sshd (PC Link's config) → the agent                                                                                                                                                                                                                                                                                                                                                                                         |
| `tests/test_r36s_remote.py`, `test_install_pc_link.py` | Windows                                                                                                   | CLI behaviour with a fake SSH layer, `wait`, `stop-link`, SSH errors explained (a refused key: the device's log and the key's fingerprint), keys never printed, installer dry run, read-back and remove                                                                                                                                                                                                                                                                                                                                                                                               |

The emulator can't exercise USB or the DRM screen buffer; see
[Device test results](#device-test-results) and
[First device test](#first-device-test).

## Files

| File                             | Goes to            | Role                                                |
| -------------------------------- | ------------------ | --------------------------------------------------- |
| `device/PC Link.sh`              | `tools\PC Link.sh` | Tools menu: start, stop, OTG power on, log          |
| `device/.pclink/pclinkd.sh`      | `tools\.pclink\`   | The link service, plus `status`, `check`, `vbus-on` |
| `device/.pclink/pclink_agent.py` | `tools\.pclink\`   | What the PC's commands run on the R36S              |
| `r36s-remote.py`                 | stays on the PC    | The PC tool                                         |
| `install-pc-link.py`             | stays on the PC    | Installs, updates or removes PC Link (`--dry-run`)  |
| `tests/`                         | stays on the PC    | The tests above                                     |

On the R36S at runtime, PC Link writes:

- **`/run/pclink`:** its scripts, keys, sshd config, status and logs, gone at
  the next restart;
- **`/var/lib/pclink`:** its SSH host key, kept.

Nothing is written to `BOOT`.
