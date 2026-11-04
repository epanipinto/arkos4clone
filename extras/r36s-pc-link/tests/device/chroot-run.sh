#!/bin/sh
# Runs a command in a writable copy of the dArkOS4Clone root file system: an overlay on the read-only image at /r,
# with /proc, /sys and /dev, a tmpfs /run and /tmp, and the PC Link folder (read-only) at /w. Needs --privileged:
#   docker run --rm --privileged --platform linux/arm64 -v /mnt/a4c:/r:ro -v <r36s-pc-link>:/w:ro \
#     r36s-pclink-tools:arm64 sh /w/tests/device/chroot-run.sh bash /w/tests/device/test_pclinkd.sh
# (r36s-pclink-tools:arm64 is tests/device/Dockerfile; any arm64 image with sh, mount and chroot works.)
set -e
mkdir -p /ov
mount -t tmpfs tmpfs /ov
mkdir -p /ov/u /ov/w /ov/m
# The overlay's / takes its mode from the upper folder: give it the image's own (dArkOS4Clone's is 0777, owner ark)
chown "$(stat -c %u:%g /r)" /ov/u
chmod "$(stat -c %a /r)" /ov/u
mount -t overlay overlay -o lowerdir=/r,upperdir=/ov/u,workdir=/ov/w /ov/m
for d in proc sys dev; do mount --rbind /$d /ov/m/$d; done
mount -t tmpfs -o mode=755 tmpfs /ov/m/run
mount -t tmpfs tmpfs /ov/m/tmp
chmod 1777 /ov/m/tmp
mkdir -p /ov/m/w
mount --bind /w /ov/m/w
mount -o remount,bind,ro /ov/m/w
if [ -d /k ]; then # the end-to-end test's key folder
  mkdir -p /ov/m/k
  mount --bind /k /ov/m/k
fi
echo "127.0.0.1 $(hostname)" >> /ov/m/etc/hosts
exec chroot /ov/m "$@"
