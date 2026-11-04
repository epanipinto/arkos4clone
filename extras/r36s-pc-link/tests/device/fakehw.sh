#!/bin/bash
# Fake R36S hardware for the PC Link tests, in the dArkOS4Clone root file system (arm64 chroot) or its Debian 13
# stand-in (standin-run.sh):
# - devmem: registers are files in $FAKEHW/regs; GRF writes honour the write-enable mask; every write is logged in
#   $FAKEHW/writes.log. The OTG status register reports 5 V when GPIO3_A4 (OTG power) is on or $FAKEHW/cable is 1,
#   and dwc2's GINTSTS reports device mode only while the GRF forces the ID input to "device".
# - sysfs: the UDC ff300000.usb (state file), dwc2 bound, net/usb0 (statistics, carrier 0, carrier_changes 1: up,
#   no PC yet); configfs: usb_gadget; the device tree: written from a real board file (dtb2fs.py), $1, default
#   dArkOS4Clone's Type2 board file.
# - modprobe, nmcli, systemctl, systemd-run and dmesg mocks in $FAKEHW/bin (systemctl and systemd-run also in
#   /usr/local/sbin, for sudo), logging to $FAKEHW/calls. systemd-run starts the command with setsid and keeps its
#   pid in $FAKEHW/unit-<name>.pid; systemctl show answers MainPID from $FAKEHW/mainpid-<unit>, and ActiveState from
#   $FAKEHW/state-<unit> if there is one, else active while the unit's pid lives; dmesg prints $FAKEHW/dmesg.
# - switches: $FAKEHW/dwc2-host keeps dwc2 in host mode; $FAKEHW/vbus-stuck makes GPIO3_A4 ignore being cleared.
# - usb0: a dummy network interface, so ip, dnsmasq and sshd run for real.
# Usage: . fakehw.sh [board.dtb]
FAKEHW=/tmp/fakehw
W=${W:-/w}
rm -rf $FAKEHW
mkdir -p $FAKEHW/bin $FAKEHW/regs $FAKEHW/sys/class/udc/ff300000.usb $FAKEHW/sys/bus/platform/drivers/dwc2/ff300000.usb \
  $FAKEHW/sys/class/net/usb0/statistics $FAKEHW/configfs/usb_gadget/pclink/functions/rndis.usb0 $FAKEHW/dt
echo "not attached" > $FAKEHW/sys/class/udc/ff300000.usb/state
for s in rx_packets tx_packets rx_errors tx_errors rx_dropped tx_dropped rx_length_errors; do
  echo 0 > $FAKEHW/sys/class/net/usb0/statistics/$s
done
echo 0 > $FAKEHW/sys/class/net/usb0/carrier
echo 1 > $FAKEHW/sys/class/net/usb0/carrier_changes
echo usb0 > $FAKEHW/configfs/usb_gadget/pclink/functions/rndis.usb0/ifname
echo 0 > $FAKEHW/cable
echo 1 > $FAKEHW/es-active
: > $FAKEHW/calls
: > $FAKEHW/writes.log
# GPIO3: A3 panel enable, A4 OTG power, A7 speaker amplifier on. GRF: ID not overridden, PHY VBUS detection off.
echo $((0x98)) > $FAKEHW/regs/0xff270000
echo 0 > $FAKEHW/regs/0xff2c0100
echo $((0x8000)) > $FAKEHW/regs/0xff2c001c
echo 0 > $FAKEHW/regs/0xff300000
python3 $W/tests/device/dtb2fs.py "${1:-$W/tests/device/fixtures/darkos-type2-panel1-amp.dtb}" $FAKEHW/dt > /dev/null

cat > $FAKEHW/bin/devmem << 'EOF'
#!/bin/bash
FAKEHW=${FAKEHW:-/tmp/fakehw}
R=$FAKEHW/regs
addr=$(printf '0x%08x' "$1")
get() { if [ -f "$R/$1" ]; then cat "$R/$1"; else echo 0; fi; }
if [ $# -ge 3 ]; then
  val=$(($3))
  echo "$addr $(printf '0x%08x' "$val")" >> $FAKEHW/writes.log
  case $addr in
    0xff2c*) old=$(get "$addr"); mask=$(((val >> 16) & 0xffff)); new=$(((old & ~mask) | (val & mask))) ;;
    *) new=$val ;;
  esac
  [ "$addr" = 0xff270000 ] && [ -e $FAKEHW/vbus-stuck ] && new=$((new | 0x10))
  echo $((new & 0xffffffff)) > "$R/$addr"
  exit 0
fi
case $addr in
  0xff2c0120)
    v=0
    if [ $(((($(get 0xff270000)) >> 4) & 1)) = 1 ] || [ "$(cat $FAKEHW/cable)" = 1 ]; then v=$((1 << 9)); fi
    ;;
  0xff300014)
    con=$(get 0xff2c0100)
    if [ $(((con >> 9) & 1)) = 1 ] && [ $(((con >> 10) & 1)) = 1 ] && [ ! -e $FAKEHW/dwc2-host ]; then v=0; else v=1; fi
    ;;
  *) v=$(get "$addr") ;;
esac
printf '0x%08X\n' "$v"
EOF

cat > $FAKEHW/bin/modprobe << 'EOF'
#!/bin/bash
echo "modprobe $*" >> ${FAKEHW:-/tmp/fakehw}/calls
[ -e ${FAKEHW:-/tmp/fakehw}/fail-modprobe ] && exit 1
exit 0
EOF

cat > $FAKEHW/bin/nmcli << 'EOF'
#!/bin/bash
echo "nmcli $*" >> ${FAKEHW:-/tmp/fakehw}/calls
[ "$*" = "-t -f RUNNING general" ] && echo running
case "$*" in *GENERAL.STATE*" device show "*) printf '10 (unmanaged)\n\n' ;; esac
exit 0
EOF

cat > $FAKEHW/bin/dmesg << 'EOF'
#!/bin/bash
cat ${FAKEHW:-/tmp/fakehw}/dmesg 2> /dev/null
exit 0
EOF

cat > $FAKEHW/bin/systemd-run << 'EOF'
#!/bin/bash
F=${FAKEHW:-/tmp/fakehw}
echo "systemd-run $*" >> $F/calls
unit=run
while [ $# -gt 0 ]; do
  case $1 in
    --unit=*) unit=${1#--unit=} ;;
    -p | --property) shift ;;
    /*) break ;;
  esac
  shift
done
setsid "$@" < /dev/null >> "$F/unit-$unit.out" 2>&1 &
echo $! > "$F/unit-$unit.pid"
EOF

cat > $FAKEHW/bin/systemctl << 'EOF'
#!/bin/bash
F=${FAKEHW:-/tmp/fakehw}
echo "systemctl $*" >> $F/calls
args=()
noblock=
for a in "$@"; do
  case $a in --quiet | -q) ;; --no-block) noblock=1 ;; *) args+=("$a") ;; esac
done
alive() { [ -f "$F/unit-$1.pid" ] && kill -0 "$(cat "$F/unit-$1.pid")" 2> /dev/null; }
case ${args[0]} in
  is-active)
    if [ "${args[1]}" = emulationstation ]; then [ "$(cat $F/es-active)" = 1 ]; exit; fi
    alive "${args[1]}"
    exit
    ;;
  start | restart)
    [ "${args[1]}" = emulationstation ] && echo 1 > $F/es-active
    exit 0
    ;;
  stop)
    if [ "${args[1]}" = emulationstation ]; then echo 0 > $F/es-active; exit 0; fi
    alive "${args[1]}" || exit 0
    kill -TERM "$(cat "$F/unit-${args[1]}.pid")"
    [ -n "$noblock" ] && exit 0
    for i in $(seq 1 450); do alive "${args[1]}" || exit 0; sleep 0.1; done
    exit 1
    ;;
  show)
    unit=${args[-1]}
    case " ${args[*]} " in
      *" ActiveState "*)
        if [ -s "$F/state-$unit" ]; then
          cat "$F/state-$unit"
        elif alive "$unit"; then
          echo active
        else
          echo inactive
        fi
        ;;
      *) if [ -s "$F/mainpid-$unit" ]; then cat "$F/mainpid-$unit"; else echo 0; fi ;;
    esac
    exit 0
    ;;
esac
exit 0
EOF
chmod 755 $FAKEHW/bin/*
for m in systemctl systemd-run; do cp $FAKEHW/bin/$m /usr/local/sbin/$m; done
ip link show usb0 > /dev/null 2>&1 || ip link add usb0 type dummy
export FAKEHW PATH=$FAKEHW/bin:$PATH PCLINK_TEST_PATH=$FAKEHW/bin
export PCLINK_SYS=$FAKEHW/sys PCLINK_DT=$FAKEHW/dt PCLINK_CONFIGFS=$FAKEHW/configfs PCLINK_DEVMEM=$FAKEHW/bin/devmem
