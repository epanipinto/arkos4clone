#!/bin/bash
# Offline tests for pclinkd.sh (R36S PC Link), as root in the dArkOS4Clone root file system (arm64 chroot; see
# chroot-run.sh) or its Debian 13 stand-in (standin-run.sh), on fake hardware (fakehw.sh) with the real ip, dnsmasq,
# sshd, ssh and sudo.
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
  sed 's/^/    | /' "$1" | tail -n 25
  return 1
fi; }
hasnot() { if grep -qF -- "$2" "$1"; then bad "$3: [$2] found in $1"; else ok "$3"; fi; }
waitfor() {
  local n=$(($1 * 10))
  shift
  for _ in $(seq 1 $n); do
    "$@" && return 0
    sleep 0.1
  done
  return 1
}
reg() { echo $(($(cat $FAKEHW/regs/$1))); }
bit() { echo $((($(reg $1) >> $2) & 1)); }
state() { sed -n 's/^state=//p' /run/pclink/status 2> /dev/null; }
is_state() { [ "$(state)" = "$1" ]; }
alive() { [ -f "$1" ] && kill -0 "$(cat "$1")" 2> /dev/null; }
port_open() { (exec 3<> "/dev/tcp/$1/$2") 2> /dev/null; }
G=/tmp/fakehw/configfs/usb_gadget/pclink
CARD=/tmp/card/tools
D=$CARD/.pclink/pclinkd.sh
SSH=(ssh -i /tmp/k -p 2222 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o BatchMode=yes -o ConnectTimeout=5 -o LogLevel=ERROR)

reset_card() {
  rm -rf /tmp/card
  mkdir -p $CARD
  cp -r $W/device/.pclink "$W/device/PC Link.sh" $CARD/
  cp /tmp/k.pub $CARD/.pclink/authorized_keys
}
stop_all() {
  [ -f $FAKEHW/unit-pclink.pid ] && kill -TERM "$(cat $FAKEHW/unit-pclink.pid)" 2> /dev/null
  waitfor 10 bash -c "! kill -0 \$(cat $FAKEHW/unit-pclink.pid 2>/dev/null) 2>/dev/null"
  pkill -f 'sshd -f /run/pclink' 2> /dev/null
  pkill -x dnsmasq 2> /dev/null
  rm -rf /run/pclink
}

ssh-keygen -q -t ed25519 -N '' -C test -f /tmp/k
ssh-keygen -q -t ed25519 -N '' -C other -f /tmp/k2
FP=$(ssh-keygen -lf /tmp/k.pub | awk '{ print $2 }')
. $W/tests/device/fakehw.sh

echo "== setup: / as on the R36S (arkos4clone's build copies its files in with mode 777, owner ark)"
chown 1000:1000 / && chmod 0777 /
check "$(stat -c '%a %u' /)" "777 1000" "/ world-writable, owned by ark"

echo "== T0: file format"
for f in "$W/device/PC Link.sh" $W/device/.pclink/pclinkd.sh; do
  if bash -n "$f"; then ok "bash -n $(basename "$f")"; else bad "bash -n $(basename "$f")"; fi
  check "$(head -n 1 "$f")" "#!/bin/bash" "$(basename "$f"): #!/bin/bash"
  check "$(tr -cd '\r' < "$f" | wc -c)" "0" "$(basename "$f"): LF line endings"
  check "$(LC_ALL=C grep -c '[^ -~	]' "$f")" "0" "$(basename "$f"): ASCII only"
done
check "$(head -n 1 $W/device/.pclink/pclink_agent.py)" "#!/usr/bin/python3" "agent: #!/usr/bin/python3"
if python3 -c "import ast, sys; ast.parse(open(sys.argv[1]).read())" $W/device/.pclink/pclink_agent.py; then ok "agent parses with this Python $(python3 -V | cut -d' ' -f2)"; else bad "agent parse"; fi

echo "== T1: check: dArkOS4Clone's Type2 board file passes, read only"
reset_card
$D check > /tmp/out 2>&1
has /tmp/out "ok: R36S Type2 layout, OTG power on, dwc2 in host mode" "check passes"
check "$(wc -l < $FAKEHW/writes.log)" "0" "no register written"

echo "== T2: check refuses EmuELEC's card-mode board file (peripheral, PHY OTG port on)"
# Not in the repository (it's the R36S's original board file, changed): copy the one USB card mode's installer makes.
if [ -f $W/tests/device/fixtures/emuelec-card-mode.dtb ]; then
  . $W/tests/device/fakehw.sh $W/tests/device/fixtures/emuelec-card-mode.dtb
  $D check > /tmp/out 2>&1
  check "$?" "1" "exit 1"
  has /tmp/out "the OTG port's board-file mode isn't otg" "says why"
else
  echo "  skipped: no tests/device/fixtures/emuelec-card-mode.dtb"
fi
. $W/tests/device/fakehw.sh $W/tests/device/fixtures/darkos-type1-panel5.dtb
$D check > /tmp/out 2>&1
has /tmp/out "ok:" "Type1 panel5 (same USB layout) passes too"
. $W/tests/device/fakehw.sh

echo "== T3: start usb"
reset_card
$D start usb > /tmp/out 2>&1
check "$?" "0" "start exits 0"
has /tmp/out "state=ready" "ready"
check "$(bit 0xff270000 4)" "0" "OTG power (GPIO3_A4) off"
check "$(($(reg 0xff270000) & 0xef))" "$((0x88))" "panel and speaker outputs on GPIO3 kept"
check "$(bit 0xff2c001c 15)" "0" "PHY VBUS detection on"
check "$(bit 0xff2c0100 9)$(bit 0xff2c0100 10)" "11" "ID forced to device"
first_vbus=$(grep -n '^0xff270000' $FAKEHW/writes.log | head -n 1 | cut -d: -f1)
first_id=$(grep -n '^0xff2c0100' $FAKEHW/writes.log | head -n 1 | cut -d: -f1)
if [ -n "$first_vbus" ] && [ -n "$first_id" ] && [ "$first_vbus" -lt "$first_id" ]; then ok "OTG power off before the role switch"; else bad "write order: vbus line $first_vbus, id line $first_id"; fi
check "$(grep '^0xff2c0100' $FAKEHW/writes.log | tr '\n' ';')" "0xff2c0100 0x04000400;0xff2c0100 0x02000200;" "ID writes as otg_mode=peripheral: output, then enable"
check "$(cat $G/idVendor):$(cat $G/idProduct):$(cat $G/bcdDevice):$(cat $G/bDeviceClass)/$(cat $G/bDeviceSubClass)/$(cat $G/bDeviceProtocol)" "0x1d6b:0x0104:0x0301:0xef/0x02/0x01" "gadget ids, IAD class"
check "$(cat $G/os_desc/use) $(cat $G/os_desc/b_vendor_code) $(cat $G/os_desc/qw_sign)" "1 0xcd MSFT100" "MS OS descriptors"
check "$(cat $G/functions/rndis.usb0/os_desc/interface.rndis/compatible_id) $(cat $G/functions/rndis.usb0/os_desc/interface.rndis/sub_compatible_id)" "RNDIS 5162001" "RNDIS compatible ID"
check "$(readlink $G/configs/c.1/rndis.usb0) $(readlink $G/os_desc/c.1)" "$G/functions/rndis.usb0 $G/configs/c.1" "function and os_desc links"
check "$(cat $G/UDC)" "ff300000.usb" "bound to the UDC"
check "$(ip -4 -o addr show usb0 | awk '{print $4}')" "10.36.36.1/24" "usb0 is 10.36.36.1/24"
has $FAKEHW/calls "nmcli device set usb0 managed no" "NetworkManager told to leave usb0 alone"
has $FAKEHW/calls "modprobe usb_f_rndis" "RNDIS function loaded"
if alive /run/pclink/dnsmasq.pid; then ok "dnsmasq running"; else bad "dnsmasq not running"; fi
if alive /run/pclink/sshd.pid && port_open 10.36.36.1 2222; then ok "sshd listening on 10.36.36.1:2222"; else bad "sshd not listening"; fi
has /run/pclink/pclinkd.log "dwc2 mode after the ID switch: device" "dwc2 reported device mode"
check "$(stat -c '%a %U' /var/lib/pclink/ssh_host_ed25519_key)" "600 root" "own host key, root only"
check "$(stat -c '%a %U' /run/pclink/keys/ark)" "644 root" "authorized keys root-owned in /run"
check "$(grep -E '^(StrictModes|LogLevel|PerSourcePenalties) ' /run/pclink/sshd_config | tr '\n' ';')" "StrictModes no;LogLevel VERBOSE;PerSourcePenalties no;" "sshd: no StrictModes (the R36S's / is 0777), verbose log, no lock-out over USB"
has /run/pclink/pclinkd.log "SSH: the PC key it takes is $FP" "logs the PC key's fingerprint"
has /run/pclink/pclinkd.log "USB network: usb0 is 10.36.36.1; NetworkManager: 10 (unmanaged)" "logs the USB network and NetworkManager's view of it"
has $FAKEHW/calls "-p ExecStopPost=/bin/bash /run/pclink/bin/pclinkd.sh ended" "systemd runs 'pclinkd.sh ended' after the service"
hasnot /run/pclink/status "warning=" "no warning (no 5 V came in)"

echo "== T4: SSH: this key only, as ark, agent through sudo; the R36S's key refusal"
check "$(stat -c '%a' /run)" "755" "/run is 0755 as on dArkOS"
/usr/sbin/sshd -f /run/pclink/sshd_config -o StrictModes=yes -o Port=2299 -o PidFile=/tmp/strict.pid -E /tmp/strict.log
waitfor 3 port_open 10.36.36.1 2299
ssh -i /tmp/k -p 2299 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o BatchMode=yes -o ConnectTimeout=5 -o LogLevel=ERROR ark@10.36.36.1 true > /tmp/out 2>&1
check "$?" "255" "with StrictModes (the first version) the key is refused, as on the R36S"
has /tmp/strict.log "Authentication refused: bad ownership or modes for directory /" "for the reason the R36S's sshd.log gives"
kill "$(cat /tmp/strict.pid)"
"${SSH[@]}" ark@10.36.36.1 'sudo -n /usr/bin/python3 /run/pclink/bin/pclink_agent.py ping' > /tmp/out 2>&1
has /tmp/out '"ok": true' "PC Link's sshd: key login + sudo agent" || tail -n 15 /run/pclink/sshd.log
has /tmp/out '"uid": 0' "agent runs as root"
has /run/pclink/sshd.log "Accepted publickey for ark from 10.36.36.1" "sshd.log: key accepted"
has /run/pclink/sshd.log "ssh2: ED25519 $FP" "with its fingerprint"
"${SSH[@]}" -o PreferredAuthentications=password -o PubkeyAuthentication=no ark@10.36.36.1 true > /tmp/out 2>&1
check "$?" "255" "password login refused"
ssh -i /tmp/k2 -p 2222 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o BatchMode=yes -o LogLevel=ERROR ark@10.36.36.1 true > /tmp/out 2>&1
check "$?" "255" "another key refused"
has /run/pclink/sshd.log "Failed publickey for ark from 10.36.36.1" "sshd.log: the other key refused, with its fingerprint"
"${SSH[@]}" root@10.36.36.1 true > /tmp/out 2>&1
check "$?" "255" "root refused"

echo "== T5: the PC connects (UDC configured, carrier, DHCP lease)"
echo "[  100.123456] dwc2 ff300000.usb: new address 12" > $FAKEHW/dmesg
echo configured > $FAKEHW/sys/class/udc/ff300000.usb/state
echo 1 > $FAKEHW/sys/class/net/usb0/carrier
echo 2 > $FAKEHW/sys/class/net/usb0/carrier_changes
echo "9999999999 02:36:53:00:00:02 10.36.36.13 pc *" > /run/pclink/dnsmasq.leases
if waitfor 4 is_state connected; then ok "state connected"; else bad "state $(state)"; fi
has /run/pclink/pclinkd.log "USB: configured; usb0 packets in 0, out 0 (errors 0/0, dropped 0/0)" "USB state logged with the traffic"
has /run/pclink/pclinkd.log "; kernel: dwc2 ff300000.usb: new address 12" "and the kernel's latest USB news"
sleep 1.5
hasnot /run/pclink/pclinkd.log "carrier changes" "the link's first carrier comes with the connection: not logged again"
$D status > /tmp/out 2>&1
has /tmp/out "service=active" "service active"
has /tmp/out "pc=10.36.36.13" "PC address from the lease"
has /tmp/out "otg_power_now=off" "OTG power off"
has /tmp/out "five_volt_in=no" "no 5 V seen"
$D diag > /tmp/out 2>&1
has /tmp/out "Service: active, usb link connected" "diag: service and link"
has /tmp/out "USB: configured; usb0 10.36.36.1/24; usb0 packets in" "diag: USB state, address, traffic"
has /tmp/out "sshd: running" "diag: sshd"
has /tmp/out "dnsmasq: running" "diag: dnsmasq"
has /tmp/out "PC key: $FP" "diag: the PC key's fingerprint"
has /tmp/out "OTG power: off; 5 V coming in: no" "diag: OTG power"

echo "== T5b: the PC puts the USB link to sleep: still connected"
echo 1 > $FAKEHW/cable
echo "[  200.000001] dwc2 ff300000.usb: USB SUSPEND" >> $FAKEHW/dmesg
echo suspended > $FAKEHW/sys/class/udc/ff300000.usb/state
if waitfor 4 grep -q "message=Connected; the PC has put the USB link to sleep." /run/pclink/status; then ok "says so"; else bad "message: $(grep message /run/pclink/status)"; fi
check "$(state)" "connected" "still connected"
has /run/pclink/pclinkd.log "; kernel: dwc2 ff300000.usb: USB SUSPEND" "the kernel's news since the last line logged"
check "$(grep -c 'new address 12' /run/pclink/pclinkd.log)" "1" "each kernel message logged once"
echo configured > $FAKEHW/sys/class/udc/ff300000.usb/state
if waitfor 4 grep -q "message=Connected to the PC." /run/pclink/status; then ok "awake again"; else bad "message: $(grep message /run/pclink/status)"; fi

echo "== T5c: something takes the USB network's address or brings it down: logged, put back"
ip addr flush dev usb0
if waitfor 5 grep -q "the USB network lost its setup (usb0: no IPv4 address" /run/pclink/pclinkd.log; then ok "logged, with what was found"; else bad "not logged"; fi
if waitfor 3 bash -c "ip -4 -o addr show usb0 | grep -q 10.36.36.1/24"; then ok "address put back"; else bad "address not back"; fi
if waitfor 3 grep -q "the USB network is set up again" /run/pclink/pclinkd.log; then ok "all clear logged"; else bad "no all clear"; fi
has /run/pclink/status "warning=The USB network lost its address at" "status warning for the menu"
ip link set usb0 down
if waitfor 5 bash -c "ip -o link show usb0 | grep -q '[<,]UP[,>]'"; then ok "brought down: up again"; else bad "still down"; fi

echo "== T5d: dnsmasq or sshd stops: logged with its last log lines, started again"
kill "$(cat /run/pclink/dnsmasq.pid)"
if waitfor 5 grep -q "dnsmasq stopped (its log:" /run/pclink/pclinkd.log; then ok "dnsmasq: logged"; else bad "dnsmasq: not logged"; fi
if waitfor 5 alive /run/pclink/dnsmasq.pid; then ok "dnsmasq running again"; else bad "dnsmasq not restarted"; fi
kill "$(cat /run/pclink/sshd.pid)"
if waitfor 5 grep -q "sshd stopped (its log:" /run/pclink/pclinkd.log; then ok "sshd: logged"; else bad "sshd: not logged"; fi
if waitfor 5 port_open 10.36.36.1 2222; then ok "sshd listening again"; else bad "sshd not restarted"; fi

echo "== T5e: the cable comes out and goes back in, the UDC still saying configured (as on the R36S): seen from the 5 V"
echo 0 > $FAKEHW/cable
if waitfor 4 is_state ready; then ok "cable out: waiting for the PC"; else bad "state $(state)"; fi
has /run/pclink/status "message=Waiting for the PC (cable unplugged?)." "says so"
has /run/pclink/pclinkd.log "USB: the PC's 5 V went away (cable unplugged?); usb0 packets in" "logged, with the traffic"
check "$(cat $FAKEHW/sys/class/udc/ff300000.usb/state)" "configured" "(the UDC state didn't change)"
echo 1 > $FAKEHW/cable
if waitfor 4 is_state connected; then ok "cable back: connected"; else bad "state $(state)"; fi
has /run/pclink/status "message=Connected to the PC." "says so"
has /run/pclink/pclinkd.log "USB: the PC's 5 V came in" "logged"

echo "== T5f: the PC re-enumerates the R36S (usb0's carrier drops and comes back): logged with the kernel's messages"
printf '[  300.000001] dwc2 ff300000.usb: new device is high-speed\n[  300.500002] dwc2 ff300000.usb: new address 35\n' >> $FAKEHW/dmesg
echo 4 > $FAKEHW/sys/class/net/usb0/carrier_changes
if waitfor 4 grep -q "USB: the PC re-enumerated the R36S (usb0 carrier changes 2 -> 4); usb0 packets in" /run/pclink/pclinkd.log; then
  ok "down and up between two looks: logged"
else
  bad "not logged"
fi
has /run/pclink/pclinkd.log "; kernel: dwc2 ff300000.usb: new device is high-speed|dwc2 ff300000.usb: new address 35" "with the kernel's messages"
echo 0 > $FAKEHW/sys/class/net/usb0/carrier
echo 5 > $FAKEHW/sys/class/net/usb0/carrier_changes
if waitfor 4 grep -q "USB: usb0 lost its carrier (carrier changes 4 -> 5)" /run/pclink/pclinkd.log; then ok "carrier lost: logged"; else bad "not logged"; fi
echo 1 > $FAKEHW/sys/class/net/usb0/carrier
echo 6 > $FAKEHW/sys/class/net/usb0/carrier_changes
if waitfor 4 grep -q "USB: the PC re-enumerated the R36S (usb0 carrier changes 5 -> 6)" /run/pclink/pclinkd.log; then ok "carrier back: logged"; else bad "not logged"; fi
check "$(state)" "connected" "still connected: the PC's 5 V never went"

echo "== T5g: traffic: the receive errors this kernel's RNDIS code makes are labelled harmless"
S=$FAKEHW/sys/class/net/usb0/statistics
echo 2255 > $S/rx_packets
echo 2409 > $S/tx_packets
echo 2215 > $S/rx_errors
echo 2215 > $S/rx_length_errors
echo 10 > $S/rx_dropped
$D diag > /tmp/out 2>&1
has /tmp/out "usb0 packets in 2255, out 2409 (errors 2215/0, all harmless length errors, dropped 10/0)" "all length errors"
echo 2220 > $S/rx_errors
$D diag > /tmp/out 2>&1
has /tmp/out "usb0 packets in 2255, out 2409 (errors 2220/0, 2215 of them harmless length errors, dropped 10/0)" "other errors too"

echo "== T6: something switches OTG power back on: off again within 2 s"
echo $(($(reg 0xff270000) | 16)) > $FAKEHW/regs/0xff270000
if waitfor 3 bash -c "[ \$(((\$(cat $FAKEHW/regs/0xff270000) >> 4) & 1)) = 0 ]"; then ok "switched off again"; else bad "OTG power stayed on"; fi
has /run/pclink/pclinkd.log "switched on by something else" "logged"

echo "== T7: unplugged, then stop: link down, ID back, OTG power stays off"
echo "not attached" > $FAKEHW/sys/class/udc/ff300000.usb/state
if waitfor 4 is_state ready; then ok "back to waiting"; else bad "state $(state)"; fi
echo 1 > $FAKEHW/cable
if waitfor 3 test -e /run/pclink/detector-ok; then ok "the PC's 5 V seen with our power off: 5 V detection validated"; else bad "detector-ok not set"; fi
$D stop > /tmp/out 2>&1
has /tmp/out "state=stopped" "stopped"
has /tmp/out "OTG power stays off until Tools > PC Link > OTG power on" "says OTG power stays off"
if alive $FAKEHW/unit-pclink.pid; then bad "service still running"; else ok "service ended"; fi
if alive /run/pclink/sshd.pid || pgrep -f 'sshd -f /run/pclink' > /dev/null; then bad "sshd still running"; else ok "sshd stopped"; fi
if pgrep -x dnsmasq > /dev/null; then bad "dnsmasq still running"; else ok "dnsmasq stopped"; fi
check "$(cat $G/UDC)" "" "gadget unbound"
check "$(find "$G/configs/c.1" "$G/os_desc" -mindepth 1 -maxdepth 1 \( -name 'rndis*' -o -name c.1 \) 2> /dev/null | wc -l)" "0" "links removed"
check "$(bit 0xff2c0100 9)$(bit 0xff2c0100 10)" "00" "ID override off again"
check "$(grep '^0xff2c0100' $FAKEHW/writes.log | tail -n 2 | tr '\n' ';')" "0xff2c0100 0x02000000;0xff2c0100 0x04000000;" "override released before the forced value"
check "$(bit 0xff270000 4)" "0" "OTG power still off"

echo "== T8: vbus-on: refused while 5 V comes in or before the 5 V detection was seen working; done once unplugged"
$D vbus-on > /tmp/out 2>&1
check "$?" "1" "refused"
has /tmp/out "5 V is coming in on the OTG port" "says why"
check "$(bit 0xff270000 4)" "0" "OTG power still off"
echo 0 > $FAKEHW/cable
mv /run/pclink/detector-ok /run/pclink/detector-ok.saved
$D vbus-on > /tmp/out 2>&1
check "$?" "1" "refused without a validated 5 V detection"
has /tmp/out "hasn't seen a PC's 5 V on this port since the last restart" "says why, and to restart instead"
check "$(bit 0xff270000 4)" "0" "OTG power still off"
mv /run/pclink/detector-ok.saved /run/pclink/detector-ok
$D vbus-on > /tmp/out 2>&1
check "$?" "0" "accepted with the cable out"
check "$(bit 0xff270000 4)" "1" "OTG power on"
check "$(bit 0xff2c001c 15)" "1" "PHY VBUS detection back to its original (off)"
$D vbus-on > /tmp/out 2>&1
has /tmp/out "already on" "again: already on"
stop_all

echo "== T9: Wi-Fi: SSH on every address, no USB changes"
: > $FAKEHW/writes.log
: > $FAKEHW/calls
$D start wifi > /tmp/out 2>&1
check "$?" "0" "start wifi exits 0"
has /tmp/out "mode=wifi" "mode wifi"
if port_open 127.0.0.1 2222; then ok "sshd on every address"; else bad "sshd not on 127.0.0.1"; fi
"${SSH[@]}" ark@127.0.0.1 'sudo -n /usr/bin/python3 /run/pclink/bin/pclink_agent.py ping' > /tmp/out 2>&1
has /tmp/out '"ok": true' "key login over the other address"
check "$(wc -l < $FAKEHW/writes.log)" "0" "no register written"
hasnot $FAKEHW/calls "modprobe" "no gadget"
check "$(grep '^PerSourcePenalties' /run/pclink/sshd_config)" "PerSourcePenalties yes" "over Wi-Fi OpenSSH's lock-out after failed logins stays on"
$D stop > /tmp/out 2>&1
has /tmp/out "state=stopped" "stopped"
stop_all

echo "== T10: keys: none, CRLF, a private key"
rm $CARD/.pclink/authorized_keys
$D start usb > /tmp/out 2>&1
check "$?" "1" "no key: refused"
has /tmp/out "No PC key" "says so"
printf '%s\r\n' "$(cat /tmp/k.pub)" > $CARD/.pclink/authorized_keys
$D start wifi > /tmp/out 2>&1
has /tmp/out "state=ready" "CRLF key accepted"
"${SSH[@]}" ark@127.0.0.1 true > /dev/null 2>&1
check "$?" "0" "and works"
stop_all
cp /tmp/k $CARD/.pclink/authorized_keys
$D start wifi > /tmp/out 2>&1
check "$?" "1" "private key file: refused"
has /tmp/out "has no SSH public key" "says so"
reset_card

echo "== T11: failure after OTG power is off: power stays off, ID put back, state failed"
. $W/tests/device/fakehw.sh
touch $FAKEHW/fail-modprobe
$D start usb > /tmp/out 2>&1
check "$?" "1" "start fails"
has /tmp/out "state=failed" "state failed"
has /tmp/out "can't set up the USB network gadget" "says why"
check "$(bit 0xff270000 4)" "0" "OTG power left off (safe side)"
check "$(bit 0xff2c0100 9)" "0" "ID override released"
rm -f $FAKEHW/fail-modprobe
stop_all

echo "== T12: a board file that doesn't match: nothing written, nothing started"
if [ -f $W/tests/device/fixtures/emuelec-card-mode.dtb ]; then
  . $W/tests/device/fakehw.sh $W/tests/device/fixtures/emuelec-card-mode.dtb
  $D start usb > /tmp/out 2>&1
  check "$?" "1" "refused"
  has /tmp/out "Not started: the OTG port's board-file mode isn't otg" "says why"
  check "$(wc -l < $FAKEHW/writes.log)" "0" "no register written"
  hasnot $FAKEHW/calls "systemd-run" "no service started"
  . $W/tests/device/fakehw.sh
else
  echo "  skipped: no tests/device/fixtures/emuelec-card-mode.dtb"
fi

echo "== T13: pclink.conf: another subnet and port; bad lines ignored"
printf 'subnet=192.168.236\r\nport=2323\nsubnet=1.2.3.4.5\nport=99\n' > $CARD/.pclink/pclink.conf
$D start usb > /tmp/out 2>&1
check "$(ip -4 -o addr show usb0 | awk '{print $4}')" "192.168.236.1/24" "usb0 on the configured subnet"
if port_open 192.168.236.1 2323; then ok "sshd on the configured port"; else bad "sshd not on 192.168.236.1:2323"; fi
pgrep -a dnsmasq > /tmp/out
has /tmp/out "--dhcp-range=192.168.236.10,192.168.236.20" "DHCP range follows"
$D stop > /dev/null 2>&1
stop_all
rm $CARD/.pclink/pclink.conf

echo "== T14: start while running: no second link, and never USB on top of Wi-Fi"
: > $FAKEHW/calls
: > $FAKEHW/writes.log
$D start wifi > /dev/null 2>&1
if flock -n /run/pclink/lock true; then ok "the running service doesn't hold the lock"; else bad "lock still held after start"; fi
$D start wifi > /tmp/out 2>&1
has /tmp/out "already on" "same mode again: says it's on"
$D start usb > /tmp/out 2>&1
check "$?" "1" "USB while Wi-Fi runs: refused"
has /tmp/out "PC Link is already on over wifi: stop it first" "says why"
check "$(grep -c 'systemd-run' $FAKEHW/calls)" "1" "one service"
check "$(wc -l < $FAKEHW/writes.log)" "0" "no register written (OTG power untouched, so no cable prompt)"
stop_all

echo "== T15: dwc2 stays in host mode: start fails, power stays off, ID put back"
. $W/tests/device/fakehw.sh
reset_card
touch $FAKEHW/dwc2-host
$D start usb > /tmp/out 2>&1
check "$?" "1" "start fails"
has /tmp/out "The OTG port stayed in host mode: dwc2 didn't follow the ID switch." "says why"
has /tmp/out "state=failed" "state failed"
check "$(bit 0xff270000 4)" "0" "OTG power off"
check "$(bit 0xff2c0100 9)" "0" "ID override released"
if alive /run/pclink/sshd.pid; then bad "sshd started anyway"; else ok "no sshd"; fi
rm -f $FAKEHW/dwc2-host
stop_all

echo "== T16: OTG power won't stay off: link dropped at once, R36S powered off after 2 s"
. $W/tests/device/fakehw.sh
reset_card
$D start usb > /dev/null 2>&1
touch $FAKEHW/vbus-stuck
echo $(($(reg 0xff270000) | 16)) > $FAKEHW/regs/0xff270000
if waitfor 4 bash -c "[ -z \"\$(cat $G/UDC)\" ]"; then ok "gadget unbound"; else bad "gadget still bound"; fi
if waitfor 2 is_state failed; then ok "state failed"; else bad "state $(state)"; fi
has /run/pclink/status "unplug the cable now" "tells to unplug"
hasnot $FAKEHW/calls "systemctl poweroff" "no poweroff yet (one failed check)"
# 8 failed checks, 0.25 s apart on the R36S; slower here, where each devmem is a bash script under qemu
if waitfor 20 grep -q 'systemctl poweroff --no-block' $FAKEHW/calls; then ok "powered off (orderly)"; else bad "no poweroff"; fi
if waitfor 30 grep -q 'systemctl poweroff --force --force' $FAKEHW/calls; then ok "still running 2 s later: forced"; else bad "no forced poweroff"; fi
has /run/pclink/status "the R36S is powering off to cut it" "says so"
$D stop > /tmp/out 2>&1
has /tmp/out "OTG power reads on" "teardown doesn't claim it's safe"
has /tmp/out "state=failed" "stop reports failed"
rm -f $FAKEHW/vbus-stuck
stop_all

echo "== T17: start, stop and vbus-on take turns (lock)"
. $W/tests/device/fakehw.sh
mkdir -p /run/pclink
(
  flock 9
  sleep 3
) 9> /run/pclink/lock &
sleep 0.5
t0=$(date +%s)
$D stop > /dev/null 2>&1
waited=$(($(date +%s) - t0))
if [ "$waited" -ge 2 ]; then ok "stop waited for the lock (${waited} s)"; else bad "stop didn't wait (${waited} s)"; fi
wait

echo "== T18: while the service is still starting or stopping: no OTG power on, no start"
. $W/tests/device/fakehw.sh
reset_card
echo 1 > $FAKEHW/regs/0xff270000
mkdir -p /run/pclink
: > /run/pclink/detector-ok
: > $FAKEHW/writes.log
echo deactivating > $FAKEHW/state-pclink
$D vbus-on > /tmp/out 2>&1
check "$?" "1" "vbus-on refused while stopping"
has /tmp/out "PC Link is still stopping" "says why"
echo activating > $FAKEHW/state-pclink
$D vbus-on > /tmp/out 2>&1
has /tmp/out "Stop PC Link first" "vbus-on refused while starting"
$D start usb > /tmp/out 2>&1
check "$?" "1" "start refused"
has /tmp/out "still starting or stopping" "says why"
check "$(wc -l < $FAKEHW/writes.log)" "0" "no register written"
rm -f $FAKEHW/state-pclink
stop_all

echo "== T19: vbus-on after the service was killed outright: undoes the link first"
. $W/tests/device/fakehw.sh
reset_card
$D start usb > /dev/null 2>&1
echo 1 > $FAKEHW/cable
if waitfor 8 test -e /run/pclink/detector-ok; then ok "5 V detection validated"; else bad "detector-ok not set"; fi
kill -KILL "$(cat $FAKEHW/unit-pclink.pid)"
waitfor 3 bash -c "! kill -0 \$(cat $FAKEHW/unit-pclink.pid) 2>/dev/null"
echo failed > $FAKEHW/state-pclink
echo 0 > $FAKEHW/cable
check "$(cat $G/UDC)$(bit 0xff2c0100 9)" "ff300000.usb1" "left behind: gadget bound, ID forced"
$D vbus-on > /tmp/out 2>&1
check "$?" "0" "vbus-on works"
check "$(cat $G/UDC)" "" "gadget unbound"
check "$(bit 0xff2c0100 9)$(bit 0xff2c0100 10)" "00" "ID override released"
check "$(bit 0xff270000 4)" "1" "then OTG power on"
rm -f $FAKEHW/state-pclink
stop_all

echo "== T20: the PC stops answering while USB says configured: logged, but not while the PC's 5 V is gone"
. $W/tests/device/fakehw.sh
reset_card
PCLINK_PROBE_SECS=1 $D start usb > /dev/null 2>&1
echo "9999999999 02:36:53:00:00:02 10.36.36.13 pc *" > /run/pclink/dnsmasq.leases
echo 1 > $FAKEHW/cable
echo configured > $FAKEHW/sys/class/udc/ff300000.usb/state
waitfor 4 is_state connected
echo 0 > $FAKEHW/cable
if waitfor 4 is_state ready; then ok "cable out"; else bad "state $(state)"; fi
sleep 10
hasnot /run/pclink/pclinkd.log "the PC stopped answering" "no stall reported while the cable is out"
echo 1 > $FAKEHW/cable
if waitfor 30 grep -q "the PC stopped answering over USB, though USB says configured" /run/pclink/pclinkd.log; then ok "cable back: logged (this usb0 can't answer ARP)"; else bad "not logged"; fi
if waitfor 5 grep -q "warning=The PC stopped answering at" /run/pclink/status; then ok "status warning for the menu"; else bad "no status warning: $(cat /run/pclink/status)"; fi
stop_all

echo "== T21: the service is killed outright: 'ended' (systemd's ExecStopPost) logs it and tidies up"
. $W/tests/device/fakehw.sh
reset_card
$D start usb > /dev/null 2>&1
kill -KILL "$(cat $FAKEHW/unit-pclink.pid)"
waitfor 3 bash -c "! kill -0 \$(cat $FAKEHW/unit-pclink.pid) 2>/dev/null"
SERVICE_RESULT=signal EXIT_CODE=killed EXIT_STATUS=KILL bash /run/pclink/bin/pclinkd.sh ended
has /run/pclink/pclinkd.log "service ended: signal (killed KILL), link ready" "how it ended, logged"
check "$(state)" "failed" "state failed"
has /run/pclink/status "message=PC Link stopped unexpectedly (signal, KILL)." "says so"
check "$(cat $G/UDC)" "" "gadget unbound"
check "$(bit 0xff2c0100 9)$(bit 0xff2c0100 10)" "00" "ID override released"
check "$(bit 0xff270000 4)" "0" "OTG power still off"
if alive /run/pclink/sshd.pid; then bad "sshd still running"; else ok "sshd stopped"; fi
if pgrep -x dnsmasq > /dev/null; then bad "dnsmasq still running"; else ok "dnsmasq stopped"; fi
stop_all
. $W/tests/device/fakehw.sh
reset_card
$D start usb > /dev/null 2>&1
$D stop > /dev/null 2>&1
: > $FAKEHW/writes.log
SERVICE_RESULT=success EXIT_CODE=exited EXIT_STATUS=0 bash /run/pclink/bin/pclinkd.sh ended
has /run/pclink/pclinkd.log "service ended: success (exited 0), link stopped" "after a normal stop: logged"
check "$(state)" "stopped" "and nothing else"
check "$(wc -l < $FAKEHW/writes.log)" "0" "no register written"
stop_all

echo
echo "passed $PASS, failed $FAIL"
[ "$FAIL" -eq 0 ]
