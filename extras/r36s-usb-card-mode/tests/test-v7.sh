#!/bin/sh
# Offline tests for USB card mode v7 started from the dArkOS card (BOOT). Runs in Docker's arm64 busybox image:
#   docker run --rm --platform linux/arm64 -v "<extras/r36s-usb-card-mode>:/w:ro" -v "<fixtures>:/k:ro" busybox \
#     sh /w/tests/test-v7.sh
# /w = the feature folder. /k = files this repository can't ship (see README.md, Tests): initramfs.cpio (extracted from
# EmuELEC's KERNEL), KERNEL, rk3326-evb-lp3-v12-linux.dtb (the card-mode board file install-usb-card-mode.py makes),
# rk3326-evb-lp3-v12-linux.dtb.orig (the original), busybox-r36s, boot.ini.darkos (dArkOS's boot.ini for clone type2
# panel1 amp), and boot-head.img and emuelec-head.img (the first 16 MiB of the BOOT and EMUELEC partitions). Never run
# it --privileged: post-flash.sh can write /proc/sysrq-trigger, which Docker keeps read-only, and the mocked poweroff
# stops each case before that line anyway.
PASS=0
FAIL=0
ok() { PASS=$((PASS + 1)); echo "  ok: $*"; }
bad() { FAIL=$((FAIL + 1)); echo "  FAIL: $*"; }
check() { if [ "$1" = "$2" ]; then ok "$3"; else bad "$3: got [$1] want [$2]"; fi; }
has() { if grep -qF -- "$2" "$1"; then ok "$3"; else bad "$3: [$2] not in $1"; sed 's/^/    | /' "$1"; fi; }
hasnot() { if grep -qF -- "$2" "$1"; then bad "$3: [$2] found in $1"; else ok "$3"; fi; }
md5() { if [ -f "$1" ]; then /bin/busybox md5sum < "$1" | cut -d' ' -f1; else echo "MISSING:$1"; fi; }

# Mock busybox for post-flash.sh: mount and poweroff are recorded (poweroff ends the case), sleep is instant.
mkdir -p /usr/bin
cat > /usr/bin/busybox << 'EOF'
#!/bin/sh
case "$1" in
  mount) echo "mount $*" >> /tmp/calls; exit "${MOCK_MOUNT_RC:-0}" ;;
  poweroff) echo "poweroff" >> /tmp/calls; kill -9 "$PPID"; exit 0 ;;
  sleep) exit 0 ;;
esac
exec /bin/busybox "$@"
EOF
chmod 755 /usr/bin/busybox

echo "== A: syntax, with busybox 1.38 and with the initramfs busybox 1.32.1"
mkdir -p /r && (cd /r && cpio -id < /k/initramfs.cpio 2> /dev/null)
mkdir -p /r/tmp /r/flash /r/dev
[ -e /r/dev/null ] || mknod -m 666 /r/dev/null c 1 3
for f in r36s-usb.v7.sh post-flash.boot.sh; do
  if sh -n "/w/$f"; then ok "sh -n $f"; else bad "sh -n $f"; fi
  cp "/w/$f" "/r/tmp/$f"
  if chroot /r /usr/bin/busybox sh -n "/tmp/$f"; then ok "initramfs sh -n $f"; else bad "initramfs sh -n $f"; fi
done

echo "== S: every busybox applet the scripts call exists in the initramfs busybox (chmod has a fallback)"
LIST=" $(chroot /r /usr/bin/busybox --list | tr '\n' ' ') "
for a in $( (sed -n 's/.*\$BB \([a-z0-9]*\).*/\1/p' /w/r36s-usb.v7.sh; grep -o '\$BB [a-z0-9]*' /w/r36s-usb.v7.sh | cut -d' ' -f2;
  grep -o '/usr/bin/busybox [a-z0-9]*' /w/post-flash.boot.sh /w/r36s-usb.v7.sh | cut -d' ' -f2) | sort -u); do
  case "$LIST" in
    *" $a "*) ok "initramfs busybox has $a" ;;
    *) if [ "$a" = chmod ]; then ok "chmod missing, as known: setup_clock_tools() falls back"; else bad "initramfs busybox lacks $a"; fi ;;
  esac
done
XLIST=" $(/k/busybox-r36s --list | tr '\n' ' ') "
for a in $(grep -o '\$XB [a-z][a-z0-9]*' /w/r36s-usb.v7.sh | cut -d' ' -f2 | sort -u); do
  case "$XLIST" in
    *" $a "*) ok "busybox-r36s has $a" ;;
    *) bad "busybox-r36s lacks $a" ;;
  esac
done

echo "== B: host_setup() with the initramfs busybox's blkid on real FAT images"
sed -n '/^host_setup() {/,/^}/p' /w/r36s-usb.v7.sh > /r/tmp/host_setup.sh
# The first 16 MiB of the real partitions (from the dArkOS4Clone image and the games card's raw backup) are enough
# for blkid. mkfs.vfat's tiny FAT32 is one blkid can't read a label from: the boot.ini.usbmode fallback case.
cp /k/boot-head.img /k/emuelec-head.img /r/tmp/
/k/busybox-r36s dd if=/dev/zero of=/r/tmp/nolabel.img bs=1024 count=8192 2> /dev/null
/k/busybox-r36s mkfs.vfat -n BOOT /r/tmp/nolabel.img > /dev/null
blkid_field() { chroot /r /usr/bin/busybox blkid "$1" | sed -n "s/.* $2=\"\([^\"]*\)\".*/\1/p"; }
check "$(blkid_field /tmp/boot-head.img LABEL) $(blkid_field /tmp/boot-head.img UUID)" "BOOT F162-CB2C" "dArkOS BOOT: label BOOT, UUID F162-CB2C as in boot=UUID=F162-CB2C"
check "$(blkid_field /tmp/emuelec-head.img LABEL) $(blkid_field /tmp/emuelec-head.img UUID)" "EMUELEC 1210-3527" "games card EMUELEC: label EMUELEC, UUID 1210-3527 as in its extlinux.conf"
check "$(blkid_field /tmp/nolabel.img LABEL)" "" "tiny mkfs.vfat image: blkid finds no label"
hs() {
  rm -f /r/flash/boot.ini.usbmode
  [ "$3" = usbmode ] && : > /r/flash/boot.ini.usbmode
  chroot /r /usr/bin/busybox sh -c 'BB=/usr/bin/busybox; FLASHDEV=$1; R36S_BOOTINI=$2; . /tmp/host_setup.sh; host_setup
    echo "$FLASH_LABEL|$HOST|$PC_WAIT|$DONE_MSG|$NOPC_MSG"' sh "$1" "$2"
}
V6_DONE="All writes saved - powering off. To play, put this card in slot TF2 and the dArkOS card in slot TF1."
V6_NOPC="No PC detected. This card holds the games and the USB card mode: to play, put it in slot TF2 and the dArkOS card in slot TF1."
D_DONE="All writes saved - powering off. Turn the R36S on to play."
D_NOPC="No PC detected - powering off. Turn the R36S on to play."
F_NEXT="dArkOS's boot.ini was not restored: see USB-CARD-MODE-RECOVERY.txt on the BOOT drive."
check "$(hs /tmp/boot-head.img restored)" "BOOT|darkos|90|$D_DONE|$D_NOPC" "label BOOT: dArkOS host, 90 s wait, play messages"
check "$(hs /tmp/emuelec-head.img '')" "EMUELEC|emuelec|20|$V6_DONE|$V6_NOPC" "label EMUELEC: same wait and messages as v6.1"
check "$(hs /tmp/emuelec-head.img '' usbmode)" "EMUELEC|darkos|90|$D_DONE|$D_NOPC" "boot.ini.usbmode on /flash also means the dArkOS card"
check "$(hs /tmp/nolabel.img '' usbmode)" "|darkos|90|$D_DONE|$D_NOPC" "no label but boot.ini.usbmode: dArkOS host"
check "$(hs /tmp/nolabel.img '')" "|emuelec|20|$V6_DONE|$V6_NOPC" "no label, no boot.ini.usbmode: EMUELEC host"
check "$(hs /tmp/missing.img '' usbmode)" "|darkos|90|$D_DONE|$D_NOPC" "blkid fails but boot.ini.usbmode: dArkOS host"
check "$(hs /tmp/boot-head.img failed)" "BOOT|darkos|90|All writes saved - powering off. $F_NEXT|No PC detected - powering off. $F_NEXT" "restore failed: messages point to the recovery file"

echo "== C: the helper's folder survives the re-exec from RAM"
mkdir -p /flash/usbmode
sed -n '1,/^SRC=/p' /w/r36s-usb.v7.sh > /flash/usbmode/stub.sh
echo 'echo "SRC=$SRC"' >> /flash/usbmode/stub.sh
cp /flash/usbmode/stub.sh /flash/stub.sh
check "$(/usr/bin/busybox sh /flash/usbmode/stub.sh)" "SRC=/flash/usbmode" "dArkOS card: files and log in /flash/usbmode"
check "$(/usr/bin/busybox sh /flash/stub.sh)" "SRC=/flash" "EMUELEC: files and log in /flash, as v6.1"

echo "== D: clock tools are taken from the helper's folder"
sed -n '/^setup_clock_tools() {/,/^}/p' /w/r36s-usb.v7.sh > /tmp/sct.sh
cp /k/busybox-r36s /flash/usbmode/busybox-r36s
check "$(LOG=/tmp/sct.log; BB=/bin/busybox; SRC=/flash/usbmode; . /tmp/sct.sh; setup_clock_tools; echo "rc=$? XB=$XB")" "rc=0 XB=/tmp/busybox-r36s" "static busybox copied from /flash/usbmode"
rm -f /tmp/busybox-r36s

echo "== E: post-flash.sh on BOOT: boot.ini first, then the helper, then always power off"
cat > /tmp/mock-helper.sh << 'EOF'
echo "helper R36S_BOOTINI=$R36S_BOOTINI boot.ini=$(/bin/busybox md5sum < /flash/boot.ini | cut -d' ' -f1)" >> /tmp/calls
exit "${MOCK_HELPER_RC:-1}"
EOF
DARKOS=$(md5 /k/boot.ini.darkos)
USBMODE=$(md5 /w/boot.ini.usbmode)
fresh_flash() {
  rm -rf /flash
  mkdir -p /flash/usbmode
  cp /w/post-flash.boot.sh /flash/post-flash.sh
  cp /tmp/mock-helper.sh /flash/usbmode/r36s-usb.v7.sh
  cp /w/boot.ini.usbmode /flash/boot.ini
  cp /k/boot.ini.darkos /flash/boot.ini.darkos
  cp /k/boot.ini.darkos /flash/boot.ini.darkos-original
}
run_pf() {
  : > /tmp/calls
  # The real /dev/tty0 is a terminal; here it points at fd 9, a pipe, so every line is kept (">" can't truncate).
  ln -sf /proc/self/fd/9 /dev/tty0
  { timeout 20 sh -c 'boot=UUID=F162-CB2C; BOOT_STEP=mount_flash; . /flash/post-flash.sh; echo RETURNED >> /tmp/calls' 9>&1 > /dev/null 2>&1; } 2> /dev/null | cat > /tmp/screen
}
fresh_flash
run_pf
check "$(md5 /flash/boot.ini)" "$DARKOS" "E1 boot.ini is dArkOS's again"
check "$(tr '\n' ';' < /tmp/calls)" "mount mount -o remount,rw /flash;mount mount -o remount,ro /flash;helper R36S_BOOTINI=restored boot.ini=$DARKOS;poweroff;" "E1 order: rw, restore, ro, then the helper (already restored), then power off"
has /tmp/screen "dArkOS's boot.ini is back (from boot.ini.darkos)" "E1 says boot.ini is back"
has /tmp/screen "USB card mode could not start - powering off. Turn the R36S on to play." "E1 helper failure: power off, play next"
check "$(ls /flash | grep -c 'boot.ini.new')" "0" "E1 no boot.ini.new left behind"

fresh_flash
rm /flash/boot.ini.darkos
run_pf
check "$(md5 /flash/boot.ini)" "$DARKOS" "E2 no boot.ini.darkos: restored from boot.ini.darkos-original"
has /tmp/screen "(from boot.ini.darkos-original)" "E2 says which copy"

fresh_flash
cp /w/boot.ini.usbmode /flash/boot.ini.darkos
run_pf
check "$(md5 /flash/boot.ini)" "$DARKOS" "E3 boot.ini.darkos is a card-mode file: skipped, -original used"

fresh_flash
rm /flash/boot.ini.darkos /flash/boot.ini.darkos-original
run_pf
check "$(md5 /flash/boot.ini)" "$USBMODE" "E4 nothing to restore: boot.ini untouched"
has /tmp/calls "helper R36S_BOOTINI=failed" "E4 helper still runs (the PC can fix boot.ini) and is told"
has /tmp/screen "WARNING: dArkOS's boot.ini could not be put back" "E4 warning on screen"
has /tmp/screen "was not put back - see USB-CARD-MODE-RECOVERY.txt" "E4 power-off message points to the recovery file"
hasnot /tmp/calls "RETURNED" "E4 never returns to the initramfs"

fresh_flash
export MOCK_MOUNT_RC=1
run_pf
unset MOCK_MOUNT_RC
check "$(md5 /flash/boot.ini)" "$USBMODE" "E5 /flash can't be made writable: boot.ini untouched"
check "$(tr '\n' ';' < /tmp/calls)" "mount mount -o remount,rw /flash;helper R36S_BOOTINI=failed boot.ini=$USBMODE;poweroff;" "E5 helper told, then power off"

fresh_flash
export MOCK_HELPER_RC=0
run_pf
unset MOCK_HELPER_RC
check "$(tail -n 1 /tmp/calls)" "poweroff" "E6 even a helper that returns 0 ends in power off"
hasnot /tmp/calls "RETURNED" "E6 never returns to the initramfs"

fresh_flash
cp /w/r36s-usb.v7.sh /flash/usbmode/r36s-usb.v7.sh
run_pf
check "$(tr '\n' ';' < /tmp/calls)" "mount mount -o remount,rw /flash;mount mount -o remount,ro /flash;poweroff;" "E7 real v7 without a USB device controller exits, post-flash powers off"

echo "== I: boot.ini.usbmode in U-Boot (commands mocked in sh; real KERNEL, board file, sizes and MD5s)"
cat > /tmp/uboot.sh << 'EOF'
setenv() { eval "$1=\$2"; }
load() {
  [ -f "/ub/$4" ] || { echo "load-fail $4" >> /tmp/ub; return 1; }
  eval "MEM_$3=/ub/$4"
  echo "load $3 $4" >> /tmp/ub
}
md5sum() {
  eval "f=\$MEM_$2"
  got=$(head -c $((0x$3)) "$f" | /bin/busybox md5sum | cut -d' ' -f1)
  if [ "$got" = "$4" ]; then echo "md5-ok $2" >> /tmp/ub; return 0; fi
  echo "md5-bad $2" >> /tmp/ub
  return 1
}
booti() { echo "booti $* | $bootargs" >> /tmp/ub; exit 0; }
EOF
sed '1d' /w/boot.ini.usbmode > /tmp/body.sh
fresh_ub() {
  rm -rf /ub
  mkdir -p /ub/usbmode
  cp /k/KERNEL /k/rk3326-evb-lp3-v12-linux.dtb /ub/usbmode/
  for f in Image uInitrd rk3326-r36s-type2-panel1-with-amp-linux.dtb; do echo "$f" > "/ub/$f"; done
}
run_ub() {
  : > /tmp/ub
  sh -c '. /tmp/uboot.sh; . /tmp/body.sh; echo "no booti" >> /tmp/ub'
}
CARD="booti 0x02000000 - 0x01f00000 | boot=UUID=F162-CB2C quiet console=ttyFIQ0 console=tty0 fbcon=rotate:3 consoleblank=0"
DARK="booti 0x02000000 0x01100000 0x01f00000 | root=/dev/mmcblk0p2"
fresh_ub
run_ub
check "$(tr '\n' ';' < /tmp/ub)" "load 0x02000000 usbmode/KERNEL;md5-ok 0x02000000;load 0x01f00000 usbmode/rk3326-evb-lp3-v12-linux.dtb;md5-ok 0x01f00000;$CARD;" "I1 files intact: card-mode kernel boots"
fresh_ub
rm /ub/usbmode/KERNEL
run_ub
has /tmp/ub "$DARK" "I2 no KERNEL: dArkOS boots"
has /tmp/ub "load 0x01f00000 rk3326-r36s-type2-panel1-with-amp-linux.dtb" "I2 with dArkOS's board file"
fresh_ub
printf 'X' | dd of=/ub/usbmode/KERNEL bs=1 seek=10000000 conv=notrunc 2> /dev/null
run_ub
has /tmp/ub "md5-bad 0x02000000" "I3 damaged KERNEL caught by MD5"
has /tmp/ub "$DARK" "I3 dArkOS boots"
fresh_ub
head -c 20000000 /k/KERNEL > /ub/usbmode/KERNEL
run_ub
has /tmp/ub "$DARK" "I4 truncated KERNEL: dArkOS boots"
fresh_ub
rm /ub/usbmode/rk3326-evb-lp3-v12-linux.dtb
run_ub
has /tmp/ub "$DARK" "I5 no card-mode board file: dArkOS boots"
fresh_ub
cp /k/rk3326-evb-lp3-v12-linux.dtb.orig /ub/usbmode/rk3326-evb-lp3-v12-linux.dtb
run_ub
has /tmp/ub "md5-bad 0x01f00000" "I6 wrong board file (the host-mode original) caught by MD5"
has /tmp/ub "$DARK" "I6 dArkOS boots"

echo "== Q: serial console (v7.2), in the initramfs busybox against a mock configfs"
# The helper's functions run in the initramfs (chroot /r), with busybox-r36s as XB under its own name (busybox picks the
# applet from its file name). BB is a function: the initramfs busybox with an instant sleep. udc_state() returns the
# USB states listed in /tmp/q/states, one per call, then repeats the last; RESTART in the list also sets the restart flag.
Q=/r/tmp/q
mkdir -p $Q
cp /k/busybox-r36s $Q/busybox-r36s
for f in has_applets serial_setup serial_remove start_udc wait_for_pc console_session console_tools r36s_status \
  r36s_shot r36s_reboot wait_for_unplug restart finish; do
  sed -n "/^$f() {/,/^}/p" /w/r36s-usb.v7.sh
done > $Q/funcs.sh
{
  cat << 'EOF'
bbf() { [ "$1" = sleep ] && return 0; /usr/bin/busybox "$@"; }
BB=bbf
XB=/tmp/q/busybox-r36s
LOG=/tmp/q/log
G=/tmp/q/g
F=$G/functions/mass_storage.0
SRC=/tmp/q/src
UDC=ff300000.usb
HOST=darkos
PC_WAIT=90
FB=/tmp/q/fb
EOF
  grep -E '^(HELPER_VERSION|ACM|TTY|RESTART|RESTART_UNPLUG|CR)=' /w/r36s-usb.v7.sh
  cat << 'EOF'
msg() {
  echo "$*" >> /tmp/q/msgs
  case $* in *"card reader only"*) [ -f /tmp/q/udc-heals ] && $BB rm -rf "$G/UDC" ;; esac
}
fail() { echo "FAIL $*" >> /tmp/q/msgs; exit 1; }
udc_state() {
  if IFS= read -r s <&7; then echo "$s" > /tmp/q/last; else IFS= read -r s < /tmp/q/last; fi
  [ "$s" = RESTART ] && { : > "$RESTART"; s=configured; }
  echo "$s"
}
: > /tmp/q/last
[ -f /tmp/q/states ] && exec 7< /tmp/q/states
. /tmp/q/funcs.sh
EOF
} > $Q/prelude.sh
qrun() { chroot /r /usr/bin/busybox sh -c ". /tmp/q/prelude.sh; $1" 2>&1; }
fresh_g() {
  rm -rf $Q/g $Q/src $Q/msgs $Q/log $Q/states $Q/udc-heals /r/tmp/r36s-restart /r/tmp/r36s-restart-unplug
  mkdir -p $Q/g/configs/c.1/strings/0x409 $Q/g/functions/mass_storage.0/lun.0 $Q/g/functions/mass_storage.0/lun.1 $Q/src
  echo 0x0104 > $Q/g/idProduct
  for a in bDeviceClass bDeviceSubClass bDeviceProtocol; do echo 0x00 > $Q/g/$a; done
  echo "Mass storage" > $Q/g/configs/c.1/strings/0x409/configuration
  : > $Q/g/UDC
  echo /dev/mmcblk1 > $Q/g/functions/mass_storage.0/lun.0/file
  echo /dev/mmcblk0 > $Q/g/functions/mass_storage.0/lun.1/file
  ln -s functions/mass_storage.0 $Q/g/configs/c.1/mass_storage.0
  : > $Q/msgs
}
rep() { n=0; while [ $n -lt "$1" ]; do echo "$2"; n=$((n + 1)); done; }
gadget() {
  echo "$(cat $Q/g/idProduct) $(cat $Q/g/bDeviceClass) $(cat $Q/g/bDeviceSubClass) $(cat $Q/g/bDeviceProtocol)" \
    "[$(cat $Q/g/configs/c.1/strings/0x409/configuration)] $(ls $Q/g/configs/c.1 | tr '\n' ' ')[$(cat $Q/g/UDC)]"
}
COMPOSITE="0x0106 0xEF 0x02 0x01 [Card reader + serial] acm.gs0 mass_storage.0 strings"
READER="0x0104 0x00 0x00 0x00 [Mass storage] mass_storage.0 strings"

fresh_g
check "$(qrun 'serial_setup; echo "rc=$? SERIAL=$SERIAL"')" "rc=0 SERIAL=on" "Q1 dArkOS card: serial port set up"
check "$(gadget)" "$COMPOSITE []" "Q1 1d6b:0106 with the IAD device class (EF/02/01), acm.gs0 linked next to mass_storage.0"
check "$(readlink $Q/g/configs/c.1/acm.gs0) $([ -d $Q/g/functions/acm.gs0 ] && echo dir)" "functions/acm.gs0 dir" "Q1 link made from the gadget folder"
has $Q/msgs "Serial console set up (USB 1d6b:0106, /dev/ttyGS0)" "Q1 says so"
check "$(qrun 'SERIAL=on; serial_remove; echo "SERIAL=[$SERIAL]"')" "SERIAL=[]" "Q2 serial_remove"
check "$(gadget)" "$READER []" "Q2 back to v7.1's card reader: 1d6b:0104, no device class, only mass_storage.0"
check "$([ -e $Q/g/functions/acm.gs0 ] && echo left || echo gone)" "gone" "Q2 acm.gs0 removed"

for sw in no-serial no-serial.txt; do
  fresh_g
  : > $Q/src/$sw
  check "$(qrun 'serial_setup; echo "rc=$? SERIAL=[$SERIAL]"')" "rc=1 SERIAL=[]" "Q3 switch file usbmode/$sw: no serial port"
  check "$(gadget)" "$READER []" "Q3 $sw: v7.1's card reader"
  has $Q/msgs "Serial console off: usbmode/no-serial is on BOOT" "Q3 $sw: says why"
done
fresh_g
check "$(qrun 'HOST=emuelec; serial_setup; echo "rc=$?"')" "rc=1" "Q4 games card (EMUELEC): no serial port"
check "$(gadget)|$(cat $Q/msgs)" "$READER []|" "Q4 nothing changed or said"
fresh_g
check "$(qrun 'XB=$BB; serial_setup; echo "rc=$?"')" "rc=1" "Q5 busybox-r36s didn't run (XB is the initramfs busybox): no serial port"
has $Q/msgs "Serial console unavailable: it needs busybox-r36s" "Q5 says why"
fresh_g
check "$(qrun 'XB=/usr/bin/busybox; serial_setup; echo "rc=$?"')" "rc=1" "Q6 a busybox without ln, rmdir, stty, base64: no serial port"
check "$(qrun 'has_applets $XB ln rmdir stty base64 gzip && echo yes; has_applets /usr/bin/busybox ln || echo no' | tr '\n' ' ')" "yes no " "Q6 has_applets"
fresh_g
: > $Q/g/functions/acm.gs0
check "$(qrun 'serial_setup; echo "rc=$? SERIAL=[$SERIAL]"')" "rc=1 SERIAL=[]" "Q7 the kernel can't make acm.gs0: no serial port"
check "$(cat $Q/g/idProduct)" "0x0104" "Q7 card reader alone"
fresh_g
: > $Q/g/configs/c.1/acm.gs0
check "$(qrun 'serial_setup; echo "rc=$?"')" "rc=1" "Q8 acm.gs0 can't be linked: no serial port"
check "$([ -e $Q/g/functions/acm.gs0 ] && echo left || echo gone) $(cat $Q/g/idProduct)" "gone 0x0104" "Q8 acm.gs0 removed again, card reader alone"

fresh_g
qrun serial_setup > /dev/null
check "$(qrun 'SERIAL=on; start_udc; echo "rc=$? SERIAL=$SERIAL"')" "rc=0 SERIAL=on" "Q9 USB device mode starts with the serial port"
check "$(gadget)" "$COMPOSITE [ff300000.usb]" "Q9 bound as the composite device"
fresh_g
qrun serial_setup > /dev/null
rm -f $Q/g/UDC
mkdir $Q/g/UDC
: > $Q/udc-heals
check "$(qrun 'SERIAL=on; start_udc; echo "rc=$? SERIAL=[$SERIAL]"')" "rc=0 SERIAL=[]" "Q10 the composite device doesn't bind: card reader alone"
check "$(gadget)" "$READER [ff300000.usb]" "Q10 bound as v7.1's card reader"
has $Q/msgs "USB device mode with the serial port failed: card reader only" "Q10 says so"
fresh_g
rm -f $Q/g/UDC
mkdir $Q/g/UDC
check "$(qrun 'start_udc; echo "rc=$?"')" "rc=1" "Q11 the card reader alone doesn't bind: error, as v7.1"
has $Q/log "can't create /tmp/q/g/UDC" "Q11 the reason goes to the log"

fresh_g
{ rep 2 "not attached"; echo configured; } > $Q/states
check "$(qrun 'SERIAL=on; wait_for_pc; echo "rc=$? i=$i st=$st SERIAL=$SERIAL"')" "rc=0 i=2 st=configured SERIAL=on" "Q12 PC configures the composite device"
fresh_g
qrun serial_setup > /dev/null
: > $Q/msgs
{ rep 16 addressed; echo configured; } > $Q/states
check "$(qrun 'SERIAL=on; wait_for_pc; echo "rc=$? i=$i SERIAL=[$SERIAL]"')" "rc=0 i=16 SERIAL=[]" "Q13 not configured within 15 s: card reader alone, then configured"
check "$(gadget)" "$READER [ff300000.usb]" "Q13 rebound as v7.1's card reader"
check "$(grep -c "didn't set up the USB serial port (USB state: addressed): card reader only" $Q/msgs)" "1" "Q13 says so"
fresh_g
qrun serial_setup > /dev/null
: > $Q/msgs
echo addressed > $Q/states
check "$(qrun 'SERIAL=on; wait_for_pc; echo "rc=$? i=$i st=$st"')" "rc=1 i=90 st=addressed" "Q14 never configured: gives up at 90 s"
check "$(grep -c "card reader only" $Q/msgs)" "1" "Q14 one fallback only"
fresh_g
echo "not attached" > $Q/states
check "$(qrun 'SERIAL=on; wait_for_pc; echo "rc=$? i=$i st=$st SERIAL=$SERIAL"')" "rc=1 i=90 st=not attached SERIAL=on" "Q15 no PC: 90 s, as v7.1"
fresh_g
{ rep 80 "not attached"; echo default; } > $Q/states
check "$(qrun 'wait_for_pc; echo "rc=$? i=$i st=$st"')" "rc=1 i=125 st=default" "Q16 PC turns up at 80 s: it gets 45 s"
fresh_g
qrun serial_setup > /dev/null
{ rep 80 "not attached"; rep 20 addressed; echo configured; } > $Q/states
check "$(qrun 'SERIAL=on; wait_for_pc; echo "rc=$? i=$i SERIAL=[$SERIAL]"')" "rc=0 i=100 SERIAL=[]" "Q17 late PC needing the fallback is still served"
fresh_g
echo "not attached" > $Q/states
check "$(qrun 'HOST=emuelec; PC_WAIT=20; wait_for_pc; echo "rc=$? i=$i"')" "rc=1 i=3" "Q18 games card: gives up after 3 s unplugged, as v6.1"
echo default > $Q/states
check "$(qrun 'HOST=emuelec; PC_WAIT=20; wait_for_pc; echo "rc=$? i=$i"')" "rc=1 i=20" "Q18 games card: 20 s at most"

fresh_g
frame() { echo "@@$1 ${#2} $2"; }
{
  echo ""
  echo "garbage"
  echo "@@"
  frame a1 "echo hi"
  printf '%s\r\n' "$(frame a2 'echo hi')"
  echo "@@a3 5 echo hi"
  frame a4 "exit 3"
  frame a5 "cat"
  frame a6 "echo err >&2"
  frame a7 "x=5"
  frame a8 'echo "[$x]"'
  frame a9 " echo spaced "
  frame b1 ""
  frame b2 'echo $HELPER_VERSION $HOST'
  frame b3 'has_applets $XB stty && echo yes'
} > $Q/in
qrun 'console_session < /tmp/q/in > /tmp/q/out'
check "$(tr '\n' '|' < $Q/out)" "@@B a1|hi||@@E a1 0|@@B a2|hi||@@E a2 0|@@X a3 length|@@B a4||@@E a4 3|@@B a5||@@E a5 0|@@B a6|err||@@E a6 0|@@B a7||@@E a7 0|@@B a8|[]||@@E a8 0|@@B a9|spaced||@@E a9 0|@@B b1||@@E b1 0|@@B b2|7.2 darkos||@@E b2 0|@@B b3|yes||@@E b3 0|" "Q19 console: framing, CRLF, length check, exit codes, stderr, stdin, subshells, helper functions"
check "$(qrun 'console_tools; type wc; echo abc | wc -c' | tr '\n' ' ')" "wc is /tmp/r36s-bin/wc 4 " "Q20 console: busybox-r36s applets on PATH too"

fresh_g
echo configured > $Q/states
check "$(qrun 'SERIAL=on; FB_ON=1; R36S_BOOTINI=restored; r36s_status' | tr '\n' '|')" "helper 7.2|host darkos|usb configured|serial on|screens 1|boot.ini restored|share lun.0 /dev/mmcblk1|share lun.1 /dev/mmcblk0|" "Q21 status for r36s-console.py"
check "$(qrun 'r36s_reboot; echo "rc=$?"' | tr '\n' '|')" "Not restarting: /dev/mmcblk1 is still shared. Eject both cards on the PC first.|rc=1|" "Q22 no restart while a card is shared"
: > $Q/g/functions/mass_storage.0/lun.0/file
check "$(qrun 'r36s_reboot; echo "rc=$?"' | tr '\n' '|')" "Not restarting: /dev/mmcblk0 is still shared. Eject both cards on the PC first.|rc=1|" "Q22 nor with one still shared"
check "$([ -e /r/tmp/r36s-restart ] && echo set || echo unset)" "unset" "Q22 no restart flag"
: > $Q/g/functions/mass_storage.0/lun.1/file
check "$(qrun 'r36s_reboot; echo "rc=$?"' | tr '\n' '|')" "Restarting in a moment.|rc=0|" "Q23 both cards ejected: restart"
check "$([ -e /r/tmp/r36s-restart ] && echo set || echo unset)" "set" "Q23 restart flag set"
fresh_g
check "$(qrun 'r36s_reboot force; echo "rc=$?"' | tr '\n' '|')" "Restarting in a moment.|rc=0|" "Q24 force skips the check"
fresh_g
check "$(qrun 'r36s_reboot unplug; echo "rc=$?"' | tr '\n' '|')" "Not restarting: /dev/mmcblk1 is still shared. Eject both cards on the PC first.|rc=1|" "Q24 exit's restart: also only after the eject"
: > $Q/g/functions/mass_storage.0/lun.0/file
: > $Q/g/functions/mass_storage.0/lun.1/file
check "$(qrun 'r36s_reboot unplug; echo "rc=$?"' | tr '\n' '|')" "Unplug the cable now: the R36S then restarts by itself.|rc=0|" "Q24 exit's restart: once unplugged"
check "$([ -e /r/tmp/r36s-restart-unplug ] && echo unplug) $([ -e /r/tmp/r36s-restart ] && echo now)" "unplug " "Q24 only the restart-when-unplugged flag"

FIN='restart() { echo restart; exit 0; }; cleanup() { echo cleanup; }; screen() { echo "screen $1"; }; power_off() { echo "power_off $1"; exit 0; }; DONE_MSG=done; finish'
fresh_g
echo "not attached" > $Q/states
check "$(qrun "$FIN" | tr '\n' '|')" "cleanup|screen off|power_off done|" "Q29 finish: unplugged, nothing asked: power off as v7.1"
fresh_g
echo "not attached" > $Q/states
: > /r/tmp/r36s-restart-unplug
check "$(qrun "$FIN" | tr '\n' '|')" "restart|" "Q29 finish: exit asked for a restart: restart once unplugged"
has $Q/msgs "Cable unplugged" "Q29 only after the unplug"
fresh_g
echo configured > $Q/states
: > /r/tmp/r36s-restart
check "$(qrun "$FIN" | tr '\n' '|')" "restart|" "Q29 finish: rearm asked for a restart: at once, cable still in"

fresh_g
{ rep 3 configured; rep 4 "not attached"; echo configured; rep 10 "not attached"; echo END; } > $Q/states
check "$(qrun 'wait_for_unplug; echo "rc=$?"; IFS= read -r s <&7; echo "next=$s"' | tr '\n' ' ')" "rc=0 next=END " "Q25 unplugged: 10 s in a row (a 4 s drop doesn't count)"
fresh_g
{ rep 2 configured; echo RESTART; rep 5 configured; } > $Q/states
check "$(qrun 'wait_for_unplug; echo "rc=$?"; IFS= read -r s <&7; echo "next=$s"' | tr '\n' ' ')" "rc=1 next=configured " "Q26 restart asked: returns at once"
fresh_g
check "$(qrun 'cleanup() { echo cleanup; }; power_off() { echo "power_off $1"; }; bbf() { [ "$1" = sleep ] || echo "busybox $*"; }; restart' | tr '\n' '|')" \
  "cleanup|busybox reboot -f|power_off The restart didn't happen - powering off. Turn the R36S on to play.|" "Q27 restart: cleanup, reboot -f, no log written to BOOT, power off if it didn't happen"
has $Q/msgs "Restarting, as asked over the serial console" "Q27 says so"

mkdir -p $Q/fb $Q/scr
echo 640,480 > $Q/fb/virtual_size
echo 32 > $Q/fb/bits_per_pixel
echo 2560 > $Q/fb/stride
tar -xf /w/screens.tar -C $Q/scr
gunzip -c $Q/scr/active.gz > /r/dev/fb0
qrun r36s_shot > $Q/shot.txt
check "$(sed -n '1p;/^md5 /p;/^data$/p' $Q/shot.txt | tr '\n' '|')" "geometry 640,480 32 2560|md5 $(md5 /r/dev/fb0)|data|" "Q28 shot: fb0's format, MD5, then the data"
sed '1,/^data$/d' $Q/shot.txt | /k/busybox-r36s base64 -d | gunzip -c > $Q/shot.raw
check "$(md5 $Q/shot.raw)" "$(md5 /r/dev/fb0)" "Q28 shot: the data is fb0, gzipped and base64-encoded"
check "$([ -e /r/tmp/r36s-shot ] && echo left || echo gone)" "gone" "Q28 shot: no copy left in RAM"
rm -f /r/dev/fb0

echo
echo "passed $PASS, failed $FAIL"
[ "$FAIL" -eq 0 ]
