"""Compares pclink_agent.py's hard-coded kernel ABI values with drm_abi.c's output (from the kernel uapi headers).

gcc -o /tmp/drm_abi drm_abi.c && /tmp/drm_abi > /tmp/abi.json && python3 check_abi.py /tmp/abi.json
"""

import importlib.util
import json
import struct
import sys
from pathlib import Path

spec = importlib.util.spec_from_file_location("pclink_agent", Path(__file__).resolve().parents[2] / "device/.pclink/pclink_agent.py")
agent = importlib.util.module_from_spec(spec)
spec.loader.exec_module(agent)

abi = json.loads(Path(sys.argv[1]).read_text())
# What the agent assumes: its ioctl numbers, and the offsets and sizes in its struct.pack / unpack_from calls.
assumed = {
    "DRM_IOCTL_GEM_CLOSE": agent.DRM_IOCTL_GEM_CLOSE,
    "DRM_IOCTL_PRIME_HANDLE_TO_FD": agent.DRM_IOCTL_PRIME_HANDLE_TO_FD,
    "DRM_IOCTL_MODE_GETRESOURCES": agent.DRM_IOCTL_MODE_GETRESOURCES,
    "DRM_IOCTL_MODE_GETCRTC": agent.DRM_IOCTL_MODE_GETCRTC,
    "DRM_IOCTL_MODE_GETFB": agent.DRM_IOCTL_MODE_GETFB,
    "DRM_IOCTL_MODE_MAP_DUMB": agent.DRM_IOCTL_MODE_MAP_DUMB,
    "EVIOCGBIT_KEY": agent.EVIOCGBIT_KEY,
    "EVIOCGREP": agent.EVIOCGREP,
    "input_event_size": len(agent.event(1, 2, 3)),
    "card_res_size": struct.calcsize("<QQQQ8I"),
    "card_res_crtc_id_ptr": 8,
    "card_res_count_crtcs": 36,
    "crtc_size": 104,
    "crtc_crtc_id": 12,
    "crtc_fb_id": 16,
    "crtc_x": 20,
    "crtc_y": 24,
    "crtc_mode_valid": 32,
    "crtc_hdisplay": 40,
    "crtc_vdisplay": 50,
    "crtc_vrefresh": 60,
    "fb_cmd_size": struct.calcsize("<7I"),
    "fb_cmd_handle": 24,
    "map_dumb_size": struct.calcsize("<IIQ"),
    "map_dumb_offset": 8,
    "prime_size": struct.calcsize("<IIi"),
    "prime_fd": 8,
    "gem_close_size": struct.calcsize("<II"),
    "DRM_CLOEXEC": agent.DRM_CLOEXEC,
    "buttons": agent.BUTTONS,
}
bad = {k: (v, abi[k]) for k, v in assumed.items() if abi[k] != v}
for key, (have, want) in bad.items():
    print(f"MISMATCH {key}: agent {have} kernel {want}")
print(f"{len(assumed) - len(bad)} of {len(assumed)} ABI values match ({len(abi['buttons'])} button codes)")
sys.exit(1 if bad else 0)
