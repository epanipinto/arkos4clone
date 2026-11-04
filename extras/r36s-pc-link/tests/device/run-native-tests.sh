#!/bin/bash
# In the r36s-pclink-tools image (see Dockerfile), as root with --privileged and the PC Link folder at /w:
# 1. the kernel ABI check (drm_abi.c + check_abi.py), on this image's architecture;
# 2. with --buttons, the agent's button tests against a real uinput "GO-Super Gamepad" (needs native uinput ioctls).
set -e
gcc -O0 -o /tmp/drm_abi /w/tests/device/drm_abi.c
/tmp/drm_abi > /tmp/abi.json
echo "== ABI ($(uname -m))"
python3 /w/tests/device/check_abi.py /tmp/abi.json
if [ "$1" = --buttons ]; then
  echo "== buttons (native uinput)"
  mkdir -p /run/pclink
  python3 /w/tests/device/test_agent.py Buttons -v
fi
