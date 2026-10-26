import hashlib
import os
import time

import numpy as np
import pillow_heif
from PIL import Image

from fakes import FakeRemover, make_settings, write_photo
from vinted_listing.imaging import (
    background_white_balance,
    load_photo,
    photos_key,
    process_photos,
    process_photos_cached,
    prune_cache,
)


def split_image(bg, fg, size=(10, 10)):
    """Left half background (alpha 0) in colour `bg`, right half foreground (alpha 255) in colour `fg`."""
    w, h = size
    arr = np.zeros((h, w, 3), np.uint8)
    arr[:, : w // 2] = bg
    arr[:, w // 2 :] = fg
    alpha = np.full((h, w), 255, np.uint8)
    alpha[:, : w // 2] = 0
    return Image.fromarray(arr), alpha


def test_exif_rotation_applied(tmp_path):
    exif = Image.Exif()
    exif[0x0112] = 6  # Orientation: rotate 90 CW
    p = tmp_path / "rot.jpg"
    Image.new("RGB", (40, 20), (10, 20, 30)).save(p, "JPEG", exif=exif)
    assert load_photo(p).size == (20, 40)


def test_heic_is_readable(tmp_path):
    pillow_heif.register_heif_opener()
    p = tmp_path / "phone.heic"
    Image.new("RGB", (64, 48), (200, 10, 10)).save(p, format="HEIF")
    img = load_photo(p)
    assert img.mode == "RGB" and img.size == (64, 48)


def test_white_balance_uses_background_and_keeps_garment_colour():
    img, alpha = split_image(bg=(200, 200, 235), fg=(180, 40, 40))
    out = np.asarray(background_white_balance(img, alpha)).astype(int)
    bg, fg = out[:, :5].reshape(-1, 3).mean(0), out[:, -5:].reshape(-1, 3).mean(0)
    assert bg.max() - bg.min() < 5  # cast removed (gains within the clamp here)
    assert fg[0] > 3 * fg[1]  # garment still clearly red


def test_white_balance_gains_are_clamped():
    img, alpha = split_image(bg=(100, 100, 250), fg=(50, 50, 50))
    out = np.asarray(background_white_balance(img, alpha)).astype(int)
    assert out[0, 0, 2] >= int(250 / 1.15) - 1
    assert out[0, 0, 0] <= int(100 * 1.15) + 1


def test_white_balance_skipped_without_enough_background():
    img = Image.new("RGB", (10, 10), (100, 100, 180))
    alpha = np.full((10, 10), 255, np.uint8)
    assert np.array_equal(np.asarray(background_white_balance(img, alpha)), np.asarray(img))


def test_process_photos_outputs(tmp_path):
    exif = Image.Exif()
    exif[0x0110] = "PhoneModel"
    big = tmp_path / "in" / "IMG_1.jpg"
    big.parent.mkdir()
    Image.new("RGB", (3000, 2000), (120, 80, 60)).save(big, "JPEG", exif=exif)
    small = write_photo(tmp_path / "in" / "IMG_2.jpg", (300, 200))
    remover = FakeRemover()

    out = process_photos([big, small], tmp_path / "o", remover, background=(236, 236, 236))

    assert [p.name for p in out] == ["01.jpg", "02.jpg"]
    first = Image.open(out[0])
    assert max(first.size) == 1600
    assert all(abs(c - 236) <= 3 for c in first.getpixel((5, 5)))  # transparent -> neutral grey
    assert len(first.getexif()) == 0  # no EXIF (no GPS)
    assert max(Image.open(out[1]).size) == 300  # small photos are not upscaled
    assert remover.calls == 2


def test_originals_untouched(tmp_path):
    photos = [write_photo(tmp_path / "in" / f"IMG_{i}.jpg") for i in range(2)]
    before = [hashlib.sha256(p.read_bytes()).hexdigest() for p in photos]
    process_photos(photos, tmp_path / "o", FakeRemover(), background=(236, 236, 236))
    assert [hashlib.sha256(p.read_bytes()).hexdigest() for p in photos] == before


def test_cache_hit_skips_processing(tmp_path):
    s = make_settings(tmp_path)
    photos = [write_photo(tmp_path / "in" / f"IMG_{i}.jpg") for i in range(2)]
    remover = FakeRemover()
    first = process_photos_cached("levis", photos, s, remover)
    second = process_photos_cached("levis", photos, s, remover)
    assert first == second and remover.calls == 2  # second call served from cache
    assert first[0].parent.parent == s.preprocessed_dir and first[0].parent.name.startswith("levis-")


def test_cache_key_changes_with_photos_and_settings(tmp_path):
    s = make_settings(tmp_path)
    photos = [write_photo(tmp_path / "in" / "IMG_1.jpg")]
    key = photos_key(photos, s)
    assert photos_key(photos, make_settings(tmp_path, background_color=(238, 235, 230))) != key
    assert photos_key(photos, make_settings(tmp_path, white_balance=False)) != key
    write_photo(photos[0], size=(800, 600))  # replaced photo -> new size
    assert photos_key(photos, s) != key


def test_incomplete_cache_is_rebuilt(tmp_path):
    s = make_settings(tmp_path)
    photos = [write_photo(tmp_path / "in" / "IMG_1.jpg")]
    stale = s.preprocessed_dir / f"levis-{photos_key(photos, s)}"
    stale.mkdir(parents=True)
    (stale / "01.jpg").write_bytes(b"half-written")
    remover = FakeRemover()
    out = process_photos_cached("levis", photos, s, remover)
    assert remover.calls == 1 and (stale / "manifest.json").exists()
    assert Image.open(out[0]).size == (400, 300)


def test_prune_cache_removes_old_folders(tmp_path):
    root = tmp_path / "preprocessed"
    old, recent, old_tmp = root / "a-111", root / "b-222", root / "c-333.tmp"
    for d in (old, recent, old_tmp):
        d.mkdir(parents=True)
    now = time.time()
    os.utime(old, (now - 31 * 86400, now - 31 * 86400))
    os.utime(old_tmp, (now - 31 * 86400, now - 31 * 86400))
    os.utime(recent, (now - 1 * 86400, now - 1 * 86400))
    assert prune_cache(root, max_age_days=30, now=now) == 2
    assert not old.exists() and not old_tmp.exists() and recent.exists()


def test_prune_cache_missing_dir_is_noop(tmp_path):
    assert prune_cache(tmp_path / "nope") == 0
