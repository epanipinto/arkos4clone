# Test tools for R36S PC Link, and a Debian 13 stand-in for the dArkOS4Clone root file system:
# - a C compiler with the kernel's uapi headers (drm_abi.c) and python3-evdev (the button tests, which need real
#   uinput ioctls, so they run in the native amd64 image rather than under arm64 emulation);
# - the same OpenSSH, dnsmasq, iproute2 and sudo as dArkOS4Clone (Debian 13), and its user ark (uid 1000, passwordless
#   sudo that keeps the caller's PATH, home folder mode 777), so test_pclinkd.sh also runs without the dArkOS image
#   (standin-run.sh).
#   docker build --platform linux/amd64 -t r36s-pclink-tools:amd64 tests/device
#   docker build --platform linux/arm64 -t r36s-pclink-tools:arm64 tests/device
FROM debian:trixie-slim
RUN apt-get update \
  && apt-get install -y --no-install-recommends gcc libc6-dev python3 python3-evdev \
    openssh-server openssh-client dnsmasq-base iproute2 sudo procps util-linux \
  && rm -rf /var/lib/apt/lists/* \
  && useradd -u 1000 -m -s /bin/bash ark && chmod 777 /home/ark \
  && echo 'ark ALL=(ALL) NOPASSWD: ALL' > /etc/sudoers.d/ark \
  && echo 'Defaults !secure_path' > /etc/sudoers.d/ark-no-secure-path \
  && chmod 440 /etc/sudoers.d/ark /etc/sudoers.d/ark-no-secure-path
