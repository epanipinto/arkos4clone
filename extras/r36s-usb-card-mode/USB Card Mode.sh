#!/bin/bash
# R36S: restart once into USB card mode from the dArkOS card, so the PC can reach both cards without swapping them.
# The PC sees "R36S TF1 boot card" (this dArkOS card) and "R36S TF2 card" (the games card) as USB drives. Use Safely
# Remove on the PC and unplug: the R36S powers off, and the next power-on starts dArkOS again. Reusable.
# How: for one boot, boot.ini becomes the card-mode part of /boot/boot.ini.usbmode followed by the current dArkOS
# boot.ini (as U-Boot's fallback). U-Boot starts EmuELEC's kernel from /boot/usbmode, whose /boot/post-flash.sh
# copies boot.ini.darkos back over boot.ini before it shares the cards. If the R36S ever stays on a black screen,
# see USB-CARD-MODE-RECOVERY.txt on the BOOT partition.
BOOT=/boot
DIR=$BOOT/usbmode
MARK=R36S-USB-CARD-MODE
NEW=/tmp/boot.ini.usbmode
sudo chmod 666 /dev/tty1
exec > /dev/tty1 2>&1
printf "\033c"

fail() {
  printf "\n\e[91m%s\n\033[0m" "$1"
  sleep 8
  printf "\033c"
  exit 1
}

# dArkOS's boot.ini: U-Boot's header on line 1, a booti command, and no card-mode marker.
is_darkos_ini() {
  [ -s "$1" ] || return 1
  head -n 1 "$1" | tr -d '\r' | grep -qix 'odroidgoa-uboot-config' || return 1
  grep -q '^booti ' "$1" && ! grep -q "$MARK" "$1"
}

mountpoint -q "$BOOT" || fail "The BOOT partition is not mounted on $BOOT."
[ -s "$DIR/usbmode.md5" ] && [ -s "$BOOT/boot.ini.usbmode" ] && [ -s "$BOOT/post-flash.sh" ] ||
  fail "USB card mode is not installed on this card ($DIR is missing)."
printf "Checking the USB card mode files...\n"
(cd "$DIR" && md5sum -c --quiet usbmode.md5 > /dev/null 2>&1) ||
  fail "USB card mode files in $BOOT are damaged or missing (usbmode.md5). Nothing was changed."

if is_darkos_ini "$BOOT/boot.ini"; then
  sudo cp -f "$BOOT/boot.ini" "$BOOT/boot.ini.darkos" && sync || fail "Could not save boot.ini.darkos. Nothing was changed."
elif grep -q "$MARK" "$BOOT/boot.ini" 2> /dev/null; then
  printf "USB card mode didn't start last time (dArkOS\nstarted instead). Trying again.\n"
else
  fail "$BOOT/boot.ini isn't dArkOS's boot.ini. Nothing was changed."
fi

SAVED=
for f in "$BOOT/boot.ini.darkos" "$BOOT/boot.ini.darkos-original"; do
  if is_darkos_ini "$f"; then
    SAVED=$f
    break
  fi
done
[ -n "$SAVED" ] || fail "No saved dArkOS boot.ini (boot.ini.darkos) to come back to. Nothing was changed."

# Card-mode part of the template, then the dArkOS boot it falls back to (and that post-flash.sh restores).
{
  sed -n "1,/^# $MARK-FALLBACK/p" "$BOOT/boot.ini.usbmode"
  sed '1d' "$SAVED"
} > "$NEW" || fail "Could not write $NEW. Nothing was changed."
if [ "$(grep -c "^# $MARK-FALLBACK" "$NEW")" != 1 ] || ! grep -q 'booti 0x02000000 - 0x01f00000' "$NEW" ||
  ! grep -q '^booti ' "$NEW" || ! head -n 1 "$NEW" | grep -qix 'odroidgoa-uboot-config'; then
  fail "The card-mode boot.ini came out wrong. Nothing was changed."
fi

sudo cp -f "$NEW" "$BOOT/boot.ini.new" && sync && sudo mv -f "$BOOT/boot.ini.new" "$BOOT/boot.ini" && sync
if ! cmp -s "$NEW" "$BOOT/boot.ini"; then
  sudo cp -f "$SAVED" "$BOOT/boot.ini"
  sync
  fail "Could not write $BOOT/boot.ini; dArkOS's boot.ini was put back."
fi

printf "\n\e[32mRestarting into USB card mode - connect the OTG port to the PC\033[0m\n\n"
printf "Plug the cable straight into the PC (not a hub)\nwhen the R36S asks; it waits 90 seconds.\n"
printf "When done, Safely Remove both R36S drives on the\nPC and unplug: the R36S powers off.\nTurn it on to play.\n"
sleep 8
sudo systemctl reboot || sudo reboot
