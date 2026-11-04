import importlib.util
import re
from pathlib import Path

import pytest
from PIL import Image
from typer.testing import CliRunner

HERE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("render_screens", HERE / "render-screens.py")
render = importlib.util.module_from_spec(spec)
spec.loader.exec_module(render)
pytestmark = pytest.mark.skipif(not all((render.FONTS / name).is_file() for name in (render.BOLD, render.SEMI, render.REG)), reason="needs the Segoe UI fonts")


def test_screens_tar_is_current(tmp_path: Path) -> None:
    """screens.tar must be exactly what render-screens.py draws. After changing a screen, rebuild it and usbmode.md5."""
    result = CliRunner().invoke(render.app, ["--out", str(tmp_path), "--previews", str(tmp_path / "previews")])
    assert result.exit_code == 0, result.output
    assert (tmp_path / "screens.tar").read_bytes() == (HERE / "screens.tar").read_bytes()
    for name in render.SCREENS:
        with Image.open(tmp_path / "previews" / f"{name}.png") as img:
            assert img.size == (render.W, render.H)


def test_names_match_the_scripts() -> None:
    """Every screen the helper and post-flash.sh ask for exists, and every screen is used."""
    scripts = (HERE / "r36s-usb.v7.sh").read_text() + (HERE / "post-flash.boot.sh").read_text()
    used = set(re.findall(r"\b(?:r36s_)?screen (\w+)", scripts)) | set(re.findall(r"\bname=(\w+)", scripts))
    assert used == set(render.SCREENS)
