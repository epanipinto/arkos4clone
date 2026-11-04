#!/bin/bash
# R36S PC Link service for dArkOS4Clone on the R36S clone (board file "clone type2 panel1 amp"). Lets r36s-remote.py
# on the PC view and control dArkOS while it runs, over the OTG USB cable (USB networking) or over Wi-Fi, through a
# key-only SSH server on port 2222. Started from Options > Tools > PC Link ("PC Link.sh", one folder up).
#
#   pclinkd.sh start usb|wifi   start the link as the systemd service "pclink" (root; re-runs itself with sudo -n)
#   pclinkd.sh stop [--no-block] stop it. After a USB link the OTG port's 5 V stays off (see vbus-on)
#   pclinkd.sh status           key=value lines
#   pclinkd.sh diag             read only: the link right now, in words (for Show the log)
#   pclinkd.sh check            read only: is this the board layout PC Link was written for?
#   pclinkd.sh vbus-on          OTG 5 V back on: refused while the link runs or while 5 V comes in on the port
#   pclinkd.sh run usb|wifi     the service itself (systemd runs it from /run/pclink/bin)
#   pclinkd.sh ended            run by systemd after the service: logs how it ended, tidies up if it was killed
#
# USB, with no reboot and no boot-file change. dArkOS's board file makes the OTG port a USB host with 5 V always on
# (vbus_host, GPIO3_A4) and leaves the USB PHY's OTG half to nobody, so the service:
#  1. Switches the OTG 5 V off first (GPIO3_A4 low, read back) and checks it 4 times a second while the link runs, so
#     the R36S never drives 5 V into the PC. It stays off after the link stops; vbus-on, or the next restart, puts it back.
#  2. Makes the port a USB device: forces the PHY's ID input to "device" in its GRF register, the same two writes as the
#     PHY driver's otg_mode=peripheral (refused here, because the board file disables the PHY's OTG port). dwc2
#     (dr_mode otg) then switches roles by itself. Put back when the link stops.
#  3. Adds a configfs RNDIS gadget with Microsoft OS descriptors, so Windows uses its own driver. The R36S is
#     <subnet>.1 and dnsmasq gives the PC an address (no gateway, no DNS).
#  4. Starts its own sshd on <subnet>.1:2222 (Wi-Fi: every address): keys only, user ark, keys from authorized_keys.
# While the link runs it logs what it sees (USB state, the PC's 5 V and usb0's carrier, with the kernel's messages;
# traffic; an ARP check of the PC) and puts back what something else takes away (the network address, dnsmasq, sshd),
# saying so in pclinkd.log.
# Registers are only touched when the device tree matches that layout (check_board).
# Optional pclink.conf next to this script: subnet=10.36.36 and port=2222 lines.
# A fixed PATH: dArkOS's sudo keeps the caller's (sudoers.d/ark-no-secure-path). The tests put their mocks first.
export PATH=${PCLINK_TEST_PATH:+$PCLINK_TEST_PATH:}/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
UNIT=pclink
RUN=${PCLINK_RUN:-/run/pclink}
SYS=${PCLINK_SYS:-/sys}
DT=${PCLINK_DT:-/proc/device-tree}
CFS=${PCLINK_CONFIGFS:-/sys/kernel/config}
VARLIB=${PCLINK_VARLIB:-/var/lib/pclink}
DEVMEM=${PCLINK_DEVMEM:-busybox devmem}
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
UDC=ff300000.usb
G=$CFS/usb_gadget/pclink
LINK_USER=ark
GPIO3_DR=0xff270000    # bit 4: GPIO3_A4 = vbus_host
VBUS_BIT=4
GRF_VBUSDET=0xff2c001c # bit 15: 1 = the PHY's VBUS detection is off
GRF_OTG_CON=0xff2c0100 # bit 9 iddig_en (override the ID pin), bit 10 iddig_output (1 = device)
GRF_OTG_ST=0xff2c0120  # bit 9 utmi_bvalid: 5 V on the OTG port
DWC2_GOTGCTL=0xff300000 # bit 16 ConIDSts (1 = device), bit 19 BSesVld
DWC2_GINTSTS=0xff300014 # bit 0 CurMod (1 = host)
KEY_TYPES='ssh-ed25519|ecdsa-sha2-nistp256|ecdsa-sha2-nistp384|ecdsa-sha2-nistp521|ssh-rsa|sk-ssh-ed25519@openssh.com|sk-ecdsa-sha2-nistp256@openssh.com'

die() {
  echo "$*" >&2
  exit 1
}

need_root() {
  [ "$(id -u)" = 0 ] || exec sudo -n /bin/bash "${BASH_SOURCE[0]}" "$@"
}

log() {
  printf '%s %s\n' "$(date '+%F %T')" "$*" >> "$RUN/pclinkd.log"
}

# key=value status for the Tools menu and the PC; written whole, then renamed.
declare -A ST
status_write() {
  local k
  {
    for k in "${!ST[@]}"; do printf '%s=%s\n' "$k" "${ST[$k]}"; done
  } > "$RUN/status.new" && mv -f "$RUN/status.new" "$RUN/status"
}

read_conf() {
  local subnet=10.36.36 port=2222 line
  if [ -f "$HERE/pclink.conf" ]; then
    while IFS= read -r line; do
      line=${line%$'\r'}
      case $line in
        subnet=*) [[ ${line#subnet=} =~ ^([0-9]{1,3}\.){2}[0-9]{1,3}$ ]] && subnet=${line#subnet=} ;;
        port=*) [[ ${line#port=} =~ ^[0-9]{4,5}$ ]] && [ "${line#port=}" -le 65535 ] && port=${line#port=} ;;
      esac
    done < "$HERE/pclink.conf"
  fi
  printf 'SUBNET=%s\nPORT=%s\n' "$subnet" "$port"
}

# ------------------------------------------------------------------------------------------------- registers

reg() {
  local v
  v=$($DEVMEM "$(printf '0x%08x' "$1")" 32 2> /dev/null) || return 1
  [[ $v =~ ^0[xX][0-9a-fA-F]+$ ]] || return 1
  echo $((v))
}

reg_write() {
  $DEVMEM "$(printf '0x%08x' "$1")" 32 "$(printf '0x%08x' "$2")"
}

bit_of() {
  echo $((($1 >> $2) & 1))
}

# Rockchip GRF registers: the high half is a write-enable mask, so one bit changes without a read-modify-write.
grf_bit() {
  reg_write "$1" $(((1 << ($2 + 16)) | ($3 << $2)))
}

# GPIO3's data register has no such mask: read, change bit 4, write, read back. The kernel also writes this register
# (speaker amplifier, panel), so the bit is checked again every second while the link runs.
vbus_set() {
  local v
  for _ in 1 2 3; do
    v=$(reg $GPIO3_DR) || return 1
    if [ "$1" = 1 ]; then v=$((v | 1 << VBUS_BIT)); else v=$((v & ~(1 << VBUS_BIT) & 0xffffffff)); fi
    reg_write $GPIO3_DR $v || return 1
    v=$(reg $GPIO3_DR) || return 1
    [ "$(bit_of "$v" $VBUS_BIT)" = "$1" ] && return 0
  done
  return 1
}

vbus_state() {
  local v
  v=$(reg $GPIO3_DR) || {
    echo unknown
    return
  }
  [ "$(bit_of "$v" $VBUS_BIT)" = 1 ] && echo on || echo off
}

five_volt_in() {
  local v
  v=$(reg $GRF_OTG_ST) || return 2
  [ "$(bit_of "$v" 9)" = 1 ]
}

dt_str() {
  tr -d '\0' < "$DT/$1" 2> /dev/null
}

dt_hex() {
  od -An -v -tx1 "$DT/$1" 2> /dev/null | tr -d ' \n'
}

# The layout this was written for: RK3326, dwc2 OTG port in dr_mode otg, its 5 V switch on GPIO3_A4 (active high),
# and the USB PHY at GRF 0xff2c0100 (px30 / rk3328 register map) with its OTG half disabled.
check_board() {
  case $(dt_str compatible) in *rockchip,rk3326* | *rockchip,px30*) ;; *) echo "this isn't an RK3326 board" && return 1 ;; esac
  [ "$(dt_str usb@ff300000/dr_mode)" = otg ] || {
    echo "the OTG port's board-file mode isn't otg"
    return 1
  }
  [ -n "$(dt_hex pinctrl/gpio3@ff270000/phandle)" ] && [ "$(dt_hex vbus_host/gpio)" = "$(dt_hex pinctrl/gpio3@ff270000/phandle)0000000400000000" ] || {
    echo "the board file's vbus_host isn't GPIO3_A4"
    return 1
  }
  case $(dt_str syscon@ff2c0000/usb2-phy@100/compatible) in *rockchip,px30-usb2phy* | *rockchip,rk3328-usb2phy*) ;; *) echo "unexpected USB PHY" && return 1 ;; esac
  [ "$(dt_str syscon@ff2c0000/usb2-phy@100/otg-port/status)" = disabled ] || {
    echo "the USB PHY's OTG port is enabled in the board file (its driver owns the ID switch then)"
    return 1
  }
  [ -e "$SYS/class/udc/$UDC" ] || {
    echo "no USB device controller $UDC (dwc2 not loaded?)"
    return 1
  }
  reg $GPIO3_DR > /dev/null || {
    echo "can't read registers with '$DEVMEM'"
    return 1
  }
}

save_regs() {
  [ -s "$RUN/regs.orig" ] && return 0 # the first link since boot saw the real originals
  local con det
  con=$(reg $GRF_OTG_CON) && det=$(reg $GRF_VBUSDET) || return 1
  printf 'iddig_en=%s\niddig_output=%s\nvbus_det_off=%s\n' "$(bit_of "$con" 9)" "$(bit_of "$con" 10)" "$(bit_of "$det" 15)" > "$RUN/regs.orig"
}

# Same order as the PHY driver's otg_mode_store for "peripheral": the forced value, then the override.
role_device() {
  grf_bit $GRF_OTG_CON 10 1 && grf_bit $GRF_OTG_CON 9 1
}

role_restore() {
  local iddig_en=0 iddig_output=0 con
  [ -s "$RUN/regs.orig" ] && . "$RUN/regs.orig"
  if [ "$iddig_en" = 0 ]; then
    grf_bit $GRF_OTG_CON 9 0 && grf_bit $GRF_OTG_CON 10 "$iddig_output"
  else
    grf_bit $GRF_OTG_CON 10 "$iddig_output" && grf_bit $GRF_OTG_CON 9 1
  fi || return 1
  con=$(reg $GRF_OTG_CON) || return 1
  [ "$(bit_of "$con" 9)$(bit_of "$con" 10)" = "$iddig_en$iddig_output" ]
}

# dwc2's own view (read only, and only while dwc2 is bound, so its clock is on): "device" or "host".
dwc2_mode() {
  local v
  [ -e "$SYS/bus/platform/drivers/dwc2/$UDC" ] || return 1
  v=$(reg $DWC2_GINTSTS) || return 1
  [ "$(bit_of "$v" 0)" = 1 ] && echo host || echo device
}

# ---------------------------------------------------------------------------------------------- gadget, network

gadget_up() {
  modprobe libcomposite && modprobe usb_f_rndis || return 1
  [ -d "$CFS/usb_gadget" ] || mount -t configfs none "$CFS" 2> /dev/null
  [ -d "$CFS/usb_gadget" ] || return 1
  [ -d "$G" ] && gadget_down
  mkdir -p "$G/strings/0x409" "$G/configs/c.1/strings/0x409" "$G/functions/rndis.usb0/os_desc/interface.rndis" "$G/os_desc" || return 1
  echo 0x1d6b > "$G/idVendor" &&
    echo 0x0104 > "$G/idProduct" &&
    echo 0x0301 > "$G/bcdDevice" &&
    echo 0x0200 > "$G/bcdUSB" &&
    echo 0xef > "$G/bDeviceClass" &&
    echo 0x02 > "$G/bDeviceSubClass" &&
    echo 0x01 > "$G/bDeviceProtocol" &&
    echo "R36S" > "$G/strings/0x409/manufacturer" &&
    echo "R36S PC Link" > "$G/strings/0x409/product" &&
    echo "R36SPCLINK1" > "$G/strings/0x409/serialnumber" &&
    echo "RNDIS" > "$G/configs/c.1/strings/0x409/configuration" &&
    echo 500 > "$G/configs/c.1/MaxPower" &&
    echo 0x80 > "$G/configs/c.1/bmAttributes" &&
    echo 02:36:53:00:00:01 > "$G/functions/rndis.usb0/dev_addr" &&
    echo 02:36:53:00:00:02 > "$G/functions/rndis.usb0/host_addr" &&
    echo 1 > "$G/os_desc/use" &&
    echo 0xcd > "$G/os_desc/b_vendor_code" &&
    echo MSFT100 > "$G/os_desc/qw_sign" &&
    echo RNDIS > "$G/functions/rndis.usb0/os_desc/interface.rndis/compatible_id" &&
    echo 5162001 > "$G/functions/rndis.usb0/os_desc/interface.rndis/sub_compatible_id" &&
    ln -s "$G/functions/rndis.usb0" "$G/configs/c.1/" &&
    ln -s "$G/configs/c.1" "$G/os_desc/" &&
    echo "$UDC" > "$G/UDC"
}

gadget_down() {
  [ -d "$G" ] || return 0
  echo "" > "$G/UDC" 2> /dev/null
  rm -f "$G/os_desc/c.1" "$G/configs/c.1/rndis.usb0"
  rmdir "$G/configs/c.1/strings/0x409" "$G/configs/c.1" "$G/functions/rndis.usb0" "$G/strings/0x409" "$G" 2> /dev/null
  return 0
}

net_up() {
  IFACE=
  for _ in $(seq 1 50); do
    IFACE=$(cat "$G/functions/rndis.usb0/ifname" 2> /dev/null)
    [ -n "$IFACE" ] && [ -e "$SYS/class/net/$IFACE" ] && break
    IFACE=
    sleep 0.1
  done
  [ -n "$IFACE" ] || return 1
  if command -v nmcli > /dev/null && nmcli -t -f RUNNING general 2> /dev/null | grep -qx running; then
    nmcli device set "$IFACE" managed no >> "$RUN/pclinkd.log" 2>&1 || log "NetworkManager didn't let go of $IFACE"
  fi
  ip addr flush dev "$IFACE" && ip addr add "$SUBNET.1/24" dev "$IFACE" && ip link set "$IFACE" up || return 1
  log "USB network: $IFACE is $SUBNET.1$(nm_state "$IFACE")$(dhcp_clients)"
}

dhcp_up() {
  dnsmasq --conf-file=/dev/null --interface="$IFACE" --bind-interfaces --except-interface=lo --port=0 \
    --dhcp-range="$SUBNET.10,$SUBNET.20,255.255.255.0,1h" --dhcp-option=3 --dhcp-option=6 --dhcp-authoritative \
    --dhcp-leasefile="$RUN/dnsmasq.leases" --pid-file="$RUN/dnsmasq.pid" --log-facility="$RUN/dnsmasq.log" --log-dhcp
}

sshd_up() {
  local penalties=yes
  [ "$MODE" = usb ] && penalties=no
  mkdir -p /run/sshd && chmod 755 /run/sshd
  if [ ! -s "$VARLIB/ssh_host_ed25519_key" ]; then
    mkdir -p "$VARLIB" && chmod 700 "$VARLIB" && ssh-keygen -q -t ed25519 -N '' -C 'R36S PC Link' -f "$VARLIB/ssh_host_ed25519_key" < /dev/null || return 1
    log "made the PC Link SSH host key"
  fi
  install -d -m 755 "$RUN/keys" && install -m 644 "$RUN/authorized_keys" "$RUN/keys/$LINK_USER" || return 1
  cat > "$RUN/sshd_config" << EOF
# R36S PC Link's own sshd (pclinkd.sh); dArkOS's ssh service is separate.
Port $PORT
ListenAddress $1
HostKey $VARLIB/ssh_host_ed25519_key
PidFile $RUN/sshd.pid
AuthorizedKeysFile $RUN/keys/%u
# StrictModes checks every folder from the key file up to /, and dArkOS4Clone's / is world-writable (its build copies
# its files in with mode 777), so it would refuse every key. The key file and its folders are root's, made by PC Link.
StrictModes no
AllowUsers $LINK_USER
PermitRootLogin no
PubkeyAuthentication yes
PasswordAuthentication no
KbdInteractiveAuthentication no
UsePAM yes
X11Forwarding no
AllowAgentForwarding no
AllowTcpForwarding no
PermitTunnel no
PrintMotd no
ClientAliveInterval 15
ClientAliveCountMax 4
AcceptEnv LANG LC_*
Subsystem sftp /usr/lib/openssh/sftp-server
# VERBOSE: the fingerprint of every key accepted or refused, in sshd.log (Tools > PC Link > Show the log).
LogLevel VERBOSE
# Over USB the PC is the only peer: OpenSSH's lock-out after failed logins would only get in the way.
PerSourcePenalties $penalties
EOF
  log "SSH: the PC key it takes is $(ssh-keygen -lf "$RUN/keys/$LINK_USER" 2> /dev/null | awk '{ print $2 }' | paste -sd ' ' -)"
  /usr/sbin/sshd -t -f "$RUN/sshd_config" >> "$RUN/pclinkd.log" 2>&1 && /usr/sbin/sshd -f "$RUN/sshd_config" -E "$RUN/sshd.log"
}

# The last lines of a log, on one line.
last_words() {
  tail -n "${2:-2}" "$1" 2> /dev/null | tr -d '\r' | paste -sd '|' -
}

# Packets to and from the PC on the USB network, with errors and drops: does data still flow? Length errors are harmless
# here: this kernel's RNDIS code passes on the empty remainder of each USB transfer from the PC (none when Windows pads
# the transfer to a multiple of 512 bytes), and usb0 counts each as one.
traffic() {
  [ -n "$IFACE" ] || return 0
  local k
  local -A n
  for k in rx_packets tx_packets rx_errors tx_errors rx_dropped tx_dropped rx_length_errors; do
    n[$k]=$(cat "$SYS/class/net/$IFACE/statistics/$k" 2> /dev/null) || return 0
  done
  printf '; %s packets in %s, out %s (errors %s/%s' "$IFACE" "${n[rx_packets]}" "${n[tx_packets]}" "${n[rx_errors]}" "${n[tx_errors]}"
  if [ "${n[rx_length_errors]}" -ge "${n[rx_errors]}" ] && [ "${n[rx_length_errors]}" -gt 0 ]; then
    printf ', all harmless length errors'
  elif [ "${n[rx_length_errors]}" -gt 0 ]; then
    printf ', %s of them harmless length errors' "${n[rx_length_errors]}"
  fi
  printf ', dropped %s/%s)' "${n[rx_dropped]}" "${n[tx_dropped]}"
}

# The kernel's messages about the OTG port (dwc2, the gadget, RNDIS) since the last ones logged, in KNEWS (not printed:
# KMSG_TS has to survive, which it wouldn't in a $(...) subshell).
KMSG_TS=0
KNEWS=
kernel_usb_news() {
  local news
  KNEWS=
  news=$(dmesg 2> /dev/null | awk -v t="$KMSG_TS" 'match($0, /^\[ *[0-9]+\.[0-9]+\]/) {
    if (substr($0, 2, RLENGTH - 2) + 0 > t && tolower($0) ~ /dwc2|ff300000|gadget|rndis|usb0/) print }' | tail -n 4)
  [ -n "$news" ] || return 0
  KMSG_TS=$(tail -n 1 <<< "$news" | sed 's/^\[ *\([0-9.]*\)\].*/\1/')
  KNEWS="; kernel: $(sed 's/^\[ *[0-9.]*\] //' <<< "$news" | paste -sd '|' -)"
}

nm_state() {
  command -v nmcli > /dev/null || return 0
  printf '; NetworkManager: %s' "$(nmcli -t -g GENERAL.STATE,GENERAL.CONNECTION device show "$1" 2> /dev/null | paste -sd ' ' - | sed 's/ *$//' | grep . || echo unknown)"
}

dhcp_clients() {
  local p
  p=$(pgrep -l 'dhcpcd|dhclient|udhcpc' 2> /dev/null | paste -sd ',' -)
  [ -z "$p" ] || printf '; DHCP clients: %s' "$p"
}

kill_pidfile() {
  local pid
  pid=$(cat "$1" 2> /dev/null) && [ -n "$pid" ] && kill "$pid" 2> /dev/null
  rm -f "$1"
}

pc_address() {
  [ -s "$RUN/dnsmasq.leases" ] && awk 'NR == 1 { print $3 }' "$RUN/dnsmasq.leases"
}

# ----------------------------------------------------------------------------------------------------- service

# While the USB link exists the OTG 5 V must read off. Turned on by something else: off again. Can't be confirmed off
# (a read fails, or the bit won't clear): drop the USB link at once; if that lasts 2 s, power the R36S off, which cuts
# the 5 V for certain: an orderly power-off first, then a forced one every 2 s while this still runs.
VBUS_FAILS=0
vbus_guard() {
  local st
  st=$(vbus_state)
  if [ "$st" = on ]; then
    log "OTG 5 V was switched on by something else: off again"
    vbus_set 0
    st=$(vbus_state)
  fi
  if [ "$st" = off ]; then
    VBUS_FAILS=0
    return 0
  fi
  VBUS_FAILS=$((VBUS_FAILS + 1))
  if [ "$VBUS_FAILS" = 1 ]; then
    log "OTG 5 V can't be confirmed off ($st): dropping the USB link"
    gadget_down
    ST[state]=failed
    ST[message]="OTG power couldn't be confirmed off: unplug the cable now."
    status_write
  elif [ "$VBUS_FAILS" = 8 ]; then
    log "OTG 5 V still not confirmed off after 2 s: powering the R36S off"
    ST[message]="OTG power couldn't be switched off: the R36S is powering off to cut it."
    status_write
    systemctl poweroff --no-block || log "systemctl poweroff failed"
  elif [ $((VBUS_FAILS % 8)) = 0 ]; then
    log "still running $((VBUS_FAILS / 4 - 2)) s after the power-off: forcing it"
    timeout 3 sync
    systemctl poweroff --force --force || log "forced power-off failed"
  fi
  return 1
}

# What the PC does, from three signals. The UDC state shows only the first connection: this kernel's dwc2 never reports
# a bus reset or an unplug, so after that it reads "configured" until the link stops. The PC's 5 V on the port (the
# PHY's bvalid bit, read while our own 5 V is off) shows the cable going out and in. usb0's carrier_changes, a count,
# shows the PC setting the USB network up again (a re-enumeration), even between two looks.
V5=          # the PC's 5 V at the last look: yes or no; empty before the first look
V5_GONE=0    # 1 while the PC's 5 V, once seen, is gone: the cable is out, so the stall check waits
CARRIER_N=   # usb0's carrier_changes at the last look
CARRIER_UP=0 # 1 once usb0 had a carrier: the first comes with the first connection and isn't logged
usb_status() {
  local udc v5='' n c
  [ "${ST[state]}" = failed ] && return
  udc=$(cat "$SYS/class/udc/$UDC/state" 2> /dev/null)
  if [ "$udc" != "${ST[udc]}" ]; then
    kernel_usb_news
    log "USB: ${udc:-?}$(traffic)$KNEWS"
    ST[udc]=$udc
    if [ "$udc" = configured ]; then
      ST[state]=connected
      ST[message]="Connected to the PC."
    elif [ "$udc" = suspended ] && [ "${ST[state]}" = connected ] && five_volt_in; then
      # Windows puts an idle USB device to sleep and wakes it when it has something to send
      ST[message]="Connected; the PC has put the USB link to sleep."
    elif [ "${ST[state]}" = connected ]; then
      ST[state]=ready
      ST[message]="Waiting for the PC (cable unplugged?)."
    fi
  fi
  ST[pc]=$(pc_address)
  ST[otg_power]=$(vbus_state)
  if [ "${ST[otg_power]}" = off ]; then
    five_volt_in
    case $? in 0) v5=yes ;; 1) v5=no ;; esac
    [ "$(vbus_state)" = off ] || v5=
  fi
  if [ -n "$v5" ] && [ "$v5" != "$V5" ]; then
    if [ "$v5" = yes ] && [ ! -e "$RUN/detector-ok" ]; then
      # vbus-on trusts the PHY's 5 V detection only once it has seen the PC's 5 V with our own 5 V off.
      : > "$RUN/detector-ok"
      log "the PHY sees the PC's 5 V with OTG power off: its 5 V detection works"
    elif [ -n "$V5" ]; then
      kernel_usb_news
      if [ "$v5" = yes ]; then
        log "USB: the PC's 5 V came in$KNEWS"
      else
        log "USB: the PC's 5 V went away (cable unplugged?)$(traffic)$KNEWS"
      fi
    fi
    if [ "$v5" = yes ]; then
      V5_GONE=0
      if [ "${ST[state]}" = ready ] && [ "$udc" = configured ]; then
        ST[state]=connected
        ST[message]="Connected to the PC."
      fi
    elif [ "$V5" = yes ]; then
      V5_GONE=1
      if [ "${ST[state]}" = connected ]; then
        ST[state]=ready
        ST[message]="Waiting for the PC (cable unplugged?)."
      fi
    fi
    V5=$v5
  fi
  if n=$(cat "$SYS/class/net/$IFACE/carrier_changes" 2> /dev/null) && c=$(cat "$SYS/class/net/$IFACE/carrier" 2> /dev/null); then
    if [ -z "$CARRIER_N" ]; then
      [ "$c" = 1 ] && CARRIER_UP=1
    elif [ "$n" != "$CARRIER_N" ]; then
      if [ "$c" = 0 ]; then
        kernel_usb_news
        log "USB: $IFACE lost its carrier (carrier changes $CARRIER_N -> $n)$(traffic)$KNEWS"
      elif [ "$CARRIER_UP" = 1 ]; then
        kernel_usb_news
        log "USB: the PC re-enumerated the R36S ($IFACE carrier changes $CARRIER_N -> $n)$(traffic)$KNEWS"
      fi
      [ "$c" = 1 ] && CARRIER_UP=1
    fi
    CARRIER_N=$n
  fi
}

sshd_check() {
  kill -0 "$(cat "$RUN/sshd.pid" 2> /dev/null)" 2> /dev/null && return
  if [ "${SSHD_RESTARTS:=0}" -lt 3 ]; then
    SSHD_RESTARTS=$((SSHD_RESTARTS + 1))
    log "sshd stopped (its log: $(last_words "$RUN/sshd.log" 3)): starting it again"
    if [ "$MODE" = usb ]; then sshd_up "$SUBNET.1"; else sshd_up 0.0.0.0; fi || log "sshd didn't start again"
  elif [ "$SSHD_RESTARTS" = 3 ]; then
    SSHD_RESTARTS=4
    ST[message]="sshd keeps stopping: see Tools > PC Link > Show the log."
    log "sshd keeps stopping; not starting it again"
  fi
}

DNSMASQ_RESTARTS=0
dnsmasq_check() {
  kill -0 "$(cat "$RUN/dnsmasq.pid" 2> /dev/null)" 2> /dev/null && return
  [ "$DNSMASQ_RESTARTS" -ge 3 ] && return
  DNSMASQ_RESTARTS=$((DNSMASQ_RESTARTS + 1))
  log "dnsmasq stopped (its log: $(last_words "$RUN/dnsmasq.log")): starting it again"
  dhcp_up || log "dnsmasq didn't start again"
}

# The USB network must keep its address and stay up: anything else taking the interface over (NetworkManager, a DHCP
# client) would cut the PC off while USB still looks connected. Say what was found, then put it back.
NET_BAD=0
net_check() {
  local link addrs
  [ -n "$IFACE" ] || return
  link=$(ip -o link show dev "$IFACE" 2> /dev/null)
  addrs=$(ip -4 -o addr show dev "$IFACE" 2> /dev/null | awk '{ print $4 }' | paste -sd ' ' -)
  if [[ $link =~ [\<,]UP[,\>] ]] && [[ " $addrs " == *" $SUBNET.1/24 "* ]]; then
    [ "$NET_BAD" -gt 0 ] && log "the USB network is set up again"
    NET_BAD=0
    return
  fi
  NET_BAD=$((NET_BAD + 1))
  if [ -z "$link" ]; then
    if [ "$NET_BAD" = 1 ]; then
      kernel_usb_news
      log "the USB network interface $IFACE is gone (UDC $(cat "$G/UDC" 2> /dev/null))$KNEWS"
    fi
    return
  fi
  if [ "$NET_BAD" -le 3 ] || [ $((NET_BAD % 60)) = 0 ]; then
    kernel_usb_news
    log "the USB network lost its setup ($IFACE: ${addrs:-no IPv4 address}, $(sed 's/^[^<]*<\([^>]*\)>.*/\1/' <<< "$link")$(nm_state "$IFACE")$(dhcp_clients)$KNEWS): putting it back"
  fi
  ST[warning]="The USB network lost its address at $(date +%T); PC Link put it back (see Show the log)."
  ip addr add "$SUBNET.1/24" dev "$IFACE" 2> /dev/null
  ip link set "$IFACE" up
}

# Every 30 s while the PC is connected: an ARP request to it, which Windows answers even when its firewall drops
# pings. No answer three times in a row while USB still says configured: the USB data path has stalled. Not while the
# PC's 5 V is gone: then the cable is out.
PROBE_SECS=${PCLINK_PROBE_SECS:-30}
PROBE_MISSES=0
PROBE_CHECK=
pc_probe() {
  local pc=${ST[pc]}
  if [ "${ST[state]}" != connected ] || [ "$V5_GONE" = 1 ] || [ "${ST[udc]}" != configured ] || [ -z "$pc" ] || [ -z "$IFACE" ]; then
    PROBE_MISSES=0
    PROBE_CHECK=
    return
  fi
  if [ -n "$PROBE_CHECK" ] && [ "$tick" -ge "$PROBE_CHECK" ]; then
    PROBE_CHECK=
    if ip neigh show to "$pc" dev "$IFACE" 2> /dev/null | grep -q REACHABLE; then
      [ "$PROBE_MISSES" -ge 3 ] && log "the PC answers again"
      PROBE_MISSES=0
    else
      PROBE_MISSES=$((PROBE_MISSES + 1))
      if [ "$PROBE_MISSES" = 3 ]; then
        kernel_usb_news
        log "the PC stopped answering over USB, though USB says configured$(traffic)$KNEWS"
        ST[warning]="The PC stopped answering at $(date +%T), though USB says connected (see Show the log)."
      fi
    fi
  fi
  if [ -z "$PROBE_CHECK" ] && [ $((tick % (PROBE_SECS * 4))) -lt 4 ]; then
    ip neigh flush to "$pc" dev "$IFACE" 2> /dev/null
    (timeout 1 bash -c "exec 3<> /dev/tcp/$pc/9" > /dev/null 2>&1 &)
    PROBE_CHECK=$((tick + 8))
  fi
}

# Undo the link and check the result: state stopped only if the gadget is unbound, the ID override is back and the
# OTG 5 V reads off (it stays off until vbus-on). $1: the reason, when a failure ends the link (state failed, one
# write, so start never sees an in-between "stopped").
teardown() {
  local why=$1 problems=() st
  ST[state]=stopping
  status_write
  kill_pidfile "$RUN/sshd.pid"
  kill_pidfile "$RUN/dnsmasq.pid"
  if [ "$MODE" = usb ]; then
    gadget_down
    [ -n "$(cat "$G/UDC" 2> /dev/null)" ] && problems+=("the USB gadget is still bound")
    role_restore || problems+=("the PHY's ID input couldn't be put back")
    # dwc2 changes role a moment after the ID goes back: keep the 5 V off through that
    for _ in 1 2 3 4; do
      sleep 0.25
      [ "$(vbus_state)" = on ] && log "OTG 5 V came on as the port went back to host mode: off again" && vbus_set 0
    done
    st=$(vbus_state)
    if [ "$st" = on ]; then
      vbus_set 0
      st=$(vbus_state)
    fi
    ST[otg_power]=$st
    [ "$st" = off ] || problems+=("OTG power reads $st")
  fi
  ST[udc]=
  if [ ${#problems[@]} -gt 0 ]; then
    log "stopped with problems: ${problems[*]}"
    ST[state]=failed
    ST[message]="${why:+$why }Link off, but: ${problems[*]}. Unplug the cable, then restart the R36S."
  elif [ -n "$why" ]; then
    log "stopped ($MODE)"
    ST[state]=failed
    ST[message]=$why
  else
    log "stopped ($MODE)"
    ST[state]=stopped
    ST[message]="Link off."
    [ "$MODE" = usb ] && ST[message]="Link off. OTG power stays off until Tools > PC Link > OTG power on, or a restart."
  fi
  status_write
}

fail() {
  log "FAILED: $*"
  teardown "$*"
  exit 1
}

service() {
  MODE=$1
  local why mode_now tick=0
  . "$RUN/conf"
  ST=([mode]=$MODE [state]=starting [port]=$PORT [since]="$(date '+%F %T')")
  status_write
  log "starting ($MODE, $SUBNET.1:$PORT)"
  trap 'STOP=1' TERM INT HUP
  STOP=0
  if [ "$MODE" = usb ]; then
    why=$(check_board) || fail "Not started: $why"
    save_regs || fail "Not started: can't read the USB PHY registers"
    vbus_set 0 || fail "Not started: OTG power didn't switch off"
    ST[otg_power]=off
    log "OTG 5 V off"
    grf_bit $GRF_VBUSDET 15 0 || fail "can't turn on the PHY's VBUS detection"
    for _ in $(seq 1 20); do
      five_volt_in || break
      sleep 0.5
    done
    if five_volt_in; then
      ST[warning]="5 V was already coming in on the OTG port: next time, connect the cable only when PC Link asks for it."
      log "5 V seen on the OTG port with our power off: a PC was probably connected already"
    fi
    role_device || fail "can't switch the OTG port to USB device mode"
    mode_now=
    for _ in $(seq 1 30); do
      mode_now=$(dwc2_mode) && [ "$mode_now" = device ] && break
      sleep 0.2
    done
    log "dwc2 mode after the ID switch: ${mode_now:-unknown}$([ -n "$mode_now" ] && printf ' (GOTGCTL 0x%08x)' "$(reg $DWC2_GOTGCTL)")"
    [ "$mode_now" = device ] || fail "The OTG port stayed in ${mode_now:-an unknown} mode: dwc2 didn't follow the ID switch."
    gadget_up || fail "can't set up the USB network gadget"
    net_up || fail "the USB network interface didn't come up"
    dhcp_up || fail "dnsmasq didn't start"
    sshd_up "$SUBNET.1" || fail "sshd didn't start (see $RUN/pclinkd.log)"
    # A last look before asking for the cable: PC Link's own GPIO3 write could have raced one from the kernel
    [ "$(vbus_state)" = off ] || fail "OTG power came back on while PC Link started, so it stopped."
    ST[ip]=$SUBNET.1
    ST[state]=ready
    ST[message]="Connect the OTG port to the PC now: straight into a laptop USB-C port, not a hub."
  else
    sshd_up 0.0.0.0 || fail "sshd didn't start (see $RUN/pclinkd.log)"
    ST[ip]=$(ip -4 -o addr show scope global 2> /dev/null | awk '{ sub("/.*", "", $4); print $4 }' | paste -sd ' ')
    ST[state]=ready
    ST[message]="On Wi-Fi: r36s-remote --host <address> on the PC."
  fi
  status_write
  log "ready"
  while [ "$STOP" = 0 ]; do
    [ "$MODE" = usb ] && vbus_guard
    if [ $((tick % 4)) = 0 ]; then
      if [ "$MODE" = usb ]; then
        usb_status
        if [ "${ST[state]}" != failed ]; then
          net_check
          dnsmasq_check
          pc_probe
        fi
      fi
      sshd_check
      status_write
    fi
    tick=$((tick + 1))
    sleep 0.25 &
    wait $!
  done
  teardown
}

show_status() {
  local active=inactive
  systemctl is-active --quiet $UNIT && active=active
  echo "service=$active"
  [ -f "$RUN/status" ] && cat "$RUN/status"
  if check_board > /dev/null 2>&1; then
    echo "otg_power_now=$(vbus_state)"
    if five_volt_in; then echo "five_volt_in=yes"; else echo "five_volt_in=no"; fi
  fi
}

# Read only: the link as it is right now, in words (Tools > PC Link > Show the log).
diag() {
  local p pid state
  state=$(sed -n 's/^state=//p' "$RUN/status" 2> /dev/null)
  echo "Service: $(unit_state | grep . || echo inactive)${state:+, $(sed -n 's/^mode=//p' "$RUN/status") link $state}"
  IFACE=$(cat "$G/functions/rndis.usb0/ifname" 2> /dev/null)
  if [ -n "$IFACE" ]; then
    echo "USB: $(cat "$SYS/class/udc/$UDC/state" 2> /dev/null); $IFACE $(ip -4 -o addr show dev "$IFACE" 2> /dev/null | awk '{ print $4 }' | paste -sd ' ' - | grep . || echo "has no address")$(traffic)"
  fi
  for p in sshd dnsmasq; do
    pid=$(cat "$RUN/$p.pid" 2> /dev/null)
    if [ -n "$pid" ] && kill -0 "$pid" 2> /dev/null; then echo "$p: running"; else echo "$p: not running"; fi
  done
  [ -s "$RUN/keys/$LINK_USER" ] && echo "PC key: $(ssh-keygen -lf "$RUN/keys/$LINK_USER" 2> /dev/null | awk '{ print $2 }' | paste -sd ' ' -)"
  if check_board > /dev/null 2>&1; then
    echo "OTG power: $(vbus_state); 5 V coming in: $(five_volt_in && echo yes || echo no)"
  fi
}

# systemd runs this once the service has ended, whatever the reason (ExecStopPost): log how it ended, and if it didn't
# get to tidy up (it was killed), tidy up now, so the R36S is left as after a normal stop.
ended() {
  local k v
  [ -f "$RUN/status" ] || return 0
  while IFS='=' read -r k v; do [ -n "$k" ] && ST[$k]=$v; done < "$RUN/status"
  log "service ended: ${SERVICE_RESULT:-?} (${EXIT_CODE:-?} ${EXIT_STATUS:-?}), link ${ST[state]:-?}"
  case ${ST[state]} in stopped | failed) return 0 ;; esac
  MODE=${ST[mode]}
  teardown "PC Link stopped unexpectedly (${SERVICE_RESULT:-?}, ${EXIT_STATUS:-?})."
}

# The service's systemd state: active, activating, deactivating, inactive, failed (inactive once collected).
unit_state() {
  systemctl show -p ActiveState --value $UNIT 2> /dev/null
}

# start, stop and vbus-on take turns, so OTG power can't come on while a USB link starts.
lock() {
  install -d -m 755 "$RUN" || die "can't make $RUN"
  exec 9> "$RUN/lock"
  flock -w 45 9 || die "PC Link is busy: try again in a minute."
}

start() {
  local mode=$1 st why active
  case $mode in usb | wifi) ;; *) die "usage: pclinkd.sh start usb|wifi" ;; esac
  lock
  case $(unit_state) in
    active | reloading)
      active=$(sed -n 's/^mode=//p' "$RUN/status" 2> /dev/null)
      [ "$active" = "$mode" ] || die "PC Link is already on over ${active:-another link}: stop it first."
      echo "PC Link is already on."
      show_status
      return 0
      ;;
    activating | deactivating) die "PC Link is still starting or stopping: try again in a few seconds." ;;
  esac
  [ "$HERE" = "$RUN/bin" ] && die "Start PC Link from Options > Tools > PC Link."
  [ -s "$HERE/authorized_keys" ] || die "No PC key in $HERE/authorized_keys: run install-pc-link.py on the PC first."
  install -d -m 755 "$RUN" "$RUN/bin" || die "can't make $RUN"
  if ! install -m 755 "$HERE/pclinkd.sh" "$RUN/bin/pclinkd.sh" || ! install -m 644 "$HERE/pclink_agent.py" "$RUN/bin/pclink_agent.py"; then
    die "can't copy PC Link to $RUN"
  fi
  tr -d '\r' < "$HERE/authorized_keys" | grep -E "^($KEY_TYPES) [A-Za-z0-9+/=]+( .*)?$" > "$RUN/authorized_keys"
  [ -s "$RUN/authorized_keys" ] || die "$HERE/authorized_keys has no SSH public key."
  chmod 644 "$RUN/authorized_keys"
  read_conf > "$RUN/conf"
  if [ "$mode" = usb ]; then
    why=$(check_board) || die "Not started: $why"
  fi
  rm -f "$RUN/status"
  # 9>&-: the lock is for this command only, never for the service it starts
  systemd-run --unit=$UNIT --collect --quiet -p KillMode=mixed -p TimeoutStopSec=40 -p "ExecStopPost=/bin/bash $RUN/bin/pclinkd.sh ended" \
    --description="R36S PC Link ($mode)" /bin/bash "$RUN/bin/pclinkd.sh" run "$mode" 9>&- || die "systemd-run failed"
  for _ in $(seq 1 60); do
    st=$(sed -n 's/^state=//p' "$RUN/status" 2> /dev/null)
    case $st in ready | connected | failed | stopped) break ;; esac
    sleep 0.5
  done
  show_status
  case $st in ready | connected) return 0 ;; *) return 1 ;; esac
}

stop() {
  lock
  case $(unit_state) in
    active | activating | reloading | deactivating)
      if [ "$1" = --no-block ]; then
        # r36s-remote stop-link: its SSH session can end with the link, before the rest of this output gets out
        echo "stop sent"
        systemctl stop --no-block $UNIT
      else
        systemctl stop $UNIT
      fi || {
        show_status
        die "systemctl couldn't stop PC Link."
      }
      ;;
    *) echo "PC Link is off." ;;
  esac
  show_status
}

vbus_on() {
  local why st
  lock
  case $(unit_state) in
    inactive | failed) ;;
    deactivating) die "PC Link is still stopping: try again in a few seconds." ;;
    *) die "Stop PC Link first." ;;
  esac
  why=$(check_board) || die "$why"
  [ "$(vbus_state)" = on ] && {
    echo "OTG power is already on."
    return 0
  }
  [ -e "$RUN/detector-ok" ] || die "PC Link hasn't seen a PC's 5 V on this port since the last restart, so it can't tell whether the cable is out. Unplug it and restart the R36S to get OTG power back."
  # A link the service couldn't undo (it was killed outright): undo it here, or leave the 5 V off
  gadget_down
  [ -z "$(cat "$G/UDC" 2> /dev/null)" ] || die "The USB gadget is still bound: restart the R36S with the cable out."
  role_restore || die "The PHY's ID input couldn't be put back: restart the R36S with the cable out."
  grf_bit $GRF_VBUSDET 15 0 || die "can't turn on the PHY's VBUS detection"
  for _ in 1 2 3 4 5 6; do
    five_volt_in
    st=$?
    [ $st = 0 ] && die "5 V is coming in on the OTG port: unplug the cable first, then try again."
    [ $st = 2 ] && die "can't read the USB PHY's status"
    sleep 0.5
  done
  vbus_set 1 || die "OTG power didn't switch on"
  if [ -s "$RUN/regs.orig" ]; then
    local vbus_det_off=0
    . "$RUN/regs.orig"
    grf_bit $GRF_VBUSDET 15 "$vbus_det_off"
  fi
  log "OTG 5 V back on"
  echo "OTG power is back on."
}

case $1 in
  start)
    need_root "$@"
    start "$2"
    ;;
  stop)
    need_root "$@"
    stop "$2"
    ;;
  status)
    need_root "$@"
    show_status
    ;;
  diag)
    need_root "$@"
    diag
    ;;
  ended)
    [ "$(id -u)" = 0 ] || die "ended is for systemd"
    ended
    ;;
  check)
    need_root "$@"
    why=$(check_board) || die "not supported: $why"
    echo "ok: R36S Type2 layout, OTG power $(vbus_state), dwc2 in $(dwc2_mode || echo unknown) mode"
    ;;
  vbus-on)
    need_root "$@"
    vbus_on
    ;;
  run)
    [ "$(id -u)" = 0 ] || die "run is for systemd"
    case $2 in usb | wifi) service "$2" ;; *) die "usage: pclinkd.sh run usb|wifi" ;; esac
    ;;
  *) die "usage: pclinkd.sh start usb|wifi | stop [--no-block] | status | diag | check | vbus-on" ;;
esac