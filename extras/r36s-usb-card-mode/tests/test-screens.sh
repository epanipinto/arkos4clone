#!/bin/sh
# Offline tests for the USB card mode screens (v7.1, dArkOS card): the helper's setup_screens() and screen(), and
# post-flash.sh's r36s_screen(), run with the initramfs busybox 1.32.1 in a chroot with a fake fb0 (sysfs attribute files
# and a plain /dev/fb0 file). Runs in Docker's arm64 busybox image:
#   docker run --rm --platform linux/arm64 -v "<extras/r36s-usb-card-mode>:/w:ro" -v "<fixtures>:/k:ro" busybox \
#     sh /w/tests/test-screens.sh
# /k = initramfs.cpio, extracted from EmuELEC's KERNEL. The real panel can only be checked on the R36S.
PASS=0
FAIL=0
ok() { PASS=$((PASS + 1)); echo "  ok: $*"; }
bad() { FAIL=$((FAIL + 1)); echo "  FAIL: $*"; }
check() { if [ "$1" = "$2" ]; then ok "$3"; else bad "$3: got [$1] want [$2]"; fi; }
md5() { if [ -f "$1" ]; then md5sum < "$1" | cut -d' ' -f1; else echo "MISSING:$1"; fi; }

mkdir -p /r && (cd /r && cpio -id < /k/initramfs.cpio 2> /dev/null)
mkdir -p /r/tmp /r/flash/usbmode /r/sys/class/graphics/fb0 /r/dev
[ -e /r/dev/null ] || mknod -m 666 /r/dev/null c 1 3
sed -n '/^setup_screens() {/,/^}/p;/^screen() {/,/^}/p' /w/r36s-usb.v7.sh > /r/tmp/screens.sh
sed -n '/^r36s_screen() {/,/^}/p' /w/post-flash.boot.sh > /r/tmp/pf-screen.sh
FBDIR=/r/sys/class/graphics/fb0

echo "== 1: screens.tar: six screens, each a 640x480 XRGB8888 image"
mkdir -p /x && tar -xf /w/screens.tar -C /x
check "$(cd /x && ls | tr '\n' ' ')" "active.gz connect.gz error.gz nopc.gz off.gz recovery.gz " "the screens the scripts ask for"
for f in /x/*.gz; do
  gunzip -c "$f" > "${f%.gz}.raw"
  check "$(wc -c < "${f%.gz}.raw" | tr -d ' ')" "1228800" "${f##*/} is 640 x 480 x 4 bytes"
done
cp /w/screens.tar /r/flash/usbmode/

fb() {
  printf '%s\n' "$1" > $FBDIR/virtual_size
  printf '%s\n' "$2" > $FBDIR/bits_per_pixel
  printf '%s\n' "$3" > $FBDIR/stride
  printf '1\n' > $FBDIR/blank
  : > /r/dev/fb0
  rm -rf /r/tmp/r36s-screens /r/tmp/log
}
# $1 = HOST, $2 = R36S_BOOTINI, then the screens to show in order.
helper() {
  chroot /r /usr/bin/busybox sh -c '
    BB=/usr/bin/busybox; LOG=/tmp/log; SRC=/flash/usbmode; FB=/sys/class/graphics/fb0; SCREENS=/tmp/r36s-screens; FB_ON=
    msg() { echo "$*" >> $LOG; }
    HOST=$1; R36S_BOOTINI=$2; shift 2
    . /tmp/screens.sh
    setup_screens; echo "setup=$? FB_ON=$FB_ON"
    for s in "$@"; do screen "$s"; done' sh "$@"
}

echo "== 2: helper on the dArkOS card"
fb 640,480 32 2560
check "$(helper darkos restored connect)" "setup=0 FB_ON=1" "fb0 is 640x480 XRGB8888: screens on"
check "$(cat $FBDIR/blank)" "0" "fb0 unblanked, so the kernel sets the panel's mode"
check "$(md5 /r/dev/fb0)" "$(md5 /x/connect.raw)" "connect screen drawn"
check "$(ls /r/tmp/r36s-screens | wc -l | tr -d ' ')" "6" "screens unpacked to RAM before USB mode"
helper darkos restored connect active > /dev/null
check "$(md5 /r/dev/fb0)" "$(md5 /x/active.raw)" "active screen replaces it"
for s in off nopc error; do
  helper darkos restored "$s" > /dev/null
  check "$(md5 /r/dev/fb0)" "$(md5 /x/$s.raw)" "$s screen"
done

echo "== 3: dArkOS's boot.ini not put back: the closing screens point to the recovery file"
for s in off nopc error; do
  helper darkos failed "$s" > /dev/null
  check "$(md5 /r/dev/fb0)" "$(md5 /x/recovery.raw)" "$s -> recovery screen"
done
helper darkos failed active > /dev/null
check "$(md5 /r/dev/fb0)" "$(md5 /x/active.raw)" "active screen unchanged"

echo "== 4: no screens when they could be wrong, or on the games card (EMUELEC keeps v7's behaviour)"
fb 640,480 32 2560
check "$(helper emuelec '' connect active off)" "setup=1 FB_ON=" "EMUELEC host: nothing"
check "$(cat $FBDIR/blank) $(wc -c < /r/dev/fb0 | tr -d ' ')" "1 0" "EMUELEC host: fb0 untouched, still blanked"
fb 480,640 32 1920
check "$(helper darkos restored connect)" "setup=1 FB_ON=" "other fb0 size: nothing"
check "$(cat $FBDIR/blank) $(wc -c < /r/dev/fb0 | tr -d ' ')" "1 0" "other fb0 size: fb0 untouched"
check "$(cat /r/tmp/log)" "No screens: fb0 is [480,640 32 1920], not [640,480 32 2560]" "other fb0 size: logged"
fb 640,480 16 1280
check "$(helper darkos restored connect)" "setup=1 FB_ON=" "16-bit fb0: nothing"
fb 640,480 32 2560
rm /r/dev/fb0
check "$(helper darkos restored connect)" "setup=1 FB_ON=" "no /dev/fb0 (display not bound): nothing"
fb 640,480 32 2560
mv /r/flash/usbmode/screens.tar /r/flash/usbmode/screens.off
check "$(helper darkos restored connect)" "setup=1 FB_ON=" "no screens.tar: nothing"
mv /r/flash/usbmode/screens.off /r/flash/usbmode/screens.tar

echo "== 5: post-flash.sh draws error and recovery from the card (USB mode never started)"
pf() { chroot /r /usr/bin/busybox sh -c '. /tmp/pf-screen.sh; r36s_screen "$1"' sh "$1"; }
for s in error recovery; do
  fb 640,480 32 2560
  pf "$s"
  check "$(cat $FBDIR/blank) $(md5 /r/dev/fb0)" "0 $(md5 /x/$s.raw)" "$s screen drawn, fb0 unblanked"
done
fb 480,640 32 1920
pf error
check "$(cat $FBDIR/blank) $(wc -c < /r/dev/fb0 | tr -d ' ')" "1 0" "other fb0 size: nothing"
fb 640,480 32 2560
rm /r/dev/fb0
pf error
check "$(ls /r/dev/fb0 2> /dev/null | wc -l | tr -d ' ') $(cat $FBDIR/blank)" "0 1" "no /dev/fb0: nothing (no file created)"

echo
echo "passed $PASS, failed $FAIL"
[ "$FAIL" -eq 0 ]
