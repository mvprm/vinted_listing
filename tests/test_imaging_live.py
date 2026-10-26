"""Live check of the real background remover. Run: LIVE_PHOTO=path\\to\\photo.jpg pytest -m live tests/test_imaging_live.py

The first run downloads the rembg model (BiRefNet is about 1 GB). If it is too slow on CPU, set REMBG_MODEL=u2net.
"""

import os
from pathlib import Path

import numpy as np
import pytest

from vinted_listing.imaging import RembgRemover, load_photo

pytestmark = pytest.mark.live


def test_rembg_separates_foreground_and_background():
    photo = os.environ.get("LIVE_PHOTO")
    if not photo:
        pytest.skip("set LIVE_PHOTO to a real product photo")
    img = load_photo(Path(photo))
    img.thumbnail((1600, 1600))
    out = RembgRemover(os.environ.get("REMBG_MODEL", "birefnet-general")).remove(img)
    alpha = np.asarray(out.getchannel("A"))
    assert out.mode == "RGBA" and (alpha < 26).any() and (alpha > 230).any()
