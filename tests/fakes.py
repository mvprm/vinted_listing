"""Shared test doubles and builders."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import numpy as np
from PIL import Image

from vinted_listing.config import Settings, load_settings


def make_settings(tmp_path: Path, **overrides) -> Settings:
    base = load_settings({"DRIVE_ROOT": str(tmp_path / "drive"), "VINTED_LISTING_HOME": str(tmp_path / "home")})
    return dataclasses.replace(base, **overrides)


class FakeRemover:
    """Marks the left half of every image as background (alpha 0) and the right half as foreground."""

    def __init__(self) -> None:
        self.calls = 0

    def remove(self, img: Image.Image) -> Image.Image:
        self.calls += 1
        alpha = np.full((img.height, img.width), 255, np.uint8)
        alpha[:, : img.width // 2] = 0
        rgba = img.convert("RGBA")
        rgba.putalpha(Image.fromarray(alpha))
        return rgba


def write_photo(path: Path, size: tuple[int, int] = (400, 300), colour: tuple[int, int, int] = (120, 80, 60)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, colour).save(path, "JPEG", quality=95)
    return path
