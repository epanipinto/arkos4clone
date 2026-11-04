#!/bin/sh
# R36S USB card helper v7.2 (read-write, both slots, clock sync, serial console). Started by post-flash.sh from the
# EmuELEC initramfs.
# Power on with the OTG port connected to a PC: the PC sees the card in slot TF2 (if any) and this boot card
# in slot TF1 as writable USB drives. Use Safely Remove on the PC, then unplug: everything is flushed to the
# cards and the console powers off.
# v7 runs from either card, told apart by the label of the partition on /flash:
# - EMUELEC, the games card in slot TF1 (as v6): this card no longer boots EmuELEC, so without a PC it just
#   powers off.
# - BOOT, the dArkOS card in slot TF1: "USB Card Mode" in dArkOS's Tools menu boots EmuELEC's kernel from
#   /flash/usbmode for one boot, and post-flash.sh has already put dArkOS's boot.ini back. It waits up to 90 s
#   for the PC, and turning the R36S on afterwards starts dArkOS.
# The helper's files (busybox-r36s, r36s-link.tar) and its log sit next to it: /flash, or /flash/usbmode.
# The card-mode kernel has no framebuffer console, so the messages below only reach the log and the kernel log. Started by
# EmuELEC's U-Boot, the panel keeps the logo that U-Boot hands over. dArkOS's U-Boot hands nothing over, so on the dArkOS
# card the helper turns the panel on itself and shows the pre-rendered screens in usbmode/screens.tar (v7.1).
# On the dArkOS card, v7.2 also adds a USB serial port next to the two card drives: a root shell for r36s-console.py on
# the PC (screenshots, logs, restarting into card mode or dArkOS). An empty usbmode/no-serial file on BOOT turns it off,
# and a PC that doesn't set up the combined device gets the card reader alone, as before.
# It runs from RAM so nothing is read from the card after the PC can change it, and it sets the clock and the
# RK817 real-time clock when the PC writes "R36SCLK1 <unix seconds> <nonce>" into sector 48 of the TF1 card.
# The initramfs busybox has no "ln" applet, so the configfs function link is extracted from r36s-link.tar.
if [ "$1" != "--from-ram" ]; then
  case $0 in
    /*/*) dir=${0%/*} ;;
    *) dir=/flash ;;
  esac
  /usr/bin/busybox cp "$0" /tmp/r36s-usb.sh || exit 1
  exec /usr/bin/busybox sh /tmp/r36s-usb.sh --from-ram "$dir"
fi

SRC=${2:-/flash}
BB=/usr/bin/busybox
XB=/tmp/busybox-r36s
LOG=/tmp/r36s-usb.log
G=/sys/kernel/config/usb_gadget/r36s
F=$G/functions/mass_storage.0
CPUFREQ=/sys/devices/system/cpu/cpu0/cpufreq
UDC=$($BB ls /sys/class/udc 2>/dev/null | $BB head -n 1)
CLK_IN=24576
CLK_ACK=25088
CLOCK_PID=
FB=/sys/class/graphics/fb0
SCREENS=/tmp/r36s-screens
FB_ON=
HELPER_VERSION=7.2
ACM=$G/functions/acm.gs0
TTY=/dev/ttyGS0
RESTART=/tmp/r36s-restart
RESTART_UNPLUG=/tmp/r36s-restart-unplug
SERIAL=
CONSOLE_PID=
CR=$(echo | $BB tr '\n' '\r')

# Original board file (host-only OTG port): nothing to do.
[ -z "$UDC" ] && exit 1

msg() {
  echo "$*" > /dev/tty0 2>/dev/null
  echo "r36s-usb: $*" > /dev/kmsg 2>/dev/null
  echo "[$($BB cut -d' ' -f1 /proc/uptime)] $*" >> "$LOG"
}

udc_state() { $BB cat "/sys/class/udc/$UDC/state" 2>/dev/null; }

set_governor() {
  for g in /sys/devices/system/cpu/cpufreq/policy*/scaling_governor /sys/devices/system/cpu/cpu[0-9]*/cpufreq/scaling_governor; do
    [ -w "$g" ] && echo "$1" > "$g" 2>/dev/null
  done
}

read_ahead() { $BB cat "/sys/block/${1#/dev/}/queue/read_ahead_kb" 2>/dev/null; }

set_read_ahead() {
  [ -n "$1" ] && [ -n "$2" ] && echo "$2" > "/sys/block/${1#/dev/}/queue/read_ahead_kb" 2>/dev/null
}

rtc_epoch() { $BB cat /sys/class/rtc/rtc0/since_epoch 2>/dev/null || echo none; }

rtc_text() {
  if [ -r /sys/class/rtc/rtc0/date ]; then
    echo "$($BB cat /sys/class/rtc/rtc0/date) $($BB cat /sys/class/rtc/rtc0/time) UTC"
  else
    echo "no RTC device"
  fi
}

# The dArkOS card's FAT partition is labelled BOOT and holds boot.ini.usbmode; the games card's is EMUELEC.
# Started from the dArkOS card, the R36S was restarted from dArkOS's menu, so the PC may still need plugging in,
# and the next power-on starts dArkOS (post-flash.sh sets R36S_BOOTINI=failed if it couldn't restore boot.ini).
host_setup() {
  FLASH_LABEL=$($BB blkid "$FLASHDEV" 2>/dev/null | $BB sed -n 's/.* LABEL="\([^"]*\)".*/\1/p')
  if [ "$FLASH_LABEL" = "BOOT" ] || [ -f /flash/boot.ini.usbmode ]; then
    HOST=darkos
    PC_WAIT=90
    next="Turn the R36S on to play."
    [ "$R36S_BOOTINI" = "failed" ] && next="dArkOS's boot.ini was not restored: see USB-CARD-MODE-RECOVERY.txt on the BOOT drive."
    DONE_MSG="All writes saved - powering off. $next"
    NOPC_MSG="No PC detected - powering off. $next"
  else
    HOST=emuelec
    PC_WAIT=20
    DONE_MSG="All writes saved - powering off. To play, put this card in slot TF2 and the dArkOS card in slot TF1."
    NOPC_MSG="No PC detected. This card holds the games and the USB card mode: to play, put it in slot TF2 and the dArkOS card in slot TF1."
  fi
}

# dArkOS card only. Unblanking fb0 makes the kernel set the panel's mode (fbdev emulation does no modeset without a
# framebuffer console). The screens are 640x480 XRGB8888 images, gzipped: unpacked to RAM here, before the PC can change
# the card, and only drawn when fb0 has exactly that format.
setup_screens() {
  [ "$HOST" = "darkos" ] && [ -f "$SRC/screens.tar" ] && [ -e /dev/fb0 ] || return 1
  geo="$($BB cat $FB/virtual_size 2>/dev/null) $($BB cat $FB/bits_per_pixel 2>/dev/null) $($BB cat $FB/stride 2>/dev/null)"
  if [ "$geo" != "640,480 32 2560" ]; then
    msg "No screens: fb0 is [$geo], not [640,480 32 2560]"
    return 1
  fi
  $BB mkdir -p "$SCREENS" && $BB tar -xf "$SRC/screens.tar" -C "$SCREENS" 2>>"$LOG" || return 1
  echo 0 > "$FB/blank" 2>>"$LOG" || return 1
  FB_ON=1
  msg "Screens on (fb0 unblanked)"
}

# $1 = connect, active, off, nopc or error. If dArkOS's boot.ini wasn't put back, the last three say so instead.
screen() {
  [ -n "$FB_ON" ] || return 0
  name=$1
  [ "$R36S_BOOTINI" = "failed" ] && case $1 in off | nopc | error) name=recovery ;; esac
  $BB gunzip -c "$SCREENS/$name.gz" > /dev/fb0 2>>"$LOG"
}

clock_record() { $XB dd if="$TF1" bs=1 skip=$CLK_IN count=63 2>/dev/null; }

clock_watch() {
  set -- $(clock_record)
  last=
  [ "$1" = "R36SCLK1" ] && last=$3
  while :; do
    set -- $(clock_record)
    if [ "$1" = "R36SCLK1" ] && [ -n "$3" ] && [ "$3" != "$last" ]; then
      last=$3
      before=$(rtc_epoch)
      if $XB date -u -s "@$2" > /dev/null 2>&1; then dset=ok; else dset=fail; fi
      if [ ! -e /dev/rtc0 ]; then
        rtc=none
      elif $XB hwclock -w -u -f /dev/rtc0 2>>"$LOG"; then
        rtc=ok
      else
        rtc=fail
      fi
      after=$(rtc_epoch)
      $XB printf '%-127s\n' "R36SACK1 $3 $2 date=$dset rtc=$rtc before=$before after=$after" |
        $XB dd of="$TF1" bs=128 seek=$((CLK_ACK / 128)) count=1 conv=notrunc 2>/dev/null
      $XB sync
      msg "Clock set to $($XB date -u '+%Y-%m-%d %H:%M:%S') UTC (system clock: $dset, RTC: $rtc)"
    fi
    $XB usleep 500000 2>/dev/null || $BB sleep 1
  done
}

# Finds a busybox with the clock applets. The initramfs busybox may lack chmod and /tmp may be noexec, so the
# static copy is tried in several places, with its execute bit set by the copy itself run from the card (still
# safe here: the PC can't change the card before USB mode starts). Falls back to the initramfs busybox.
setup_clock_tools() {
  if [ -f "$SRC/busybox-r36s" ]; then
    for dir in /tmp /dev/shm /run /dev; do
      t=$dir/busybox-r36s
      $BB cp "$SRC/busybox-r36s" "$t" 2>>"$LOG" || continue
      $BB chmod 755 "$t" 2>>"$LOG" || "$SRC/busybox-r36s" chmod 755 "$t" 2>>"$LOG"
      if "$t" true 2>>"$LOG"; then
        XB=$t
        return 0
      fi
      echo "cannot run $t" >> "$LOG"
    done
    $BB cat /proc/mounts >> "$LOG"
  else
    echo "$SRC/busybox-r36s is missing" >> "$LOG"
  fi
  nl='
'
  list=$($BB --list 2>/dev/null)
  for applet in date hwclock dd printf sync; do
    case "$nl$list$nl" in
      *"$nl$applet$nl"*) ;;
      *) echo "initramfs busybox has no $applet" >> "$LOG"; return 1 ;;
    esac
  done
  XB=$BB
}

save_log() {
  {
    echo "--- configfs ---"
    $BB ls -la "$G" "$G/configs/c.1" "$F" 2>&1
    [ -d "$ACM" ] && $BB ls -la "$ACM" 2>&1
    echo "UDC=[$($BB cat "$G/UDC" 2>/dev/null)] state=[$(udc_state)] idProduct=$($BB cat "$G/idProduct" 2>/dev/null) serial=${SERIAL:-off}"
    for l in "$F"/lun.*; do
      echo "$l file=[$($BB cat "$l/file" 2>/dev/null)] ro=$($BB cat "$l/ro" 2>/dev/null) inquiry=[$($BB cat "$l/inquiry_string" 2>/dev/null)]"
    done
    echo "--- dmesg tail ---"
    $BB dmesg | $BB tail -n 120
    if [ "$HOST" = "darkos" ]; then
      echo "--- display ---"
      echo "fb0: [$($BB cat $FB/virtual_size 2>/dev/null) $($BB cat $FB/bits_per_pixel 2>/dev/null) $($BB cat $FB/stride 2>/dev/null)] screens=${FB_ON:-off}"
      for c in /sys/class/drm/card*-*; do
        [ -e "$c/status" ] && echo "${c##*/}: status=$($BB cat "$c/status") enabled=$($BB cat "$c/enabled") dpms=$($BB cat "$c/dpms")"
      done
      $BB dmesg | $BB grep -i -E 'drm|dsi|vop|panel|logo|backlight|fb0|iommu' | $BB head -n 60
    fi
  } >> "$LOG" 2>&1
  if $BB mount -o remount,rw /flash; then
    $BB cp "$LOG" "$SRC/r36s-usb.log"
    $BB sync
    $BB mount -o remount,ro /flash
  fi
}

cleanup() {
  [ -n "$CLOCK_PID" ] && kill "$CLOCK_PID" 2>/dev/null
  [ -n "$CONSOLE_PID" ] && kill "$CONSOLE_PID" 2>/dev/null
  echo "" > "$G/UDC" 2>/dev/null
  for l in "$F"/lun.*; do
    echo "" > "$l/file" 2>/dev/null
  done
  set_read_ahead "$TF1" "$RA1"
  set_read_ahead "$TF2" "$RA2"
  [ -n "$OLD_GOV" ] && set_governor "$OLD_GOV"
  $BB sync
}

power_off() {
  msg "$1"
  $BB sync
  $BB sleep 2
  [ -n "$FB_ON" ] && $BB sleep 3
  $BB poweroff -f 2>/dev/null
  echo o > /proc/sysrq-trigger 2>/dev/null
  $BB sleep 5
  msg "Hold the power button to turn the R36S off."
  while :; do $BB sleep 60; done
}

# Errors before USB mode starts: exit non-zero. On EMUELEC post-flash.sh then falls back to the previous helper;
# on the dArkOS card it powers off.
fail() {
  msg "ERROR: $*"
  screen error
  cleanup
  save_log
  $BB sleep 10
  exit 1
}

# $1 = LUN number, $2 = device, $3 = SCSI inquiry text: vendor (8) + product (16) + revision (4) characters.
add_lun() {
  $BB mkdir -p "$F/lun.$1" 2>>"$LOG"
  echo 1 > "$F/lun.$1/removable"
  echo 0 > "$F/lun.$1/ro"
  # Windows marks almost every write Force Unit Access, which made the card flush after each one (about
  # 10 MB/s and 4 small files a second). The kernel writes back within ~30 s, and everything is synced at unplug.
  echo 1 > "$F/lun.$1/nofua" 2>>"$LOG"
  echo "$3" > "$F/lun.$1/inquiry_string" 2>>"$LOG"
  echo "$2" > "$F/lun.$1/file" 2>>"$LOG" || fail "could not share $2"
}

# $1 = a busybox, then the applets it must have.
has_applets() {
  list=" $($1 --list 2>/dev/null | $BB tr '\n' ' ') "
  shift
  for a; do
    case $list in *" $a "*) ;; *) return 1 ;; esac
  done
}

# dArkOS card only, unless usbmode/no-serial (or no-serial.txt) is on BOOT: a USB serial port (CDC ACM) next to the card
# drives, for r36s-console.py on the PC. Windows needs the IAD device class (EF/02/01) for a composite device, and it gets
# a product ID of its own (Linux Foundation 0x0106, "ACM + mass storage") so Windows doesn't reuse what it set up for the
# card reader alone (0x0104). The configfs link is made with the static busybox's ln, from inside the gadget folder.
serial_setup() {
  [ "$HOST" = "darkos" ] || return 1
  if [ -e "$SRC/no-serial" ] || [ -e "$SRC/no-serial.txt" ]; then
    msg "Serial console off: usbmode/no-serial is on BOOT"
    return 1
  fi
  if [ "$XB" = "$BB" ] || ! has_applets "$XB" ln rmdir stty base64 gzip; then
    msg "Serial console unavailable: it needs busybox-r36s"
    return 1
  fi
  if ! $BB mkdir "$ACM" 2>>"$LOG"; then
    msg "Serial console unavailable: no USB serial function"
    return 1
  fi
  TTY=/dev/ttyGS$($BB cat "$ACM/port_num" 2>/dev/null || echo 0)
  if ! (cd "$G" && $XB ln -s functions/acm.gs0 configs/c.1/acm.gs0) 2>>"$LOG"; then
    $XB rmdir "$ACM" 2>>"$LOG"
    msg "Serial console unavailable: could not link the USB serial function"
    return 1
  fi
  echo 0x0106 > "$G/idProduct"
  echo 0xEF > "$G/bDeviceClass"
  echo 0x02 > "$G/bDeviceSubClass"
  echo 0x01 > "$G/bDeviceProtocol"
  echo "Card reader + serial" > "$G/configs/c.1/strings/0x409/configuration"
  SERIAL=on
  msg "Serial console set up (USB 1d6b:0106, $TTY)"
}

# Back to the card reader alone: today's device, 1d6b:0104. Leaves the gadget unbound.
serial_remove() {
  echo "" > "$G/UDC" 2>/dev/null
  $BB rm -f "$G/configs/c.1/acm.gs0" 2>>"$LOG"
  $XB rmdir "$ACM" 2>>"$LOG"
  echo 0x0104 > "$G/idProduct"
  echo 0x00 > "$G/bDeviceClass"
  echo 0x00 > "$G/bDeviceSubClass"
  echo 0x00 > "$G/bDeviceProtocol"
  echo "Mass storage" > "$G/configs/c.1/strings/0x409/configuration"
  SERIAL=
}

# Starts USB device mode; if that fails with the serial port, tries again with the card reader alone. (stderr first, so
# the kernel's reason for a failed bind goes to the log.)
start_udc() {
  echo "$UDC" 2>>"$LOG" > "$G/UDC" && return 0
  [ -n "$SERIAL" ] || return 1
  msg "USB device mode with the serial port failed: card reader only"
  serial_remove
  echo "$UDC" 2>>"$LOG" > "$G/UDC"
}

# Waits up to PC_WAIT seconds for the PC to configure the device; on the dArkOS card a PC that turns up gets at least
# 45 s from then. If the PC doesn't configure the device with the serial port within 15 s, it gets the card reader
# alone (and at least 30 s more).
wait_for_pc() {
  i=0
  held=0
  limit=$PC_WAIT
  st=
  while [ $i -lt $limit ]; do
    st=$(udc_state)
    [ "$st" = "configured" ] && return 0
    [ "$HOST" = "emuelec" ] && [ $i -ge 3 ] && [ "$st" = "not attached" ] && return 1
    case $st in
      "not attached" | "") held=0 ;;
      *)
        held=$((held + 1))
        [ $held -eq 1 ] && [ "$HOST" = "darkos" ] && [ $limit -lt $((i + 45)) ] && limit=$((i + 45))
        ;;
    esac
    if [ -n "$SERIAL" ] && [ $held -ge 15 ]; then
      msg "The PC didn't set up the USB serial port (USB state: $st): card reader only"
      serial_remove
      echo "$UDC" 2>>"$LOG" > "$G/UDC" || fail "could not restart USB device mode"
      held=0
      [ $limit -lt $((i + 30)) ] && limit=$((i + 30))
    fi
    $BB sleep 1
    i=$((i + 1))
  done
  return 1
}

# The serial console. Each command is one line, "@@<id> <length> <command>"; one whose command doesn't have that length
# (cut off or garbled) is answered "@@X <id> length" and not run. Otherwise the answer is "@@B <id>", the output, then
# "@@E <id> <exit code>" after a newline. Commands run as root in a subshell, with stdin from /dev/null so they can't
# swallow the next command.
console_session() {
  while IFS= read -r line; do
    line=${line%"$CR"}
    case $line in
      @@?*" "*" "*) ;;
      *) continue ;;
    esac
    id=${line%% *}
    id=${id#@@}
    rest=${line#* }
    cmd=${rest#* }
    if [ "${#cmd}" != "${rest%% *}" ]; then
      echo "@@X $id length"
      continue
    fi
    echo "@@B $id"
    (eval "$cmd") < /dev/null 2>&1
    rc=$?
    echo ""
    echo "@@E $id $rc"
  done
}

# Plain commands in the console: the initramfs busybox's applets (the shell prefers them), then busybox-r36s's (ln,
# stty, wc...).
console_tools() {
  $BB mkdir -p /tmp/r36s-bin && $XB --install -s /tmp/r36s-bin 2>>"$LOG"
  PATH=$PATH:/tmp/r36s-bin
}

# Runs the console on the USB serial port, and again after each USB reset (the port hangs up then).
console_loop() {
  console_tools
  while :; do
    if [ -c "$TTY" ]; then
      ($XB stty raw -echo 2>>"$LOG"; console_session) < "$TTY" > "$TTY" 2>&1
    fi
    $BB sleep 1
  done
}

# For r36s-console.py: the helper's state, one "name value" per line.
r36s_status() {
  echo "helper $HELPER_VERSION"
  echo "host $HOST"
  echo "usb $(udc_state)"
  echo "serial ${SERIAL:-off}"
  echo "screens ${FB_ON:-off}"
  echo "boot.ini ${R36S_BOOTINI:-not set}"
  for l in "$F"/lun.*; do
    echo "share ${l##*/} $($BB cat "$l/file" 2>/dev/null)"
  done
}

# For r36s-console.py shot: fb0's format, the display's state, then a copy of fb0, gzipped and base64-encoded.
r36s_shot() {
  echo "geometry $($BB cat $FB/virtual_size) $($BB cat $FB/bits_per_pixel) $($BB cat $FB/stride)"
  for c in /sys/class/drm/card*-*; do
    [ -e "$c/status" ] && echo "display ${c##*/} $($BB cat "$c/status") $($BB cat "$c/enabled") $($BB cat "$c/dpms")"
  done
  $BB cat /dev/fb0 > /tmp/r36s-shot || return 1
  echo "md5 $($BB md5sum < /tmp/r36s-shot | $BB cut -d' ' -f1)"
  echo "data"
  $XB gzip -c /tmp/r36s-shot | $XB base64
  rc=$?
  $BB rm -f /tmp/r36s-shot
  return $rc
}

# For r36s-console.py: asks the helper to restart the R36S, but only once the PC has ejected both cards (each LUN's
# backing file is closed then), so nothing the PC wrote can be lost. rearm restarts at once, into card mode again. exit
# ("unplug") restarts into dArkOS only once the cable is out: dArkOS powers the OTG port itself, so it mustn't start with
# the PC still connected. "force" skips the eject check.
r36s_reboot() {
  when=now
  [ "$1" = "unplug" ] && when=unplug && shift
  if [ "$1" != "force" ]; then
    for l in "$F"/lun.*; do
      if [ -n "$($BB cat "$l/file" 2>/dev/null)" ]; then
        echo "Not restarting: $($BB cat "$l/file") is still shared. Eject both cards on the PC first."
        return 1
      fi
    done
  fi
  if [ "$when" = "unplug" ]; then
    : > "$RESTART_UNPLUG"
    echo "Unplug the cable now: the R36S then restarts by itself."
  else
    : > "$RESTART"
    echo "Restarting in a moment."
  fi
}

# Waits until the cable has been unplugged for 10 s (a USB reset through a hub can drop the link for a few seconds).
# Returns 1 instead when r36s_reboot asks for a restart.
wait_for_unplug() {
  gone=0
  while [ $gone -lt 10 ]; do
    [ -e "$RESTART" ] && return 1
    if [ "$(udc_state)" = "not attached" ]; then gone=$((gone + 1)); else gone=0; fi
    $BB sleep 1
  done
}

# A restart asked over the serial console, after the PC ejected both cards. Writes nothing to the cards: once the PC has
# had BOOT, this side's view of it is stale, so r36s-console.py saves the log first.
restart() {
  msg "Restarting, as asked over the serial console"
  cleanup
  $BB sleep 1
  $BB reboot -f
  $BB sleep 5
  power_off "The restart didn't happen - powering off. Turn the R36S on to play."
}

# After the PC: once the cable is out, power off as before, or restart if r36s-console.py exit asked for it. A restart
# asked by rearm happens at once.
finish() {
  if wait_for_unplug; then
    msg "Cable unplugged"
    [ -e "$RESTART_UNPLUG" ] && restart
    cleanup
    screen off
    power_off "$DONE_MSG"
  fi
  restart
}

msg "R36S: checking for a PC on the OTG port..."
FLASHDEV=$($BB awk '$2=="/flash"{print $1}' /proc/mounts)
TF1=${FLASHDEV%p*}
[ -b "$TF1" ] || TF1=/dev/mmcblk1
TF2=
for b in /sys/block/mmcblk[0-9]; do
  d=/dev/${b##*/}
  [ "$d" = "$TF1" ] && continue
  [ -b "$d" ] || continue
  if [ "$($BB cat "$b/device/type" 2>/dev/null)" = "SD" ]; then
    TF2=$d
    break
  fi
done
host_setup
msg "TF1=$TF1 ($($BB cat /sys/block/${TF1#/dev/}/size 2>/dev/null) sectors) TF2=${TF2:-empty} udc=$UDC"
[ "$HOST" = "darkos" ] && msg "Started from the dArkOS card ($FLASHDEV, label ${FLASH_LABEL:-unknown}); boot.ini: ${R36S_BOOTINI:-not set}"
setup_screens && screen connect
msg "RTC: $(rtc_text)"

if setup_clock_tools; then
  HAVE_XB=1
  msg "Clock sync ready ($XB)"
else
  HAVE_XB=
  msg "Clock sync unavailable (details in $SRC/r36s-usb.log)"
fi

OLD_GOV=$($BB cat "$CPUFREQ/scaling_governor" 2>/dev/null)
RA1=$(read_ahead "$TF1")
[ -n "$TF2" ] && RA2=$(read_ahead "$TF2")
set_governor performance
set_read_ahead "$TF1" 2048
set_read_ahead "$TF2" 2048

$BB mount -t configfs none /sys/kernel/config 2>>"$LOG"
$BB mkdir -p "$G/strings/0x409" "$G/configs/c.1/strings/0x409" "$F" 2>>"$LOG"
echo 16 > "$F/num_buffers" 2>>"$LOG"
echo 0x1d6b > "$G/idVendor"
echo 0x0104 > "$G/idProduct"
echo 0x0100 > "$G/bcdDevice"
echo 0x0200 > "$G/bcdUSB"
echo "R36S" > "$G/strings/0x409/manufacturer"
echo "R36S card slots (read-write)" > "$G/strings/0x409/product"
echo "R36S0002" > "$G/strings/0x409/serialnumber"
echo "Mass storage" > "$G/configs/c.1/strings/0x409/configuration"
echo 500 > "$G/configs/c.1/MaxPower"
echo 1 > "$F/stall"
if [ -n "$TF2" ]; then
  add_lun 0 "$TF2" "R36S    TF2 card        1.00"
  add_lun 1 "$TF1" "R36S    TF1 boot card   1.00"
else
  add_lun 0 "$TF1" "R36S    TF1 boot card   1.00"
fi

( cd "$G" && $BB tar -xf "$SRC/r36s-link.tar" ) >> "$LOG" 2>&1
[ -L "$G/configs/c.1/mass_storage.0" ] || fail "could not link the USB function"
msg "function linked"
serial_setup
# Last write to the boot card before the PC can change it.
save_log

start_udc || fail "could not start USB device mode"
msg "USB device mode on, state: $(udc_state)"
[ "$HOST" = "darkos" ] && [ "$(udc_state)" != "configured" ] &&
  msg "Connect the OTG port to the PC now (straight into the PC, not a hub). Waiting up to $PC_WAIT seconds..."

wait_for_pc
msg "USB state after ${i}s: $st"

if [ "$st" = "configured" ]; then
  screen active
  if [ -n "$TF2" ]; then
    msg "R36S USB CARD MODE: slot TF2 and slot TF1 are shared read-write"
  else
    msg "R36S USB CARD MODE: slot TF1 is shared read-write (slot TF2 is empty)"
  fi
  if [ -n "$HAVE_XB" ]; then
    clock_watch &
    CLOCK_PID=$!
  fi
  if [ -n "$SERIAL" ]; then
    console_loop &
    CONSOLE_PID=$!
    msg "Serial console on $TTY"
  fi
  msg "Use Safely Remove on the PC, then unplug the cable."
  finish
fi

cleanup
save_log
screen nopc
power_off "$NOPC_MSG"
