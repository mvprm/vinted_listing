"""Photo loading and processing: neutral background, colour cast from the background, gentle contrast."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

import cv2
import numpy as np
import pillow_heif
from PIL import Image, ImageOps

if TYPE_CHECKING:
    from vinted_listing.config import Settings

pillow_heif.register_heif_opener()

PHOTO_EDGE = 1600
JPEG_QUALITY = 90
BACKGROUND_ALPHA = 26  # alpha below ~0.1 counts as background
MANIFEST = "manifest.json"


class BackgroundRemover(Protocol):
    def remove(self, img: Image.Image) -> Image.Image:
        """Return the image as RGBA; the alpha channel is the foreground mask."""


class RembgRemover:
    def __init__(self, model_name: str) -> None:
        import rembg  # imported lazily: heavy, and only needed for real runs

        self._rembg = rembg
        self._session = rembg.new_session(model_name)

    def remove(self, img: Image.Image) -> Image.Image:
        return self._rembg.remove(img, session=self._session).convert("RGBA")


def load_photo(path: Path) -> Image.Image:
    """Open a photo (JPEG, PNG, WebP or HEIC) upright, as RGB, without holding the file open."""
    with Image.open(path) as img:
        return ImageOps.exif_transpose(img).convert("RGB")


def background_white_balance(
    img: Image.Image, alpha: np.ndarray, max_gain: float = 1.15, min_bg_fraction: float = 0.15
) -> Image.Image:
    """Remove a colour cast using only background pixels, so the garment's own colour can't skew it."""
    background = alpha < BACKGROUND_ALPHA
    if background.mean() < min_bg_fraction:
        return img
    arr = np.asarray(img).astype(np.float32)
    channel_means = arr[background].mean(axis=0)
    gains = np.clip(channel_means.mean() / np.maximum(channel_means, 1.0), 1 / max_gain, max_gain)
    return Image.fromarray(np.clip(arr * gains, 0, 255).round().astype(np.uint8))


def equalize_luminance(img: Image.Image, clip_limit: float = 1.2, tile: int = 8) -> Image.Image:
    lab = cv2.cvtColor(np.asarray(img), cv2.COLOR_RGB2LAB)
    clahe = cv2.createCLAHE(clipLimit=clip_limit, tileGridSize=(tile, tile))
    lab[:, :, 0] = clahe.apply(lab[:, :, 0])
    return Image.fromarray(cv2.cvtColor(lab, cv2.COLOR_LAB2RGB))


def composite(img: Image.Image, alpha: np.ndarray, colour: tuple[int, int, int]) -> Image.Image:
    a = (alpha.astype(np.float32) / 255.0)[:, :, None]
    fg = np.asarray(img).astype(np.float32)
    bg = np.array(colour, np.float32)
    return Image.fromarray((fg * a + bg * (1 - a)).round().astype(np.uint8))


def process_photos(
    photos: list[Path],
    out_dir: Path,
    remover: BackgroundRemover,
    background: tuple[int, int, int],
    white_balance: bool = True,
) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    outputs = []
    for index, path in enumerate(photos, start=1):
        img = load_photo(path)
        img.thumbnail((PHOTO_EDGE, PHOTO_EDGE), Image.Resampling.LANCZOS)
        alpha = np.asarray(remover.remove(img).getchannel("A"))
        if white_balance:
            img = background_white_balance(img, alpha)
        img = equalize_luminance(img)
        out = out_dir / f"{index:02d}.jpg"
        composite(img, alpha, background).save(out, "JPEG", quality=JPEG_QUALITY)  # no exif=: strips GPS
        outputs.append(out)
    return outputs


def photos_key(photos: list[Path], settings: Settings) -> str:
    """12 hex chars over the photos (name, size) and the settings that change the processed output."""
    payload = {
        "photos": [[p.name, p.stat().st_size] for p in photos],
        "background_color": list(settings.background_color),
        "white_balance": settings.white_balance,
        "rembg_model": settings.rembg_model,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:12]


def process_photos_cached(
    name: str, photos: list[Path], settings: Settings, remover: BackgroundRemover
) -> list[Path]:
    """Processed photos for an item, reused from `<preprocessed_dir>/<name>-<key>/` when complete."""
    key = photos_key(photos, settings)
    final = settings.preprocessed_dir / f"{name}-{key}"
    cached = _read_complete_cache(final)
    if cached is not None:
        os.utime(final)  # keep active items out of prune_cache's reach
        return cached

    tmp = final.with_name(final.name + ".tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    outputs = process_photos(photos, tmp, remover, settings.background_color, settings.white_balance)
    manifest = {
        "key": key,
        "sources": [[p.name, p.stat().st_size] for p in photos],
        "background_color": list(settings.background_color),
        "white_balance": settings.white_balance,
        "rembg_model": settings.rembg_model,
        "outputs": [p.name for p in outputs],
    }
    (tmp / MANIFEST).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    shutil.rmtree(final, ignore_errors=True)
    tmp.rename(final)
    return [final / p.name for p in outputs]


def _read_complete_cache(folder: Path) -> list[Path] | None:
    try:
        manifest = json.loads((folder / MANIFEST).read_text(encoding="utf-8"))
        outputs = [folder / name for name in manifest["outputs"]]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return outputs if outputs and all(p.is_file() for p in outputs) else None


def prune_cache(preprocessed_dir: Path, max_age_days: int = 30, now: float | None = None) -> int:
    if not preprocessed_dir.is_dir():
        return 0
    cutoff = (time.time() if now is None else now) - max_age_days * 86400
    removed = 0
    for folder in preprocessed_dir.iterdir():
        if folder.is_dir() and folder.stat().st_mtime < cutoff:
            shutil.rmtree(folder, ignore_errors=True)
            removed += not folder.exists()
    return removed
