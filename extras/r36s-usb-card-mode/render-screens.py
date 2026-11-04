r"""Render the R36S "SD card reader mode" (USB card mode) screens into screens.tar, and optionally PNG previews.

The card-mode kernel (EmuELEC 4.7) has no framebuffer console, so the helper shows these pre-rendered screens on the
panel instead of text. Each screen is a 640x480 XRGB8888 image (the kernel's fb0 format: B, G, R, X bytes, 2560-byte
rows), gzipped, and screens.tar holds them all. The artwork is drawn with Pillow at 4x and scaled down, then the text is
drawn at full size on top. Only generic shapes are drawn: no logos. The output is reproducible: same Pillow and fonts,
same bytes.

Usage: python render-screens.py [--out DIR] [--previews DIR]
"""

import functools
import gzip
import io
import itertools
import math
import tarfile
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Annotated

import typer
from PIL import Image, ImageDraw, ImageFont

app = typer.Typer(add_completion=False, pretty_exceptions_show_locals=False)

HERE = Path(__file__).resolve().parent
FONTS = Path(r"C:\Windows\Fonts")
BOLD, SEMI, REG = "segoeuib.ttf", "seguisb.ttf", "segoeui.ttf"
W, H, SS = 640, 480, 4
BAND, MARGIN, INDENT = 80, 32, 70

Colour = tuple[int, int, int]
Point = tuple[float, float]
Box = tuple[float, float, float, float]

BG, INK, SUB, MUTED = (11, 18, 32), (241, 244, 249), (196, 205, 218), (104, 118, 141)
BLUE, GREEN, AMBER, RED = (30, 111, 217), (23, 135, 74), (184, 110, 0), (192, 57, 43)
OK, WAIT = (46, 184, 114), (122, 170, 255)
GAMES, DARKOS, GOLD = (139, 92, 246), (17, 165, 181), (226, 180, 76)
SHELL, SHELL_EDGE, SHELL_DARK = (226, 231, 238), (150, 162, 180), (186, 195, 209)
KEYS, KEYS_HI, BEZEL, DISPLAY = (62, 74, 94), (90, 103, 126), (30, 38, 54), (14, 21, 36)
LID, LID_EDGE, BASE, BASE_DARK = (44, 54, 73), (78, 90, 111), (196, 204, 216), (150, 161, 178)
PAPER, PAPER_BAR, PAPER_INK, PAPER_SUB = (246, 248, 251), (222, 228, 236), (27, 36, 54), (82, 94, 114)
SKELETON, SKELETON_HI = (220, 226, 234), (230, 235, 241)
CABLE, PLUG, METAL = (214, 221, 231), (172, 182, 198), (126, 137, 156)
# microSD outline in mm (11 x 15), contacts at the top
CARD = [(0.6, 0), (9.0, 0), (9.7, 0.7), (9.7, 5.4), (11, 6.5), (11, 14.4), (10.4, 15), (0.6, 15), (0, 14.4), (0, 0.6)]
DEVICE, LID_BOX, CABLE_Y = (44.0, 92.0), (272.0, 96.0, 572.0, 294.0), 300.0


@functools.cache
def font(name: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONTS / name), size)


class Screen:
    """A 640x480 screen. Artwork uses panel coordinates but is drawn SS times larger and scaled down; text goes on top."""

    def __init__(self, band: Colour) -> None:
        self.art = Image.new("RGB", (W * SS, H * SS), BG)
        self.draw = ImageDraw.Draw(self.art)
        self.texts: list[tuple[Point, str, ImageFont.FreeTypeFont, Colour, str]] = []
        self.rect((0, 0, W, BAND), fill=band)
        cards_glyph(self, 26, 21, 22, band)
        self.text((76, BAND / 2), "SD card reader mode", font(BOLD, 34), INK, "lm")

    def rect(self, box: Box, r: float = 0, fill: Colour | None = None, line: Colour | None = None, width: float = 0) -> None:
        self.draw.rounded_rectangle([c * SS for c in box], radius=r * SS, fill=fill, outline=line, width=round(width * SS))

    def circle(self, cx: float, cy: float, r: float, fill: Colour | None = None, line: Colour | None = None, width: float = 0) -> None:
        self.draw.ellipse([(cx - r) * SS, (cy - r) * SS, (cx + r) * SS, (cy + r) * SS], fill=fill, outline=line, width=round(width * SS))

    def poly(self, points: Sequence[Point], fill: Colour | None = None, line: Colour | None = None, width: float = 0) -> None:
        self.draw.polygon([(x * SS, y * SS) for x, y in points], fill=fill, outline=line, width=round(width * SS))

    def path(self, points: Sequence[Point], colour: Colour, width: float) -> None:
        """A line with round joints and ends."""
        self.draw.line([(x * SS, y * SS) for x, y in points], fill=colour, width=round(width * SS), joint="curve")
        for x, y in (points[0], points[-1]):
            self.circle(x, y, width / 2, fill=colour)

    def arc(self, cx: float, cy: float, r: float, start: float, end: float, colour: Colour, width: float) -> None:
        """Clockwise from start to end degrees, 0 = 3 o'clock."""
        self.draw.arc([(cx - r) * SS, (cy - r) * SS, (cx + r) * SS, (cy + r) * SS], start, end, fill=colour, width=round(width * SS))

    def text(self, xy: Point, text: str, fnt: ImageFont.FreeTypeFont, colour: Colour, anchor: str = "la") -> None:
        self.texts.append((xy, text, fnt, colour, anchor))

    def status(self, y: float, text: str) -> float:
        fnt = font(BOLD, 30)
        if MARGIN + fnt.getlength(text) > W - MARGIN:
            raise typer.BadParameter(f"{text!r} doesn't fit on one line")
        self.text((MARGIN, y), text, fnt, INK)
        return y + 44

    def para(self, y: float, text: str, icon: Callable[["Screen", float, float, Colour], None]) -> float:
        """Text wrapped from INDENT to the right margin, **strong** parts in white semibold, with an icon by the first line.
        Returns the top of the next paragraph."""
        size = 26
        regular, strong = font(REG, size), font(SEMI, size)
        space, ascent, step = regular.getlength(" "), regular.getmetrics()[0], round(size * 1.27)
        # Words are split at spaces only, so punctuation right after a **strong** part stays with it.
        words: list[list[tuple[str, ImageFont.FreeTypeFont, Colour]]] = [[]]
        for i, part in enumerate(text.split("**")):
            fnt, colour = (strong, INK) if i % 2 else (regular, SUB)
            for n, piece in enumerate(part.split(" ")):
                if n:
                    words.append([])
                if piece:
                    words[-1].append((piece, fnt, colour))
        lines: list[list[tuple[list[tuple[str, ImageFont.FreeTypeFont, Colour]], float]]] = [[]]
        width = 0.0
        for word in filter(None, words):
            length = sum(fnt.getlength(piece) for piece, fnt, _ in word)
            if lines[-1] and INDENT + width + space + length > W - MARGIN:
                lines.append([])
                width = 0.0
            width += (space if lines[-1] else 0) + length
            lines[-1].append((word, length))
        icon(self, MARGIN + 15, y + ascent - size * 0.36, SUB)
        for n, line in enumerate(lines):
            x = float(INDENT)
            for word, length in line:
                left = x
                for piece, fnt, colour in word:
                    self.text((left, y + n * step + ascent), piece, fnt, colour, "ls")
                    left += fnt.getlength(piece)
                x += length + space
        bottom = y + len(lines) * step
        if bottom > H - 10:
            raise typer.BadParameter(f"{text!r} runs off the bottom of the screen")
        return bottom + 8

    def image(self) -> Image.Image:
        img = self.art.resize((W, H), Image.Resampling.LANCZOS)
        draw = ImageDraw.Draw(img)
        for xy, text, fnt, colour, anchor in self.texts:
            draw.text(xy, text, font=fnt, fill=colour, anchor=anchor)
        return img


def card_points(x: float, y: float, w: float) -> list[Point]:
    k = w / 11
    return [(x + px * k, y + py * k) for px, py in CARD]


def microsd(s: Screen, x: float, y: float, w: float, fill: Colour, contacts: Colour | None = GOLD) -> None:
    s.poly(card_points(x, y, w), fill=fill)
    if contacts:
        k = w / 11
        for i in range(8):
            cx = x + (1.0 + i * 1.0) * k
            s.rect((cx, y + 1.0 * k, cx + 0.62 * k, y + 3.7 * k), r=0.18 * k, fill=contacts)


def cards_glyph(s: Screen, x: float, y: float, w: float, behind: Colour) -> None:
    """The mode's icon: two overlapping microSD cards."""
    s.poly(card_points(x + 0.55 * w, y, w), fill=behind, line=INK, width=max(1.4, w * 0.09))
    microsd(s, x, y + 0.35 * w, w, INK, contacts=behind)


def badge(s: Screen, cx: float, cy: float, r: float, colour: Colour, mark: str, ring: Colour = BG) -> None:
    s.circle(cx, cy, r + 3.5, fill=ring)
    s.circle(cx, cy, r, fill=colour)
    if mark == "check":
        s.path([(cx - 0.42 * r, cy + 0.03 * r), (cx - 0.12 * r, cy + 0.33 * r), (cx + 0.44 * r, cy - 0.3 * r)], INK, 0.19 * r)
    elif mark == "!":
        s.rect((cx - 0.1 * r, cy - 0.52 * r, cx + 0.1 * r, cy + 0.16 * r), r=0.1 * r, fill=INK)
        s.circle(cx, cy + 0.42 * r, 0.12 * r, fill=INK)
    else:
        s.text((cx, cy + 0.04 * r), mark, font(BOLD, round(r * 1.35)), INK, "mm")


def handheld(s: Screen, x: float, y: float, w: float = 118, display: Callable[[Screen, float, float, float], None] | None = None) -> Point:
    """A generic upright handheld shaped like the R36S (85 x 132 mm): screen on top, D-pad, four buttons, two sticks, the
    TF1 card slot on the left side and TF2 on the right. Returns the OTG port on the bottom edge."""
    u, h = w / 118, w * 132 / 85
    for left in (x + 12 * u, x + w - 38 * u):
        s.rect((left, y - 4 * u, left + 26 * u, y + 6 * u), r=3 * u, fill=SHELL_DARK)
    s.rect((x - 6 * u, y + 0.47 * h, x + 4 * u, y + 0.47 * h + 18 * u), r=2 * u, fill=DARKOS)
    s.rect((x + w - 4 * u, y + 0.47 * h, x + w + 6 * u, y + 0.47 * h + 18 * u), r=2 * u, fill=GAMES)
    s.rect((x, y, x + w, y + h), r=13 * u, fill=SHELL, line=SHELL_EDGE, width=1.2)
    s.rect((x + 7 * u, y + 9 * u, x + w - 7 * u, y + 89 * u), r=6 * u, fill=BEZEL)
    s.rect((x + 11 * u, y + 13 * u, x + w - 11 * u, y + 85 * u), r=2 * u, fill=DISPLAY)
    if display:
        display(s, x + w / 2, y + 49 * u, u)
    dx, dy = x + 0.27 * w, y + 0.615 * h
    s.rect((dx - 13 * u, dy - 4.5 * u, dx + 13 * u, dy + 4.5 * u), r=2 * u, fill=KEYS)
    s.rect((dx - 4.5 * u, dy - 13 * u, dx + 4.5 * u, dy + 13 * u), r=2 * u, fill=KEYS)
    for ox, oy in ((0, -9), (9, 0), (0, 9), (-9, 0)):
        s.circle(x + 0.73 * w + ox * u, dy + oy * u, 4.8 * u, fill=KEYS)
    for ox in (-8, 8):
        cx, cy = x + w / 2 + ox * u, y + 0.715 * h
        s.rect((cx - 4.5 * u, cy - 1.8 * u, cx + 4.5 * u, cy + 1.8 * u), r=1.8 * u, fill=SHELL_EDGE)
    for sx in (0.36, 0.64):
        s.circle(x + sx * w, y + 0.85 * h, 11 * u, fill=KEYS)
        s.circle(x + sx * w, y + 0.85 * h, 6.5 * u, fill=KEYS_HI)
    for px in (0.30, 0.70):
        s.rect((x + px * w - 6 * u, y + h - 3 * u, x + px * w + 6 * u, y + h + 0.5 * u), r=1 * u, fill=BEZEL)
    return x + 0.70 * w, y + h


def show_cards(s: Screen, cx: float, cy: float, u: float) -> None:
    cards_glyph(s, cx - 15 * u, cy - 17 * u, 17 * u, DISPLAY)


def show_badge(colour: Colour, mark: str) -> Callable[[Screen, float, float, float], None]:
    def draw(s: Screen, cx: float, cy: float, u: float) -> None:
        badge(s, cx, cy, 23 * u, colour, mark, ring=DISPLAY)

    return draw


def laptop(s: Screen, box: Box = LID_BOX, ghost: bool = False) -> tuple[Box, Point]:
    """A generic laptop: the lid box on a base. Returns its screen box and the USB port on the base's left end."""
    x1, y1, x2, y2 = box
    screen = (x1 + 9, y1 + 9, x2 - 9, y2 - 7)
    if ghost:
        dashed(s, [(x1, y1), (x2, y1), (x2, y2), (x1, y2), (x1, y1)], MUTED)
        dashed(s, [(x1 - 24, y2 + 4), (x2 + 24, y2 + 4), (x2 + 24, y2 + 12), (x1 - 24, y2 + 12), (x1 - 24, y2 + 4)], MUTED)
    else:
        s.rect(box, r=10, fill=LID, line=LID_EDGE, width=1.2)
        s.rect(screen, r=3, fill=PAPER)
        s.rect((x1 - 24, y2, x2 + 24, y2 + 12), r=5, fill=BASE)
        s.rect(((x1 + x2) / 2 - 26, y2, (x1 + x2) / 2 + 26, y2 + 3.5), r=1.5, fill=BASE_DARK)
    return screen, (x1 - 24, y2 + 6)


def window(s: Screen, screen: Box, filled: bool) -> None:
    """A file window on the laptop: both cards listed as drives, or grey placeholders while nothing is connected."""
    x1, y1, x2, _ = screen
    s.rect((x1, y1, x2, y1 + 22), r=3, fill=PAPER_BAR)
    s.rect((x1, y1 + 12, x2, y1 + 22), fill=PAPER_BAR)
    for i in range(3):
        s.circle(x1 + 14 + i * 12, y1 + 11, 3.6, fill=SHELL_EDGE)
    rows = (("Games card", "R36S-GAMES", GAMES), ("dArkOS card", "BOOT, EASYROMS", DARKOS))
    for i, (name, drives, colour) in enumerate(rows):
        top = y1 + 32 + i * 74
        if filled:
            microsd(s, x1 + 16, top + 6, 34, colour)
            s.text((x1 + 64, top + 8), name, font(SEMI, 22), PAPER_INK)
            s.text((x1 + 64, top + 36), drives, font(REG, 20), PAPER_SUB)
            badge(s, x2 - 28, top + 29, 11, OK, "check", ring=PAPER)
        else:
            microsd(s, x1 + 16, top + 6, 34, SKELETON, contacts=None)
            s.rect((x1 + 64, top + 12, x1 + 64 + 120, top + 27), r=7.5, fill=SKELETON)
            s.rect((x1 + 64, top + 39, x1 + 64 + 156, top + 52), r=6.5, fill=SKELETON_HI)


def cable(s: Screen, port: Point, end_x: float, plugged: bool, y: float = CABLE_Y) -> None:
    """From the handheld's port down and along to end_x: into the PC's port, or ending in a loose plug."""
    px, py = port
    r = 9.0
    s.rect((px - 6.5, py, px + 6.5, py + 10), r=3, fill=PLUG)
    bend = [(px + r - r * math.cos(t), y - r + r * math.sin(t)) for t in (i * math.pi / 16 for i in range(9))]
    s.path([(px, py + 10), *bend, (end_x - 15, y)], CABLE, 6)
    s.rect((end_x - 15, y - 6.5, end_x, y + 6.5), r=3, fill=PLUG)
    if not plugged:
        s.rect((end_x, y - 4, end_x + 7, y + 4), r=1.5, fill=METAL)


def dashed(s: Screen, points: Sequence[Point], colour: Colour, dash: float = 9, gap: float = 7, width: float = 2.4) -> None:
    for (ax, ay), (bx, by) in itertools.pairwise(points):
        length = math.hypot(bx - ax, by - ay)
        t = 0.0
        while t < length:
            e = min(t + dash, length)
            s.path([(ax + (bx - ax) * t / length, ay + (by - ay) * t / length), (ax + (bx - ax) * e / length, ay + (by - ay) * e / length)], colour, width)
            t += dash + gap


def arrow(s: Screen, x1: float, x2: float, y: float, colour: Colour) -> None:
    """A dashed arrow pointing right."""
    dashed(s, [(x1, y), (x2 - 13, y)], colour, dash=7, gap=6, width=3.4)
    s.poly([(x2, y), (x2 - 13, y - 8), (x2 - 13, y + 8)], fill=colour)


def icon_plug(s: Screen, cx: float, cy: float, colour: Colour) -> None:
    s.rect((cx - 3.5, cy - 11, cx + 3.5, cy - 4), r=1, fill=colour)
    s.rect((cx - 6.5, cy - 4, cx + 6.5, cy + 5), r=2.5, fill=colour)
    s.path([(cx, cy + 5), (cx, cy + 11)], colour, 2.6)


def icon_clock(s: Screen, cx: float, cy: float, colour: Colour) -> None:
    s.circle(cx, cy + 1.5, 9.5, line=colour, width=2.4)
    s.path([(cx, cy + 1.5), (cx, cy - 4)], colour, 2.2)
    s.path([(cx, cy + 1.5), (cx + 4.5, cy + 3.5)], colour, 2.2)
    s.rect((cx - 3.2, cy - 12.5, cx + 3.2, cy - 9.5), r=1, fill=colour)


def icon_eject(s: Screen, cx: float, cy: float, colour: Colour) -> None:
    s.poly([(cx - 10, cy + 2), (cx + 10, cy + 2), (cx, cy - 10)], fill=colour)
    s.rect((cx - 10, cy + 5.5, cx + 10, cy + 9), r=1, fill=colour)


def icon_power(s: Screen, cx: float, cy: float, colour: Colour) -> None:
    s.arc(cx, cy + 1, 9.5, -58, 238, colour, 2.6)
    s.path([(cx, cy - 10.5), (cx, cy - 1)], colour, 2.6)


def icon_file(s: Screen, cx: float, cy: float, colour: Colour) -> None:
    s.poly([(cx - 8, cy - 11), (cx + 3, cy - 11), (cx + 8, cy - 6), (cx + 8, cy + 11), (cx - 8, cy + 11)], line=colour, width=2.2)
    for dy in (0, 5):
        s.path([(cx - 4, cy + dy), (cx + 4, cy + dy)], colour, 1.8)


def icon_retry(s: Screen, cx: float, cy: float, colour: Colour) -> None:
    r, end = 9.0, 300.0
    s.arc(cx, cy, r, 20, end, colour, 2.5)
    ex, ey = cx + r * math.cos(math.radians(end)), cy + r * math.sin(math.radians(end))
    tx, ty = -math.sin(math.radians(end)), math.cos(math.radians(end))
    nx, ny = -ty, tx
    s.poly([(ex + 5 * tx, ey + 5 * ty), (ex - 2 * tx + 5 * nx, ey - 2 * ty + 5 * ny), (ex - 2 * tx - 5 * nx, ey - 2 * ty - 5 * ny)], fill=colour)


def icon_warn(s: Screen, cx: float, cy: float, colour: Colour) -> None:
    s.poly([(cx, cy - 11), (cx + 11, cy + 9), (cx - 11, cy + 9)], line=colour, width=2.3)
    s.path([(cx, cy - 3.5), (cx, cy + 2)], colour, 2.3)
    s.circle(cx, cy + 5.6, 1.4, fill=colour)


def connect() -> Screen:
    s = Screen(BLUE)
    port = handheld(s, *DEVICE, display=show_cards)
    screen, _ = laptop(s)
    window(s, screen, filled=False)
    cable(s, port, 180, plugged=False)
    s.text((port[0] - 12, 291), "OTG", font(SEMI, 17), SUB, "rm")
    arrow(s, 198, 242, CABLE_Y, WAIT)
    y = s.status(318, "Plug the OTG port into the PC")
    y = s.para(y, "Straight into the PC, not into a hub.", icon_plug)
    s.para(y, "Waiting up to **90 seconds**, then it turns off.", icon_clock)
    return s


def active() -> Screen:
    s = Screen(GREEN)
    port = handheld(s, *DEVICE, display=show_cards)
    screen, pc_port = laptop(s)
    window(s, screen, filled=True)
    cable(s, port, pc_port[0], plugged=True)
    badge(s, 188, CABLE_Y, 15, OK, "check")
    y = s.status(318, "Connected to the PC")
    s.para(y, "When you're done, **Safely Remove** both cards on the PC, then unplug the cable.", icon_eject)
    return s


def off() -> Screen:
    s = Screen(GREEN)
    port = handheld(s, 261, DEVICE[1], display=show_badge(OK, "check"))
    cable(s, port, 420, plugged=False)
    y = s.status(318, "All writes saved")
    s.para(y, "Powering off. **Turn the R36S on to play.**", icon_power)
    return s


def nopc() -> Screen:
    s = Screen(AMBER)
    port = handheld(s, *DEVICE, display=show_cards)
    laptop(s, ghost=True)
    cable(s, port, 180, plugged=False)
    x1, y1, x2, y2 = LID_BOX
    badge(s, (x1 + x2) / 2, (y1 + y2) / 2, 30, AMBER, "?")
    y = s.status(318, "No PC detected")
    y = s.para(y, "Powering off. **Turn the R36S on to play.**", icon_power)
    s.para(y, "Try again: **Options > Tools > USB Card Mode**.", icon_retry)
    return s


def error() -> Screen:
    s = Screen(RED)
    handheld(s, 261, DEVICE[1], display=show_badge(RED, "!"))
    y = s.status(318, "The card reader didn't start")
    y = s.para(y, "Powering off. **Turn the R36S on to play.**", icon_power)
    s.para(y, r"Details: **usbmode\r36s-usb.log** on BOOT.", icon_file)
    return s


def recovery() -> Screen:
    s = Screen(RED)
    microsd(s, 148, 104, 56, DARKOS)
    s.text((176, 196), "dArkOS card", font(SEMI, 20), SUB, "ma")
    arrow(s, 224, 272, 146, INK)
    screen, _ = laptop(s, (300, 92, 532, 230))
    x1, y1, x2, y2 = screen
    cx, top = (x1 + x2) / 2, y1 + 32
    s.poly([(cx - 21, top), (cx + 10, top), (cx + 21, top + 11), (cx + 21, top + 54), (cx - 21, top + 54)], fill=(255, 255, 255), line=SHELL_EDGE, width=1.5)
    for i, dy in enumerate((15, 25, 35, 45)):
        s.rect((cx - 12, top + dy - 1.8, cx + (12 if i % 2 else 5), top + dy + 1.8), r=1.8, fill=SKELETON)
    badge(s, cx + 21, top + 50, 10.5, RED, "!", ring=PAPER)
    s.text((cx, y2 - 9), "RECOVERY.txt", font(SEMI, 18), PAPER_INK, "ms")
    y = s.status(262, "Check before you play")
    y = s.para(y, "Powering off. boot.ini wasn't put back.", icon_warn)
    s.para(y, "If dArkOS doesn't start, put the dArkOS card in the PC and follow **USB-CARD-MODE-RECOVERY.txt**.", icon_file)
    return s


# The names r36s-usb.v7.sh and post-flash.sh ask for.
SCREENS: dict[str, Callable[[], Screen]] = {"connect": connect, "active": active, "off": off, "nopc": nopc, "error": error, "recovery": recovery}


@app.command()
def main(
    out: Annotated[Path, typer.Option(help="Folder for screens.tar")] = HERE,
    previews: Annotated[Path | None, typer.Option(help="Folder for PNG previews (not installed)")] = None,
) -> None:
    """Render the SD card reader mode screens into screens.tar."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.USTAR_FORMAT) as tar:
        for name, build in SCREENS.items():
            img = build().image()
            raw = img.tobytes("raw", "BGRX")
            assert len(raw) == W * H * 4
            data = gzip.compress(raw, compresslevel=9, mtime=0)
            info = tarfile.TarInfo(f"{name}.gz")
            info.size, info.mode, info.mtime, info.uname, info.gname = len(data), 0o644, 0, "root", "root"
            tar.addfile(info, io.BytesIO(data))
            if previews:
                previews.mkdir(parents=True, exist_ok=True)
                img.save(previews / f"{name}.png")
            typer.echo(f"{name}.gz: {len(data):,} bytes")
    (out / "screens.tar").write_bytes(buf.getvalue())
    typer.echo(f"screens.tar: {len(buf.getvalue()):,} bytes")


if __name__ == "__main__":
    app()
