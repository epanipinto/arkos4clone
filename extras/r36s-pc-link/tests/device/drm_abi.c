/* Prints, as JSON, the kernel ABI values pclink_agent.py hard-codes (DRM and evdev ioctl numbers, struct sizes and
 * field offsets, button codes), taken from the kernel's own uapi headers. check_abi.py compares them. */
#include <stddef.h>
#include <stdio.h>
#include <fcntl.h>
#include <drm/drm.h>
#include <drm/drm_mode.h>
#include <linux/input.h>

#define OFF(s, m) ((unsigned long)offsetof(s, m))
#define SIZE(s) ((unsigned long)sizeof(s))

int main(void)
{
	printf("{\n");
	printf(" \"DRM_IOCTL_GEM_CLOSE\": %lu,\n", (unsigned long)DRM_IOCTL_GEM_CLOSE);
	printf(" \"DRM_IOCTL_PRIME_HANDLE_TO_FD\": %lu,\n", (unsigned long)DRM_IOCTL_PRIME_HANDLE_TO_FD);
	printf(" \"DRM_IOCTL_MODE_GETRESOURCES\": %lu,\n", (unsigned long)DRM_IOCTL_MODE_GETRESOURCES);
	printf(" \"DRM_IOCTL_MODE_GETCRTC\": %lu,\n", (unsigned long)DRM_IOCTL_MODE_GETCRTC);
	printf(" \"DRM_IOCTL_MODE_GETFB\": %lu,\n", (unsigned long)DRM_IOCTL_MODE_GETFB);
	printf(" \"DRM_IOCTL_MODE_MAP_DUMB\": %lu,\n", (unsigned long)DRM_IOCTL_MODE_MAP_DUMB);
	printf(" \"EVIOCGBIT_KEY\": %lu,\n", (unsigned long)EVIOCGBIT(EV_KEY, 96));
	printf(" \"EVIOCGREP\": %lu,\n", (unsigned long)EVIOCGREP);
	printf(" \"input_event_size\": %lu,\n", SIZE(struct input_event));
	printf(" \"card_res_size\": %lu, \"card_res_crtc_id_ptr\": %lu, \"card_res_count_crtcs\": %lu,\n", SIZE(struct drm_mode_card_res),
	       OFF(struct drm_mode_card_res, crtc_id_ptr), OFF(struct drm_mode_card_res, count_crtcs));
	printf(" \"crtc_size\": %lu, \"crtc_crtc_id\": %lu, \"crtc_fb_id\": %lu, \"crtc_x\": %lu, \"crtc_y\": %lu, \"crtc_mode_valid\": %lu,\n",
	       SIZE(struct drm_mode_crtc), OFF(struct drm_mode_crtc, crtc_id), OFF(struct drm_mode_crtc, fb_id), OFF(struct drm_mode_crtc, x),
	       OFF(struct drm_mode_crtc, y), OFF(struct drm_mode_crtc, mode_valid));
	printf(" \"crtc_hdisplay\": %lu, \"crtc_vdisplay\": %lu, \"crtc_vrefresh\": %lu,\n",
	       OFF(struct drm_mode_crtc, mode) + OFF(struct drm_mode_modeinfo, hdisplay),
	       OFF(struct drm_mode_crtc, mode) + OFF(struct drm_mode_modeinfo, vdisplay),
	       OFF(struct drm_mode_crtc, mode) + OFF(struct drm_mode_modeinfo, vrefresh));
	printf(" \"fb_cmd_size\": %lu, \"fb_cmd_handle\": %lu, \"map_dumb_size\": %lu, \"map_dumb_offset\": %lu,\n", SIZE(struct drm_mode_fb_cmd),
	       OFF(struct drm_mode_fb_cmd, handle), SIZE(struct drm_mode_map_dumb), OFF(struct drm_mode_map_dumb, offset));
	printf(" \"prime_size\": %lu, \"prime_fd\": %lu, \"gem_close_size\": %lu, \"DRM_CLOEXEC\": %lu,\n", SIZE(struct drm_prime_handle),
	       OFF(struct drm_prime_handle, fd), SIZE(struct drm_gem_close), (unsigned long)DRM_CLOEXEC);
	printf(" \"buttons\": {\"up\": %d, \"down\": %d, \"left\": %d, \"right\": %d, \"a\": %d, \"b\": %d, \"x\": %d, \"y\": %d,", BTN_DPAD_UP,
	       BTN_DPAD_DOWN, BTN_DPAD_LEFT, BTN_DPAD_RIGHT, BTN_EAST, BTN_SOUTH, BTN_NORTH, BTN_WEST);
	printf(" \"l1\": %d, \"r1\": %d, \"l2\": %d, \"r2\": %d, \"select\": %d, \"start\": %d, \"l3\": %d, \"r3\": %d, \"fn\": %d}\n", BTN_TL, BTN_TR,
	       BTN_TL2, BTN_TR2, BTN_TRIGGER_HAPPY1, BTN_TRIGGER_HAPPY2, BTN_TRIGGER_HAPPY3, BTN_TRIGGER_HAPPY4, BTN_TRIGGER_HAPPY5);
	printf("}\n");
	return 0;
}
