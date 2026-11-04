#!/bin/sh
# Runs a command in the r36s-pclink-tools image as a stand-in for the dArkOS4Clone root file system (Debian 13 with
# the same OpenSSH, dnsmasq, iproute2 and sudo; see Dockerfile), when the dArkOS image isn't at hand. Like
# chroot-run.sh, it gives the command a tmpfs /run with mode 0755, as systemd makes it. Needs --privileged:
#   docker run --rm --privileged --platform linux/amd64 -v <r36s-pc-link>:/w:ro r36s-pclink-tools:amd64 \
#     sh /w/tests/device/standin-run.sh bash /w/tests/device/test_pclinkd.sh
set -e
mount -t tmpfs -o mode=755 tmpfs /run
exec "$@"
