#!/bin/sh
# Checks that dArkOS's own console_detect (which reads the board file from boot.ini's ${dtb_loadaddr} line) still
# reports clone-r36s when boot.ini is the card-mode one (as after a U-Boot fallback). Runs in Docker's arm64 busybox:
#   docker run --rm --platform linux/arm64 -v "<cd>:/cd:ro" -v "<extras/r36s-usb-card-mode>:/w:ro" -v "<fixtures>:/k:ro" \
#     busybox sh /w/tests/test-console-detect.sh
# /cd = console_detect (rootfs/dArkOS/usr/local/bin in this repository), with ld-linux-aarch64.so.1 and libc.so.6 from
# the dArkOS4Clone root image (/lib/ld-linux-aarch64.so.1, /lib/aarch64-linux-gnu/libc.so.6).
# /k = boot.ini.darkos (dArkOS's boot.ini for clone type2 panel1 amp).
mkdir -p /boot
CD="/cd/ld-linux-aarch64.so.1 --library-path /cd /cd/console_detect"
FAIL=0
for f in /k/boot.ini.darkos /w/boot.ini.usbmode; do
  cp "$f" /boot/boot.ini
  got=$($CD -b 2>&1)
  echo "$f -> $got"
  [ "$got" = "clone-r36s" ] || FAIL=1
done
sed 's/rk3326-r36s-type2-panel1-with-amp-linux.dtb/rk3326-evb-lp3-v12-linux.dtb/' /w/boot.ini.usbmode > /boot/boot.ini
got=$($CD -b 2>&1)
echo "negative control (card-mode board file on the dtb_loadaddr line) -> $got"
[ "$got" != "clone-r36s" ] || FAIL=1
[ "$FAIL" -eq 0 ] && echo "console_detect: OK" || echo "console_detect: FAILED"
[ "$FAIL" -eq 0 ]
