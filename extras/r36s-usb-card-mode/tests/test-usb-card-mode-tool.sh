#!/bin/bash
# Offline tests for "USB Card Mode.sh" (dArkOS Tools menu). Runs in Docker's debian:trixie-slim (bash, coreutils,
# diffutils, util-linux like dArkOS), with /boot as a tmpfs so it is a mountpoint:
#   docker run --rm --tmpfs /boot -v "<extras/r36s-usb-card-mode>:/w:ro" -v "<fixtures>:/k:ro" \
#     debian:trixie-slim bash /w/tests/test-usb-card-mode-tool.sh
# /k = KERNEL, rk3326-evb-lp3-v12-linux.dtb (the card-mode board file install-usb-card-mode.py makes), busybox-r36s and
# boot.ini.darkos (dArkOS's boot.ini for clone type2 panel1 amp). sudo, systemctl, reboot and sleep are mocked.
PASS=0
FAIL=0
ok() { PASS=$((PASS + 1)); echo "  ok: $*"; }
bad() { FAIL=$((FAIL + 1)); echo "  FAIL: $*"; }
check() { if [ "$1" = "$2" ]; then ok "$3"; else bad "$3: got [$1] want [$2]"; fi; }
has() { if grep -qF -- "$2" "$1"; then ok "$3"; else bad "$3: [$2] not in $1"; sed 's/^/    | /' "$1"; fi; }
hasnot() { if grep -qF -- "$2" "$1"; then bad "$3: [$2] found in $1"; else ok "$3"; fi; }
md5() { if [ -f "$1" ]; then md5sum < "$1" | cut -d' ' -f1; else echo "MISSING:$1"; fi; }
TOOL="/w/USB Card Mode.sh"

mkdir -p /mock
printf '#!/bin/sh\nexec "$@"\n' > /mock/sudo
printf '#!/bin/sh\necho "systemctl $*" >> /tmp/calls\n' > /mock/systemctl
printf '#!/bin/sh\necho "reboot $*" >> /tmp/calls\n' > /mock/reboot
printf '#!/bin/sh\nexit 0\n' > /mock/sleep
chmod 755 /mock/*
export PATH=/mock:$PATH

fresh_boot() {
  find /boot -mindepth 1 -delete
  mkdir -p /boot/usbmode
  cp /k/boot.ini.darkos /boot/boot.ini
  cp /k/boot.ini.darkos /boot/boot.ini.darkos-original
  cp /w/boot.ini.usbmode /boot/boot.ini.usbmode
  cp /w/post-flash.boot.sh /boot/post-flash.sh
  cp /k/KERNEL /k/rk3326-evb-lp3-v12-linux.dtb /w/r36s-usb.v7.sh /k/busybox-r36s /w/r36s-link.tar /w/screens.tar /w/usbmode.md5 /boot/usbmode/
}
run_tool() {
  : > /tmp/calls
  : > /dev/tty1
  bash "$TOOL" > /tmp/stdout 2>&1
  echo "exit=$?" >> /tmp/calls
}
DARKOS=$(md5 /k/boot.ini.darkos)
USBMODE=$(md5 /w/boot.ini.usbmode)

echo "== T0: syntax and file format"
if bash -n "$TOOL"; then ok "bash -n"; else bad "bash -n"; fi
check "$(head -c 3 "$TOOL" | od -An -tx1 | tr -d ' ')" "23212f" "starts with #!/ (no BOM)"
check "$(tr -cd '\r' < "$TOOL" | wc -c)" "0" "LF line endings"
check "$(LC_ALL=C grep -c '[^ -~]' "$TOOL")" "0" "ASCII only"

echo "== T1: dArkOS boot.ini: saved, card-mode boot.ini written, restart"
fresh_boot
run_tool
check "$(md5 /boot/boot.ini.darkos)" "$DARKOS" "boot.ini.darkos is dArkOS's boot.ini"
check "$(md5 /boot/boot.ini)" "$USBMODE" "boot.ini is exactly boot.ini.usbmode (its fallback = this dArkOS boot.ini)"
check "$(tr -cd '\r' < /boot/boot.ini | wc -c)" "0" "no CR in boot.ini"
check "$(find /boot -maxdepth 1 -name 'boot.ini.new' | wc -l)" "0" "no boot.ini.new left behind"
has /dev/tty1 "Restarting into USB card mode - connect the OTG port to the PC" "restart message"
check "$(tr '\n' ';' < /tmp/calls)" "systemctl reboot;exit=0;" "reboots"

echo "== T2: boot.ini already card-mode (U-Boot fell back last time): boot.ini.darkos kept"
fresh_boot
cp /w/boot.ini.usbmode /boot/boot.ini
cp /k/boot.ini.darkos /boot/boot.ini.darkos
sed -i 's/max_cpufreq=1296/max_cpufreq=1200/' /boot/boot.ini.darkos
CHANGED=$(md5 /boot/boot.ini.darkos)
run_tool
check "$(md5 /boot/boot.ini.darkos)" "$CHANGED" "boot.ini.darkos not overwritten by the card-mode file"
has /dev/tty1 "USB card mode didn't start last time" "says so"
has /boot/boot.ini "max_cpufreq=1200" "fallback taken from boot.ini.darkos"
has /tmp/calls "systemctl reboot" "reboots"

echo "== T3: damaged KERNEL: nothing changed"
fresh_boot
printf 'X' | dd of=/boot/usbmode/KERNEL bs=1 seek=12345678 conv=notrunc 2> /dev/null
run_tool
check "$(md5 /boot/boot.ini)" "$DARKOS" "boot.ini untouched"
check "$(find /boot -maxdepth 1 -name 'boot.ini.darkos' | wc -l)" "0" "boot.ini.darkos not even written"
has /dev/tty1 "damaged or missing (usbmode.md5)" "explains"
hasnot /tmp/calls "reboot" "no restart"
has /tmp/calls "exit=1" "exit 1"

echo "== T4: card mode not installed (no usbmode.md5)"
fresh_boot
rm /boot/usbmode/usbmode.md5
run_tool
check "$(md5 /boot/boot.ini)" "$DARKOS" "boot.ini untouched"
has /dev/tty1 "USB card mode is not installed" "explains"
hasnot /tmp/calls "reboot" "no restart"

echo "== T5: post-flash.sh missing from the manifest's view"
fresh_boot
rm /boot/post-flash.sh
run_tool
check "$(md5 /boot/boot.ini)" "$DARKOS" "boot.ini untouched"
hasnot /tmp/calls "reboot" "no restart"

echo "== T6: boot.ini is neither dArkOS's nor card mode's"
fresh_boot
printf 'something else\n' > /boot/boot.ini
run_tool
check "$(cat /boot/boot.ini)" "something else" "boot.ini untouched"
has /dev/tty1 "isn't dArkOS's boot.ini" "explains"
hasnot /tmp/calls "reboot" "no restart"

echo "== T7: card-mode boot.ini and no boot.ini.darkos: boot.ini.darkos-original is the fallback"
fresh_boot
cp /w/boot.ini.usbmode /boot/boot.ini
run_tool
check "$(md5 /boot/boot.ini)" "$USBMODE" "card-mode boot.ini built from boot.ini.darkos-original"
has /tmp/calls "systemctl reboot" "reboots"

echo "== T8: card-mode boot.ini and no saved dArkOS boot.ini at all"
fresh_boot
cp /w/boot.ini.usbmode /boot/boot.ini
rm /boot/boot.ini.darkos-original
run_tool
check "$(md5 /boot/boot.ini)" "$USBMODE" "boot.ini untouched"
has /dev/tty1 "No saved dArkOS boot.ini" "explains"
hasnot /tmp/calls "reboot" "no restart"

echo "== T9: dArkOS later switched board files: the fallback follows boot.ini, not the template"
fresh_boot
sed -i 's/rk3326-r36s-type2-panel1-with-amp-linux.dtb/rk3326-r36s-type2-panel1-linux.dtb/' /boot/boot.ini
NEWDARK=$(md5 /boot/boot.ini)
run_tool
check "$(md5 /boot/boot.ini.darkos)" "$NEWDARK" "boot.ini.darkos is the current dArkOS boot.ini"
has /boot/boot.ini 'load mmc 1:1 ${dtb_loadaddr} rk3326-r36s-type2-panel1-linux.dtb' "fallback loads the current board file"
hasnot /boot/boot.ini "panel1-with-amp" "and not the old one"
check "$(sed -n '1,/^# R36S-USB-CARD-MODE-FALLBACK/p' /boot/boot.ini | md5sum | cut -d' ' -f1)" "$(sed -n '1,/^# R36S-USB-CARD-MODE-FALLBACK/p' /w/boot.ini.usbmode | md5sum | cut -d' ' -f1)" "card-mode part unchanged"
check "$(sed '1,/^# R36S-USB-CARD-MODE-FALLBACK/d' /boot/boot.ini | md5sum | cut -d' ' -f1)" "$(sed '1d' /boot/boot.ini.darkos | md5sum | cut -d' ' -f1)" "fallback part = boot.ini.darkos minus its header"

echo "== T10: the card-mode boot.ini is the only one console_detect could misread: same dtb_loadaddr line"
check "$(grep -F '${dtb_loadaddr}' /w/boot.ini.usbmode | head -n 1)" "$(grep -F '${dtb_loadaddr}' /k/boot.ini.darkos | head -n 1)" "first \${dtb_loadaddr} line is dArkOS's board file"

echo
echo "passed $PASS, failed $FAIL"
[ "$FAIL" -eq 0 ]
