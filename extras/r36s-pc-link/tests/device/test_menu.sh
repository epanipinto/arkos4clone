#!/bin/bash
# Offline tests for "PC Link.sh", the Tools menu: a fake dialog answers from a script, a fake sudo answers pclinkd.sh's
# status from a file, and gptokeyb is /bin/true. Run as root in the dArkOS4Clone root file system (chroot-run.sh) or its
# Debian 13 stand-in (standin-run.sh), with the PC Link folder at /w.
W=/w
PASS=0
FAIL=0
ok() {
  PASS=$((PASS + 1))
  echo "  ok: $*"
}
bad() {
  FAIL=$((FAIL + 1))
  echo "  FAIL: $*"
}
check() { if [ "$1" = "$2" ]; then ok "$3"; else bad "$3: got [$1] want [$2]"; fi; }
has() { if grep -qF -- "$2" "$1"; then ok "$3"; else
  bad "$3: [$2] not in $1"
  sed 's/^/    | /' "$1" | tail -n 15
fi; }
hasnot() { if grep -qF -- "$2" "$1"; then bad "$3: [$2] found in $1"; else ok "$3"; fi; }

export MENU_TEST=/tmp/menu
M=$MENU_TEST
CARD=/tmp/menucard/tools
rm -rf $M /tmp/menucard
mkdir -p $M/bin $CARD
cp -r $W/device/.pclink "$W/device/PC Link.sh" $CARD/

# dialog: logs each call's arguments (dialog.N, one per line) and answers from $M/script, one line per dialog that
# waits for a key: a menu tag, yes, no, ok, cancel, or enter (a stray Enter: the default item, or the default button).
cat > $M/bin/dialog << 'EOF'
#!/bin/bash
M=$MENU_TEST
n=$(($(cat $M/n 2> /dev/null || echo 0) + 1))
echo $n > $M/n
printf '%s\n' "$@" > $M/dialog.$n
kind= default= defaultno= first= args=("$@")
for i in "${!args[@]}"; do
  case ${args[$i]} in
    --menu) kind=menu first=${args[$((i + 5))]} ;;
    --yesno | --msgbox | --infobox) kind=${args[$i]#--} ;;
    --textbox) kind=textbox && cp "${args[$((i + 1))]}" $M/textbox.$n ;;
    --default-item) default=${args[$((i + 1))]} ;;
    --defaultno) defaultno=1 ;;
  esac
done
echo "$n $kind" >> $M/calls
[ "$kind" = infobox ] && exit 0
answer=$(head -n 1 $M/script 2> /dev/null)
sed -i 1d $M/script 2> /dev/null
case $kind:$answer in
  menu:enter) echo "${default:-$first}" >&2 ;;
  menu:[0-9]*) echo "$answer" >&2 ;;
  yesno:enter) [ -z "$defaultno" ] ;;
  yesno:yes | msgbox:ok | msgbox:enter | textbox:ok | textbox:enter) exit 0 ;;
  *) exit 1 ;;
esac
EOF

# sudo: pclinkd.sh's answers come from $M/status ($M/status.after replaces it when the link starts or stops); the
# rest (chmod, setfont, kill) is only logged.
cat > $M/bin/sudo << 'EOF'
#!/bin/bash
M=$MENU_TEST
[ "$1" = -n ] && shift
echo "sudo $*" >> $M/calls
case $* in
  "/bin/bash "*" status") cat $M/status ;;
  "/bin/bash "*" diag") echo "Service: active, usb link connected" ;;
  "/bin/bash "*" start "* | "/bin/bash "*" stop" | "/bin/bash "*" vbus-on")
    [ -f $M/status.after ] && mv $M/status.after $M/status
    cat $M/status
    ;;
  "tail -n 40 /run/pclink/pclinkd.log") echo "a pclinkd.log line" ;;
  "tail -n 30 /run/pclink/sshd.log") echo "an sshd.log line" ;;
esac
exit 0
EOF
chmod 755 $M/bin/dialog $M/bin/sudo

ON_USB=$'service=active\nmode=usb\nstate=connected\npc=10.36.36.13\nip=10.36.36.1\nport=2222\nudc=configured\notg_power_now=off\nfive_volt_in=yes'
OFF_POWERED=$'service=inactive\notg_power_now=on\nfive_volt_in=no'
OFF_AFTER_USB=$'service=inactive\nmode=usb\nstate=stopped\nmessage=Link off.\notg_power_now=off\nfive_volt_in=no'

# menu <status> <answers...>: runs the menu once; the answers run out with a cancel, which leaves the menu. With AFTER
# set, that status replaces <status> once the menu starts or stops the link.
menu() {
  rm -f $M/n $M/calls $M/dialog.* $M/textbox.* $M/status.after
  printf '%s\n' "$1" > $M/status
  [ -n "$AFTER" ] && printf '%s\n' "$AFTER" > $M/status.after
  shift
  printf '%s\n' "$@" > $M/script
  PATH=$M/bin:$PATH PCLINK_TTY=$M/tty PCLINK_GPTOKEYB=/bin/true timeout 60 bash "$CARD/PC Link.sh" > $M/out 2>&1
  echo "exit $?" >> $M/out
}
kinds() { awk '$1 ~ /^[0-9]+$/ { print $2 }' $M/calls | paste -sd ' ' -; }
nth() { awk -v k="$1" -v n="${2:-1}" '$1 ~ /^[0-9]+$/ && $2 == k && ++c == n { print $1 }' $M/calls; }
calls() { grep '^sudo ' $M/calls > $M/sudo-calls; }

echo "== M0: file format"
f="$W/device/PC Link.sh"
if bash -n "$f"; then ok "bash -n"; else bad "bash -n"; fi
check "$(tr -cd '\r' < "$f" | wc -c)" "0" "LF line endings"
check "$(LC_ALL=C grep -c '[^ -~	]' "$f")" "0" "ASCII only"

echo "== M1: Start over the USB cable while the link is on: says so, no question about the port, nothing started"
menu "$ON_USB" 1 ok
check "$(kinds)" "menu msgbox menu" "menu, a message, back to the menu"
has $M/dialog.2 "PC Link is already on over the USB cable." "says it's on"
has $M/dialog.2 "Link: USB, connected (the PC is 10.36.36.13)" "and how"
calls
hasnot $M/sudo-calls "start usb" "nothing started"
grep -h . $M/dialog.* > $M/all-dialogs
hasnot $M/all-dialogs "right now dArkOS powers that port" "no word of dArkOS powering the port"
check "$(sed -n '/^--default-item$/{n;p}' $M/dialog.3)" "1" "the menu comes back on the same item"
has $M/out "exit 0" "the menu ends cleanly"

echo "== M2: link off, OTG port powered: the question says so, and defaults to No"
menu "$OFF_POWERED" 1 no
check "$(kinds)" "menu yesno menu" "menu, the question, back to the menu"
has $M/dialog.2 "right now dArkOS powers that port." "says dArkOS powers the port"
has $M/dialog.2 "--defaultno" "No is the default"
calls
hasnot $M/sudo-calls "start usb" "No: nothing started"

echo "== M3: link off, OTG power still off from a session: the question doesn't claim the port is powered"
menu "$OFF_AFTER_USB" 1 no
has $M/dialog.2 "The OTG port's 5 V is still off from the last" "says the 5 V is off"
hasnot $M/dialog.2 "right now dArkOS powers that port" "no claim that dArkOS powers it"

echo "== M4: a stray Enter on the question answers No"
menu "$OFF_POWERED" 1 enter
check "$(kinds)" "menu yesno menu" "back to the menu"
calls
hasnot $M/sudo-calls "start usb" "nothing started"

echo "== M5: Yes starts the link and waits for the PC"
AFTER="$ON_USB" menu "$OFF_POWERED" 1 yes ok
check "$(kinds)" "menu yesno infobox msgbox menu" "question, starting, connected, back to the menu"
calls
has $M/sudo-calls "start usb" "started over USB"
has "$M/dialog.$(nth msgbox)" "The PC is connected (it is 10.36.36.13)." "says the PC is connected"

echo "== M6: Show the log, then a stray Enter: the log again, never Start"
menu "$ON_USB" 5 ok enter ok
check "$(kinds)" "menu textbox menu textbox menu" "log, menu on Show the log, log again"
check "$(sed -n '/^--default-item$/{n;p}' $M/dialog.3)" "5" "the menu comes back on Show the log"
has $M/textbox.2 "== Now" "log: the link now"
has $M/textbox.2 "Service: active, usb link connected" "log: pclinkd.sh diag"
has $M/textbox.2 "a pclinkd.log line" "log: pclinkd.log"
has $M/textbox.2 "== SSH server (sshd.log)" "log: the SSH server's section"
has $M/textbox.2 "an sshd.log line" "log: sshd.log"
calls
hasnot $M/sudo-calls "start usb" "nothing started"

echo "== M7: Start over Wi-Fi while the USB link is on: stop it first"
menu "$ON_USB" 2 ok
has $M/dialog.2 "PC Link is on over the USB cable." "says which link is on"
has $M/dialog.2 "Stop the link before starting it over Wi-Fi." "and what to do"
calls
hasnot $M/sudo-calls "start wifi" "nothing started"

echo "== M8: OTG power on: refused while the link runs, and the question defaults to No"
menu "$ON_USB" 4 ok
has $M/dialog.2 "PC Link is on: stop the link first." "refused while on"
calls
hasnot $M/sudo-calls "vbus-on" "OTG power left alone"
menu "$OFF_AFTER_USB" 4 enter
has $M/dialog.2 "--defaultno" "the question defaults to No"
calls
hasnot $M/sudo-calls "vbus-on" "a stray Enter answers No"

echo
echo "passed $PASS, failed $FAIL"
[ "$FAIL" -eq 0 ]
