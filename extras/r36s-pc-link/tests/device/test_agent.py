"""Offline tests for pclink_agent.py inside the dArkOS4Clone root file system (arm64 chroot, as root; chroot-run.sh),
with dArkOS's own es_settings.cfg and es_systems.cfg, a uinput "GO-Super Gamepad" standing in for the R36S's pad, a
fake fb0, a fake ES process tree for "a game is running", and the systemctl / systemd-run mocks from fakehw.sh.

    python3 /w/tests/device/test_agent.py -v      (after: . /w/tests/device/fakehw.sh)
"""

import base64
import importlib.util
import json
import os
import select
import shutil
import signal
import struct
import subprocess
import sys
import time
import unittest
import zlib
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

W = Path("/w")
AGENT = W / "device/.pclink/pclink_agent.py"
FAKEHW = Path("/tmp/fakehw")
CONFIG = Path("/run/pclink/agent.json")
spec = importlib.util.spec_from_file_location("pclink_agent", AGENT)
agent = importlib.util.module_from_spec(spec)
spec.loader.exec_module(agent)


def run_agent(*args: str, config: dict | None = None, timeout: float = 30) -> subprocess.CompletedProcess:
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps(config or {}))
    env = dict(os.environ, PATH=f"{FAKEHW}/bin:" + os.environ["PATH"])
    return subprocess.run([sys.executable, str(AGENT), *args], check=False, capture_output=True, text=True, env=env, timeout=timeout)


def png_pixels(data: bytes) -> tuple[int, int, bytes]:
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    width, height, depth, color = struct.unpack(">IIBB", data[16:26])
    assert (depth, color) == (8, 2)
    pos, idat = 8, b""
    while pos < len(data):
        (length,) = struct.unpack(">I", data[pos : pos + 4])
        kind = data[pos + 4 : pos + 8]
        if kind == b"IDAT":
            idat += data[pos + 8 : pos + 8 + length]
        pos += 12 + length
    raw = zlib.decompress(idat)
    rows = [raw[y * (width * 3 + 1) + 1 : (y + 1) * (width * 3 + 1)] for y in range(height)]
    assert all(raw[y * (width * 3 + 1)] == 0 for y in range(height))
    return width, height, b"".join(rows)


def fake_es_with_game() -> subprocess.Popen:
    """A stand-in for emulationstation.sh in the middle of a game: the systemctl mock reports it as ES's MainPID,
    and its child (sleep) is the game."""
    script = Path("/tmp/fake-es/emulationstation.sh")
    script.parent.mkdir(exist_ok=True)
    script.write_text("#!/bin/bash\nsleep 60\nexit 0\n")
    es = subprocess.Popen(["/bin/bash", str(script)])
    (FAKEHW / "mainpid-emulationstation").write_text(f"{es.pid}\n")
    for _ in range(50):
        if agent.game_processes():
            break
        time.sleep(0.1)
    return es


class EsSettings(unittest.TestCase):
    REAL = Path("/home/ark/.emulationstation/es_settings.cfg")

    def setUp(self):
        self.saved = self.REAL.read_bytes()
        (FAKEHW / "es-active").write_text("1")
        (FAKEHW / "calls").write_text("")
        (FAKEHW / "mainpid-emulationstation").unlink(missing_ok=True)

    def tearDown(self):
        self.REAL.write_bytes(self.saved)
        for backup in self.REAL.parent.glob("es_settings.cfg.pclink-*"):
            backup.unlink()

    def test_parse_real_file(self):
        found = agent.es_parse(self.REAL.read_text())
        self.assertEqual(found["ThemeSet"], ("string", "es-theme-nes-box"))
        self.assertEqual(found["MaxVRAM"], ("int", "150"))
        self.assertEqual(found["DrawClock"], ("bool", "false"))

    def test_update_keeps_other_lines(self):
        text = self.REAL.read_text()
        new, kind = agent.es_update(text, "ThemeSet", 'x&y "q" <z>')
        self.assertEqual(kind, "string")
        self.assertEqual(agent.es_parse(new)["ThemeSet"], ("string", 'x&y "q" <z>'))
        changed = [(a, b) for a, b in zip(text.splitlines(), new.splitlines()) if a != b]
        self.assertEqual(len(changed), 1)
        self.assertIn('value="x&amp;y &quot;q&quot; &lt;z&gt;"', changed[0][1])
        self.assertEqual(len(text.splitlines()), len(new.splitlines()))

    def test_new_setting_type_and_validation(self):
        new, kind = agent.es_update(self.REAL.read_text(), "ShowHiddenFiles", "true")
        self.assertEqual(kind, "bool")
        self.assertTrue(new.rstrip().endswith('<bool name="ShowHiddenFiles" value="true" />'))
        with self.assertRaisesRegex(agent.AgentError, "int setting"):
            agent.es_update(self.REAL.read_text(), "MaxVRAM", "lots")
        with self.assertRaisesRegex(agent.AgentError, "bool setting"):
            agent.es_update(self.REAL.read_text(), "DrawClock", "yes")

    def test_set_stops_es_edits_backs_up_restarts(self):
        result = run_agent("es-set", "ScreenSaverBehavior", "black")
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertTrue(data["es_restarted"])
        self.assertEqual(agent.es_parse(self.REAL.read_text())["ScreenSaverBehavior"][1], "black")
        self.assertEqual(Path(data["backup"]).read_bytes(), self.saved)
        st = self.REAL.stat()
        self.assertEqual((st.st_uid, st.st_gid), (1000, 1000))
        calls = (FAKEHW / "calls").read_text()
        self.assertLess(calls.index("stop emulationstation"), calls.index("start emulationstation"))

    def test_set_refused_while_a_game_runs(self):
        es = fake_es_with_game()
        try:
            result = run_agent("es-set", "ScreenSaverBehavior", "black")
            self.assertEqual(result.returncode, 1)
            self.assertIn("a game is running (sleep)", result.stderr)
            self.assertNotIn("stop emulationstation", (FAKEHW / "calls").read_text())
            self.assertEqual(self.REAL.read_bytes(), self.saved)
        finally:
            es.kill()
            es.wait()

    def test_set_force_stops_the_game_first(self):
        es = fake_es_with_game()
        try:
            result = run_agent("es-set", "ScreenSaverBehavior", "black", "--force")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(es.wait(timeout=10), 0)  # its game (sleep) got SIGTERM, so the script went on and ended
            self.assertEqual(agent.es_parse(self.REAL.read_text())["ScreenSaverBehavior"][1], "black")
        finally:
            es.kill()
            es.wait()

    def test_get_list(self):
        result = run_agent("es-get", "ThemeSet")
        self.assertEqual(json.loads(result.stdout)["value"], "es-theme-nes-box")
        self.assertEqual(run_agent("es-get", "Nope").returncode, 1)
        self.assertIn("SortSystems", json.loads(run_agent("es-list").stdout))


class Launch(unittest.TestCase):
    ROM = Path("/roms/snes/Test Game (USA) [!].sfc")
    SETTINGS = Path("/home/ark/.emulationstation/es_settings.cfg")

    def setUp(self):
        self.saved = self.SETTINGS.read_bytes()
        self.ROM.parent.mkdir(parents=True, exist_ok=True)
        self.ROM.write_bytes(b"rom")
        (FAKEHW / "calls").write_text("")
        (FAKEHW / "mainpid-emulationstation").unlink(missing_ok=True)

    def tearDown(self):
        self.SETTINGS.write_bytes(self.saved)
        shutil.rmtree("/roms/snes", ignore_errors=True)
        shutil.rmtree("/roms/sufami", ignore_errors=True)

    def plan(self, *extra: str) -> dict:
        result = run_agent("launch", str(self.ROM), "--dry-run", *extra)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_default_command_like_es(self):
        esc = r"/roms/snes/Test\ Game\ \(USA\)\ \[\!\].sfc"
        plan = self.plan()
        self.assertEqual((plan["system"], plan["emulator"], plan["core"], plan["governor"]), ("snes", "retroarch", "snes9x", ""))
        self.assertEqual(plan["command"], f"sudo perfmax  {esc}; nice -n -19 /usr/local/bin/retroarch -L /home/ark/.config/retroarch/cores/snes9x_libretro.so {esc}; sudo perfnorm")
        self.assertNotIn("warning", plan)

    def test_es_escape_matches_es(self):
        self.assertEqual(agent.es_escape("/a b/c'd\"e!f$g^h&i*j(k)l{m}n[o]p?q;r<s>t"), r"/a\ b/c\'d\"e\!f\$g\^h\&i\*j\(k\)l\{m\}n\[o\]p\?q\;r\<s\>t")
        # ES's quirk: it escapes the backslash, then leaves the space after it alone (it looks already escaped)
        self.assertEqual(agent.es_escape(r"/already\ escaped"), r"/already\\ escaped")

    def test_system_settings_override(self):
        text, _ = agent.es_update(self.SETTINGS.read_text(), "snes.emulator", "retroarch32")
        text, _ = agent.es_update(text, "snes.core", "snes9x2005")
        text, _ = agent.es_update(text, "snes.governor", "performance")
        self.SETTINGS.write_text(text)
        plan = self.plan()
        self.assertEqual((plan["emulator"], plan["core"], plan["governor"]), ("retroarch32", "snes9x2005", "performance"))
        self.assertIn("/usr/local/bin/retroarch32 -L /home/ark/.config/retroarch32/cores/snes9x2005_libretro.so", plan["command"])
        self.assertTrue(plan["command"].startswith("sudo perfmax performance "))

    def test_game_metadata_override(self):
        (self.ROM.parent / "gamelist.xml").write_text(f"<gameList><game><path>./{self.ROM.name}</path><name>Test</name><core>snes9x2010</core></game></gameList>")
        self.assertEqual(self.plan()["core"], "snes9x2010")
        self.assertEqual(self.plan("--core=chimerasnes")["core"], "chimerasnes")

    def test_same_name_other_folder(self):
        rom = Path("/roms/sufami/Card.smc")
        rom.parent.mkdir(parents=True, exist_ok=True)
        rom.write_bytes(b"x")
        plan = json.loads(run_agent("launch", str(rom), "--dry-run").stdout)
        self.assertEqual((plan["system"], plan["system_path"], plan["core"]), ("snes", "/roms/sufami", "snes9x2010"))

    def test_errors(self):
        self.assertIn("isn't a file", run_agent("launch", "/roms/snes/missing.sfc", "--dry-run").stderr)
        Path("/tmp/x.sfc").write_bytes(b"x")
        self.assertIn("no system", run_agent("launch", "/tmp/x.sfc", "--dry-run").stderr)
        self.assertIn("has no emulator", run_agent("launch", str(self.ROM), "--dry-run", "--emulator=mame").stderr)
        odd = self.ROM.with_suffix(".txt")
        odd.write_bytes(b"x")
        self.assertIn("ES wouldn't list this file", json.loads(run_agent("launch", str(odd), "--dry-run").stdout)["warning"])

    def make_system(self, command: str, rom_name: str) -> tuple[Path, dict]:
        """A test system running command, in an es_systems.cfg of its own, with one ROM; ES running."""
        systems = Path("/tmp/test_es_systems.cfg")
        systems.write_text(f"<systemList><system><name>testsys</name><path>/roms/testsys/</path><extension>.tst</extension><command>{xml_escape(command)}</command></system></systemList>")
        rom = Path("/roms/testsys", rom_name)
        rom.parent.mkdir(parents=True, exist_ok=True)
        rom.write_bytes(b"x")
        self.addCleanup(shutil.rmtree, "/roms/testsys", True)
        (FAKEHW / "es-active").write_text("1")
        return rom, {"es_systems": str(systems), "game_log": "/tmp/game.log"}

    def test_launch_stops_es_runs_the_game_as_ark_then_es_returns(self):
        rom, config = self.make_system("echo started %ROM% as $(id -un) > /tmp/launched; sleep 2", "My Game (USA).tst")
        Path("/tmp/launched").unlink(missing_ok=True)

        def calls() -> list[str]:
            return (FAKEHW / "calls").read_text().splitlines()

        result = run_agent("launch", str(rom), config=config)
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertTrue(data["started"])
        self.assertIsInstance(data["pid"], int)
        lines = calls()
        unit = next(i for i, line in enumerate(lines) if line.startswith("systemd-run --unit=pclink-game "))
        self.assertIn("systemctl stop emulationstation", lines[:unit])
        self.assertIn(" -p ExecStopPost=/usr/bin/systemctl start emulationstation ", lines[unit])
        self.assertNotIn("systemctl start emulationstation", lines)  # not while the game runs
        for _ in range(100):
            if "systemctl start emulationstation" in calls():
                break
            time.sleep(0.1)
        self.assertIn("systemctl start emulationstation", calls())
        self.assertEqual(Path("/tmp/launched").read_text().strip(), "started /roms/testsys/My Game (USA).tst as ark")

    def test_launch_reports_a_game_that_ends_at_once(self):
        rom, config = self.make_system("exit 3", "Quick.tst")
        result = run_agent("launch", str(rom), config=config)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("the game ended right away", result.stderr)
        lines = (FAKEHW / "calls").read_text().splitlines()
        last_es_start = len(lines) - 1 - lines[::-1].index("systemctl start emulationstation")
        self.assertLess(lines.index("systemctl stop pclink-game"), last_es_start)  # game unit gone before ES returns

    def test_launch_leaves_es_to_a_game_unit_that_wont_stop(self):
        rom, config = self.make_system("exit 3", "Stuck.tst")
        state = FAKEHW / "state-pclink-game"
        state.write_text("deactivating\n")
        self.addCleanup(state.unlink)
        result = run_agent("launch", str(rom), config=config)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("the game ended right away", result.stderr)
        self.assertIn("pclink-game didn't stop either, so ES comes back only once it ends", result.stderr)

    def test_wrapper_brings_es_back_even_when_it_fails(self):
        result = run_agent("_game", "/nonexistent/command-file")
        self.assertEqual(result.returncode, 1)
        self.assertIn("/nonexistent/command-file", result.stderr)
        self.assertIn("systemctl start emulationstation", (FAKEHW / "calls").read_text().splitlines())

    def test_user_es_systems_wins_and_tilde_is_arks_home(self):
        user_cfg = Path("/home/ark/.emulationstation/es_systems.cfg")
        user_cfg.write_text("<systemList><system><name>snes</name><path>~/snes-roms/</path><extension>.sfc</extension><command>custom %ROM%</command></system></systemList>")
        rom = Path("/home/ark/snes-roms/Game.sfc")
        rom.parent.mkdir(exist_ok=True)
        rom.write_bytes(b"x")
        try:
            plan = json.loads(run_agent("launch", str(rom), "--dry-run").stdout)
            self.assertEqual((plan["system_path"], plan["command"]), ("/home/ark/snes-roms", "custom /home/ark/snes-roms/Game.sfc"))
        finally:
            user_cfg.unlink()
            shutil.rmtree(rom.parent)

    def test_gamelist_order_like_es(self):
        etc = Path("/etc/emulationstation/gamelists/snes/gamelist.xml")
        etc.parent.mkdir(parents=True, exist_ok=True)
        etc.write_text(f"<gameList><game><path>./{self.ROM.name}</path><core>snes9x2010</core></game></gameList>")
        try:
            self.assertEqual(self.plan()["core"], "snes9x2010")  # only /etc's list exists
            (self.ROM.parent / "gamelist.xml").write_text("<gameList></gameList>")
            self.assertEqual(self.plan()["core"], "snes9x")  # ES reads only the first list that exists
        finally:
            shutil.rmtree(etc.parent.parent)  # dArkOS4Clone has no /etc/emulationstation/gamelists

    def test_game_wrapper_runs_as_ark_with_es_environment(self):
        cmd = Path("/run/pclink/test-command")
        cmd.write_text('id -un; id -Gn; echo "$HOME|$XDG_RUNTIME_DIR|$SDL_VIDEO_EGL_DRIVER|$SDL_ASSERT|$TERM|$PWD"; exit 3')
        (FAKEHW / "calls").write_text("")
        result = run_agent("_game", str(cmd), config={"game_log": "/tmp/game.log"})
        self.assertEqual(result.returncode, 0, result.stderr)
        log = Path("/tmp/game.log").read_text().splitlines()
        self.assertEqual(log[0], "ark")
        self.assertTrue({"input", "video", "audio", "render"} <= set(log[1].split()), log[1])
        self.assertEqual(log[2], "/home/ark|/run/user/1000/|libEGL.so|always_ignore|linux|/home/ark")
        self.assertIn("[pclink] game exited with code 3", log[-1])
        self.assertIn("systemctl start emulationstation", (FAKEHW / "calls").read_text())

    def test_quit_game(self):
        es = fake_es_with_game()
        try:
            result = run_agent("quit-game")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["stopped"][0]["comm"], "sleep")
            self.assertEqual(es.wait(timeout=10), 0)
        finally:
            es.kill()
            es.wait()


class Screenshot(unittest.TestCase):
    def test_rgb_32_and_16(self):
        # 3x2, 32 bpp, pitch 16 (4 bytes padding): B G R X in memory
        px = [(10, 20, 30), (40, 50, 60), (70, 80, 90), (1, 2, 3), (4, 5, 6), (7, 8, 9)]
        data = b"".join(bytes((b, g, r, 0xFF)) for r, g, b in px[:3]) + b"PADS" + b"".join(bytes((b, g, r, 0)) for r, g, b in px[3:]) + b"PADS"
        rows = agent.to_rgb_rows({"data": data, "width": 3, "height": 2, "pitch": 16, "bpp": 32, "depth": 24})
        self.assertEqual(b"".join(rows), bytes(v for p in px for v in p))
        w, h, pixels = png_pixels(agent.png(3, 2, rows))
        self.assertEqual((w, h, pixels), (3, 2, bytes(v for p in px for v in p)))
        # RGB565: pure red, green, blue, white
        words = struct.pack("<4H", 0xF800, 0x07E0, 0x001F, 0xFFFF)
        rows = agent.to_rgb_rows({"data": words, "width": 4, "height": 1, "pitch": 8, "bpp": 16, "depth": 16})
        self.assertEqual(rows[0], bytes((255, 0, 0, 0, 255, 0, 0, 0, 255, 255, 255, 255)))
        with self.assertRaisesRegex(agent.AgentError, "unsupported"):
            agent.to_rgb_rows({"data": b"", "width": 1, "height": 1, "pitch": 3, "bpp": 24, "depth": 24})

    def test_fb0_through_the_cli(self):
        sysroot = Path("/tmp/fakesys")
        fb = sysroot / "class/graphics/fb0"
        fb.mkdir(parents=True, exist_ok=True)
        (fb / "modes").write_text("U:640x480p-60\n")
        (fb / "bits_per_pixel").write_text("32\n")
        (fb / "stride").write_text("2560\n")
        (fb / "pan").write_text("0,480\n")
        devroot = Path("/tmp/fakedev")
        devroot.mkdir(exist_ok=True)
        frame = bytearray(2560 * 960)
        for y in range(480):
            for x in (0, 639):
                frame[(480 + y) * 2560 + x * 4 : (480 + y) * 2560 + x * 4 + 4] = bytes((x % 256, y % 256, 200, 0))
        (devroot / "fb0").write_bytes(frame)
        result = run_agent("screenshot", "--base64", config={"sys": str(sysroot), "dev": str(devroot), "dri_card": "/nonexistent/card0"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("fb0 640x480 32bpp", result.stderr)
        w, h, pixels = png_pixels(base64.b64decode(result.stdout))
        self.assertEqual((w, h), (640, 480))
        self.assertEqual(pixels[(5 * 640 + 639) * 3 : (5 * 640 + 639) * 3 + 3], bytes((200, 5, 639 % 256)))
        drm_only = run_agent("screenshot", "--source", "drm", config={"dri_card": "/nonexistent/card0"})
        self.assertEqual(drm_only.returncode, 1)
        self.assertIn("drm:", drm_only.stderr)


class Buttons(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import evdev

        cls.evdev = evdev
        caps = {evdev.ecodes.EV_KEY: sorted(agent.BUTTONS.values())}
        try:
            cls.ui = evdev.UInput(caps, name="GO-Super Gamepad", vendor=0x484B, product=0x1100, version=0x100, bustype=0x19)
        except OSError as e:
            raise unittest.SkipTest(f"no uinput here ({e}); qemu-user can't pass uinput ioctls: run-native-tests.sh --buttons runs these natively") from e
        time.sleep(0.5)
        for ev in Path("/sys/class/input").glob("event*"):
            if (ev / "device/name").read_text().strip() == "GO-Super Gamepad":
                node = Path("/dev/input", ev.name)
                if not node.exists():
                    node.parent.mkdir(exist_ok=True)
                    major, minor = map(int, (ev / "dev").read_text().split(":"))
                    os.mknod(node, 0o20660, os.makedev(major, minor))
                cls.node = node
        cls.reader = evdev.InputDevice(str(cls.node))

    @classmethod
    def tearDownClass(cls):
        cls.reader.close()
        cls.ui.close()

    def events(self, wait: float = 1.0) -> list[tuple[int, int]]:
        got, deadline = [], time.monotonic() + wait
        while time.monotonic() < deadline:
            if select.select([self.reader.fd], [], [], 0.1)[0]:
                got += [(e.code, e.value) for e in self.reader.read() if e.type == self.evdev.ecodes.EV_KEY]
        return got

    def test_find_pad(self):
        self.assertEqual(agent.find_pad(), self.node)

    def test_press_sequence_and_chord(self):
        self.events(0.2)
        result = run_agent("press", "a", "b+start", "--hold-ms", "50", "--gap-ms", "50")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["pressed"], ["a", "b+start"])
        self.assertEqual(self.events(), [(0x131, 1), (0x131, 0), (0x130, 1), (0x2C1, 1), (0x2C1, 0), (0x130, 0)])

    def test_buttons_all_supported(self):
        data = json.loads(run_agent("buttons").stdout)
        self.assertEqual(data["device"], str(self.node))
        self.assertTrue(all(b["supported"] for b in data["buttons"].values()))

    def test_interrupted_hold_releases(self):
        self.events(0.2)
        env = dict(os.environ, PATH=f"{FAKEHW}/bin:" + os.environ["PATH"])
        CONFIG.write_text("{}")
        proc = subprocess.Popen([sys.executable, str(AGENT), "press", "fn+x", "--hold-ms", "10000"], stderr=subprocess.PIPE, text=True, env=env)
        time.sleep(1.5)
        proc.send_signal(signal.SIGTERM)
        _, err = proc.communicate(timeout=10)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("interrupted", err)
        self.assertEqual(self.events(), [(0x2C4, 1), (0x133, 1), (0x133, 0), (0x2C4, 0)])

    def test_release_all_and_unknown(self):
        self.events(0.2)
        fd = os.open(self.node, os.O_WRONLY)
        os.write(fd, agent.event(agent.EV_KEY, 0x220, 1) + agent.event(0, 0, 0))
        os.close(fd)
        result = run_agent("release-all")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.events(), [(0x220, 1), (0x220, 0)])
        bad = run_agent("press", "a+turbo")
        self.assertEqual(bad.returncode, 1)
        self.assertIn("unknown button", bad.stderr)


class Board(unittest.TestCase):
    def test_button_codes_match_the_board_file(self):
        root = Path("/tmp/fakeproc/type2-buttons")
        subprocess.run([sys.executable, str(W / "tests/device/dtb2fs.py"), str(W / "tests/device/fixtures/darkos-type2-panel1-amp.dtb"), str(root)], check=True, capture_output=True)
        pad = root / "odroidgo3-joypad"
        by_label = {
            "GPIO DPAD-UP": "up",
            "GPIO DPAD-DOWN": "down",
            "GPIO DPAD-LEFT": "left",
            "GPIO DPAD-RIGHT": "right",
            "GPIO BTN-A": "a",
            "GPIO BTN-B": "b",
            "GPIO BTN-X": "x",
            "GPIO BTN-Y": "y",
            "GPIO BTN_TL": "l1",
            "GPIO BTN_TR": "r1",
            "GPIO BTN_TL2": "l2",
            "GPIO BTN_TR2": "r2",
            "BTN_SELECT": "select",
            "BTN_START": "start",
            "BTN_THUMBL": "l3",
            "BTN_THUMBR": "r3",
            "GPIO BTN_F": "fn",
        }
        codes = {by_label[(sw / "label").read_bytes().rstrip(b"\0").decode()]: struct.unpack(">I", (sw / "linux,code").read_bytes())[0] for sw in pad.glob("sw*")}
        self.assertEqual(codes, agent.BUTTONS)
        self.assertEqual((pad / "joypad-name").read_bytes().rstrip(b"\0").decode(), agent.DEFAULTS["pad_name"])

    def test_board_ok_follows_the_board_file(self):
        for name, want in (("darkos-type2-panel1-amp.dtb", True), ("emuelec-card-mode.dtb", False)):
            if not (W / "tests/device/fixtures" / name).is_file():
                continue  # emuelec-card-mode.dtb isn't in the repository: see README.md, Tests
            root = Path("/tmp/fakeproc", name)
            subprocess.run([sys.executable, str(W / "tests/device/dtb2fs.py"), str(W / "tests/device/fixtures" / name), str(root / "device-tree")], check=True, capture_output=True)
            agent.CFG["proc"] = str(root)
            try:
                self.assertEqual(agent.board_ok(), want, name)
            finally:
                agent.CFG["proc"] = "/proc"


class StatusAndLogs(unittest.TestCase):
    def test_status_json(self):
        run_dir = Path("/run/pclink")
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "status").write_text("mode=usb\nstate=connected\npc=10.36.36.12\n")
        (FAKEHW / "es-active").write_text("1")
        result = run_agent("status", config={"dri_card": "/nonexistent/card0", "devmem": str(FAKEHW / "bin/devmem")})
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(data["link"], {"state": "off", "last": "connected"})
        self.assertEqual(data["es"], "running")
        self.assertEqual(data["game"], [])
        self.assertIn("error", data["display"])
        self.assertIsInstance(data["uptime_s"], int)

    def test_logs(self):
        run_dir = Path("/run/pclink")
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "pclinkd.log").write_text("\n".join(f"line {i}" for i in range(100)) + "\n")
        result = run_agent("logs", "pclink", "--lines", "3")
        self.assertIn("== pclinkd.log\nline 97\nline 98\nline 99", result.stdout)
        self.assertIn("== sshd.log", result.stdout)


if __name__ == "__main__":
    unittest.main()
