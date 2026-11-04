# Test tools for R36S PC Link, and a Debian 13 stand-in for the dArkOS4Clone root file system:
# - a C compiler with the kernel's uapi headers (drm_abi.c) and python3-evdev (the button tests, which need real
#   uinput ioctls, so they run in the native amd64 image rather than under arm64 emulation);
# - the same OpenSSH, dnsmasq, iproute2 and sudo as dArkOS4Clone (Debian 13), and its user ark (uid 1000, passwordless
#   sudo that keeps the caller's PATH, home folder mode 777), so test_pclinkd.sh also runs without the dArkOS image
#   (standin-run.sh);
# - on amd64, dArkOS's gptokeyb (/opt/inttools/gptokeyb, from christianhaitian/gptokeyb, branch inttools, as dArkOS's
#   build_gptokeyb.sh builds it), so the button tests can check what a Tools menu gets from a press.
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
ARG GPTOKEYB_COMMIT=95df823030475a9b8958281d716254dbca0792f8
ARG GPTOKEYB_SHA256=a2c92e128c08ca1ef361c2d53fb61ad7a1987817b96b0b82f7782801ff38e26a
RUN if [ "$(dpkg --print-architecture)" = amd64 ]; then \
    apt-get update \
    && apt-get install -y --no-install-recommends g++ libsdl2-dev libevdev-dev curl ca-certificates \
    && curl -fsSL -o /tmp/gptokeyb.cpp "https://raw.githubusercontent.com/christianhaitian/gptokeyb/$GPTOKEYB_COMMIT/gptokeyb.cpp" \
    && echo "$GPTOKEYB_SHA256  /tmp/gptokeyb.cpp" | sha256sum -c - \
    && mkdir -p /opt/inttools \
    && g++ -O2 -w -std=c++11 $(sdl2-config --cflags) -I/usr/include/libevdev-1.0 /tmp/gptokeyb.cpp -o /opt/inttools/gptokeyb -levdev $(sdl2-config --libs) \
    && rm -rf /tmp/gptokeyb.cpp /var/lib/apt/lists/*; \
  fi
