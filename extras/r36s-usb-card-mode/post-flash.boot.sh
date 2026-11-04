# R36S USB card mode, started from the dArkOS card. "USB Card Mode" in dArkOS's Tools menu put a card-mode boot.ini
# in place for one boot; U-Boot loaded EmuELEC's kernel from usbmode/, and its initramfs mounted this BOOT partition
# read-only on /flash and sources this file right away (before it would look for EmuELEC's SYSTEM file).
# 1. Put dArkOS's boot.ini back (boot.ini.darkos, else boot.ini.darkos-original), so the next power-on starts dArkOS
#    whatever happens below. The copy goes through boot.ini.new, so boot.ini is never half written.
# 2. Run the card-mode helper: it shares both cards with the PC and powers off at the end.
# 3. Never return to the initramfs: it would stop in a debug shell looking for SYSTEM. Power off instead.
# The card-mode kernel has no framebuffer console, so the messages only reach the kernel log; the panel shows one of the
# pre-rendered screens in usbmode/screens.tar instead (the helper shows the others).
# Everything here is prefixed r36s_ because the initramfs init sources this file into its own shell.
r36s_say() {
  echo "$*" > /dev/tty0 2>/dev/null
  echo "r36s-usb: $*" > /dev/kmsg 2>/dev/null
}

# $1 = error or recovery. Unblanking fb0 makes the kernel turn the panel on; drawn only if fb0 is 640x480 XRGB8888.
r36s_screen() {
  r36s_fb=/sys/class/graphics/fb0
  [ -e /dev/fb0 ] && [ -f /flash/usbmode/screens.tar ] || return 0
  r36s_geo="$(/usr/bin/busybox cat $r36s_fb/virtual_size 2>/dev/null) $(/usr/bin/busybox cat $r36s_fb/bits_per_pixel 2>/dev/null) $(/usr/bin/busybox cat $r36s_fb/stride 2>/dev/null)"
  [ "$r36s_geo" = "640,480 32 2560" ] || return 0
  echo 0 > $r36s_fb/blank 2>/dev/null
  /usr/bin/busybox tar -xOf /flash/usbmode/screens.tar "$1.gz" 2>/dev/null | /usr/bin/busybox gunzip -c > /dev/fb0 2>/dev/null
}

r36s_off() {
  r36s_say "$1"
  /usr/bin/busybox sync
  /usr/bin/busybox sleep 8
  /usr/bin/busybox poweroff -f
  echo o > /proc/sysrq-trigger 2>/dev/null
  /usr/bin/busybox sleep 5
  r36s_say "Hold the power button to turn the R36S off."
  while :; do /usr/bin/busybox sleep 60; done
}

r36s_restore() {
  for r36s_ini in /flash/boot.ini.darkos /flash/boot.ini.darkos-original; do
    [ -s "$r36s_ini" ] || continue
    /usr/bin/busybox grep -q R36S-USB-CARD-MODE "$r36s_ini" && continue
    /usr/bin/busybox mount -o remount,rw /flash || return 1
    /usr/bin/busybox cp "$r36s_ini" /flash/boot.ini.new && /usr/bin/busybox sync &&
      /usr/bin/busybox mv -f /flash/boot.ini.new /flash/boot.ini
    r36s_rc=$?
    /usr/bin/busybox sync
    /usr/bin/busybox mount -o remount,ro /flash
    [ $r36s_rc -eq 0 ] || return 1
    [ "$(/usr/bin/busybox md5sum < /flash/boot.ini)" = "$(/usr/bin/busybox md5sum < "$r36s_ini")" ] || return 1
    r36s_say "dArkOS's boot.ini is back (from ${r36s_ini#/flash/}): the next power-on starts dArkOS."
    return 0
  done
  return 1
}

if r36s_restore; then
  export R36S_BOOTINI=restored
else
  export R36S_BOOTINI=failed
  r36s_say "WARNING: dArkOS's boot.ini could not be put back. See USB-CARD-MODE-RECOVERY.txt on the BOOT drive."
fi
/usr/bin/busybox sh /flash/usbmode/r36s-usb.v7.sh
if [ "$R36S_BOOTINI" = "restored" ]; then
  r36s_screen error
  r36s_off "USB card mode could not start - powering off. Turn the R36S on to play."
fi
r36s_screen recovery
r36s_off "USB card mode could not start and dArkOS's boot.ini was not put back - see USB-CARD-MODE-RECOVERY.txt."
