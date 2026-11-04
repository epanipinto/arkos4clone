#!/bin/bash
# Device side of the end-to-end test (tests/test_device_container.py): PC Link started over "Wi-Fi" (its sshd on every
# address) in the dArkOS4Clone chroot on fake hardware, with the test's public key (/k/key.pub), a fake fb0 showing a
# red rectangle at x 100-199, y 50-149, and a test ROM. Prints E2E-READY, then waits.
. /w/tests/device/fakehw.sh
mkdir -p /tmp/card/tools
cp -r /w/device/.pclink "/w/device/PC Link.sh" /tmp/card/tools/
cp /k/key.pub /tmp/card/tools/.pclink/authorized_keys
mkdir -p /roms/snes
echo rom > "/roms/snes/E2E Test (USA).sfc"
python3 - << 'EOF'
from pathlib import Path
fb = Path("/tmp/fakesys/class/graphics/fb0")
fb.mkdir(parents=True)
for name, value in (("modes", "U:640x480p-60"), ("bits_per_pixel", "32"), ("stride", "2560"), ("pan", "0,0")):
    (fb / name).write_text(value + "\n")
frame = bytearray(2560 * 480)
for y in range(50, 150):
    frame[y * 2560 + 400 : y * 2560 + 800] = bytes((0, 0, 255, 0)) * 100
Path("/tmp/fakedev").mkdir()
Path("/tmp/fakedev/fb0").write_bytes(frame)
EOF
/tmp/card/tools/.pclink/pclinkd.sh start wifi || {
  cat /run/pclink/pclinkd.log
  exit 1
}
printf '{"sys": "/tmp/fakesys", "dev": "/tmp/fakedev", "dri_card": "/nonexistent/card0", "devmem": "/tmp/fakehw/bin/devmem"}\n' > /run/pclink/agent.json
echo E2E-READY
exec sleep infinity
