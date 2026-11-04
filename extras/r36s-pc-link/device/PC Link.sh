#!/bin/bash
# R36S PC Link (Options > Tools): lets the PC view and control dArkOS while it runs, with r36s-remote.py on the PC,
# over the OTG USB cable or over Wi-Fi. The link keeps running (systemd service "pclink") after you go back to the
# games; open this again to stop it. Over USB the R36S switches its OTG port's 5 V off before it asks for the cable,
# and leaves it off after the link stops, until "OTG power on" here or a restart (a Wi-Fi dongle, USB drive or
# controller on that port needs it). The link itself is .pclink/pclinkd.sh; setup and details are in README.md in
# the r36s-pc-link folder on the PC.
HERE=$(cd "$(dirname "$0")" && pwd)
D="$HERE/.pclink/pclinkd.sh"
TTY=/dev/tty1
height=17
width=56

sudo chmod 666 $TTY
export TERM=linux
printf "\033c\e[?25l" > $TTY
if [[ ! -e "/dev/input/by-path/platform-odroidgo2-joypad-event-joystick" ]]; then
  sudo setfont /usr/share/consolefonts/Lat7-TerminusBold22x11.psf.gz
else
  sudo setfont /usr/share/consolefonts/Lat7-Terminus16.psf.gz
fi

ExitMenu() {
  printf "\033c" > $TTY
  pgrep -f gptokeyb | sudo xargs -r kill -9
  sudo setfont /usr/share/consolefonts/Lat7-Terminus20x10.psf.gz
  exit 0
}

refresh() {
  STATUS=$(sudo -n /bin/bash "$D" status 2>&1)
}

value() {
  sed -n "s/^$1=//p" <<< "$STATUS" | head -n 1
}

say() {
  dialog --title " $1 " --no-collapse --msgbox "$2" $height $width > $TTY
}

summary() {
  local line
  if [ "$(value service)" = active ]; then
    case $(value state) in
      connected) line="Link: USB, connected (the PC is $(value pc))" ;;
      ready) if [ "$(value mode)" = usb ]; then line="Link: USB, waiting for the PC"; else line="Link: Wi-Fi, $(value ip) port $(value port)"; fi ;;
      *) line="Link: $(value state)" ;;
    esac
  elif [ "$(value state)" = failed ]; then
    line="Link: off. Last start failed:\n$(value message)"
  else
    line="Link: off"
  fi
  printf '%s\nOTG power: %s' "$line" "$(value otg_power_now)"
  [ "$(value service)" = active ] && [ -n "$(value warning)" ] && printf '\n%s' "$(value warning)"
}

WaitForPc() {
  local msg
  for _ in $(seq 1 90); do
    refresh
    case $(value state) in
      connected)
        say "Connected" "The PC is connected$([ -n "$(value pc)" ] && printf ' (it is %s)' "$(value pc)").\n\nOn the PC:  r36s-remote status\n\nThe link stays on when you go back to the games.\nTo stop it, open PC Link again."
        return
        ;;
      failed | stopped)
        say "PC Link stopped" "$(value message)"
        return
        ;;
    esac
    # Ask for the cable only while the R36S is a USB device with its own 5 V confirmed off.
    if [ "$(value mode)" != usb ] || [ "$(value otg_power_now)" != off ]; then
      say "Don't connect the cable" "PC Link isn't ready for the USB cable\n(link: $(value mode), OTG power: $(value otg_power_now)).\n\n$(value message)\n\nStop the link before connecting anything."
      return
    fi
    msg="Connect the OTG port to the PC now: straight\ninto a laptop USB-C port, not a hub.\n\nThe R36S is $(value ip). On the PC:\n  r36s-remote wait\n\nUSB: $(value udc)\n$(value warning)\n\nPress A to go back: the link stays on."
    dialog --title " Waiting for the PC " --no-collapse --timeout 2 --msgbox "$msg" $height $width > $TTY && return
  done
}

StartUsb() {
  local out
  dialog --title " PC Link over USB " --no-collapse --yesno "Unplug the cable from the OTG port first:\nright now dArkOS powers that port.\n\nThe R36S then switches the port's 5 V off\nand turns it into a USB network device for the PC.\nThe games keep running.\n\nIs the OTG port empty?" $height $width > $TTY || return
  dialog --infobox "\nStarting PC Link..." 5 $width > $TTY
  out=$(sudo -n /bin/bash "$D" start usb 2>&1) || {
    say "PC Link didn't start" "$(tail -n 9 <<< "$out")"
    return
  }
  WaitForPc
}

StartWifi() {
  local out
  dialog --infobox "\nStarting PC Link over Wi-Fi..." 5 $width > $TTY
  out=$(sudo -n /bin/bash "$D" start wifi 2>&1) || {
    say "PC Link didn't start" "$(tail -n 9 <<< "$out")"
    return
  }
  STATUS=$out
  say "PC Link over Wi-Fi" "The R36S is at $(value ip), port $(value port).\n\nOn the PC:\n  r36s-remote --host <address> status\n\nThe link stays on when you go back to the games.\nTo stop it, open PC Link again."
}

StopLink() {
  local out
  dialog --infobox "\nStopping PC Link..." 5 $width > $TTY
  if out=$(sudo -n /bin/bash "$D" stop 2>&1); then
    STATUS=$out
    say "PC Link is off" "$(value message)\n\nUnplug the cable before you turn OTG power on."
  else
    say "PC Link didn't stop cleanly" "$(tail -n 9 <<< "$out")\n\nUnplug the cable, then restart the R36S."
  fi
}

PowerOn() {
  local out
  dialog --title " OTG power on " --no-collapse --yesno "Unplug the cable from the OTG port first.\n\nThe R36S then checks that no 5 V comes in on the\nport and powers it again (for a Wi-Fi dongle,\nUSB drive or controller). If it can't check,\nrestart the R36S instead (cable unplugged).\n\nIs the OTG port empty?" $height $width > $TTY || return
  out=$(sudo -n /bin/bash "$D" vbus-on 2>&1)
  say "OTG power" "$out"
}

ShowLog() {
  {
    echo "== Now"
    sudo -n /bin/bash "$D" diag 2>&1
    echo
    echo "== PC Link (pclinkd.log)"
    sudo -n tail -n 40 /run/pclink/pclinkd.log 2> /dev/null || echo "No PC Link log since the last restart."
    echo
    echo "== SSH server (sshd.log)"
    sudo -n tail -n 30 /run/pclink/sshd.log 2> /dev/null || echo "No SSH server log since the last restart."
  } | fold -s -w $((width - 4)) > /tmp/pclink-log.txt
  dialog --title " PC Link log " --textbox /tmp/pclink-log.txt $height $width > $TTY
}

MainMenu() {
  local choice
  while true; do
    refresh
    choice=$(dialog --title " R36S PC Link " --no-collapse --cancel-label "Back" --menu "$(summary)" $height $width 6 \
      1 "Start over the USB cable" \
      2 "Start over Wi-Fi" \
      3 "Stop the link" \
      4 "OTG power on" \
      5 "Show the log" \
      6 "Back to the games" 2>&1 > $TTY) || ExitMenu
    case $choice in
      1) StartUsb ;;
      2) StartWifi ;;
      3) StopLink ;;
      4) PowerOn ;;
      5) ShowLog ;;
      *) ExitMenu ;;
    esac
  done
}

sudo chmod 666 /dev/uinput
export SDL_GAMECONTROLLERCONFIG_FILE="/opt/inttools/gamecontrollerdb.txt"
pgrep -f gptokeyb | sudo xargs -r kill -9
/opt/inttools/gptokeyb -1 "PC Link.sh" -c "/opt/inttools/keys.gptk" > /dev/null 2>&1 &
trap ExitMenu EXIT
printf "\033c" > $TTY
[ -x "$D" ] || [ -f "$D" ] || {
  say "PC Link" "PC Link's files are missing ($D).\nRun install-pc-link.py on the PC."
  exit 1
}
MainMenu
