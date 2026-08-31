from pathlib import Path
import re

import yaml


ROOT = Path(__file__).resolve().parents[2]
SKIN_PATH = ROOT / "skins" / "jk-spaceduck.yaml"
PURPLE_GRADIENT = {"#d8c8ff", "#b3a1e6", "#7a5ccc", "#686f9a"}
RICH_COLOR = re.compile(r"\[(?:bold )?(#[0-9a-fA-F]{6})\]")


def test_jk_spaceduck_banner_art_uses_only_the_purple_gradient():
    skin = yaml.safe_load(SKIN_PATH.read_text(encoding="utf-8"))

    for key in ("banner_logo", "banner_hero"):
        colors = {color.lower() for color in RICH_COLOR.findall(skin[key])}
        assert colors
        assert colors <= PURPLE_GRADIENT
