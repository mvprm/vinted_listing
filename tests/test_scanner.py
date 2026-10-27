import pytest

from vinted_listing.scanner import InboxScanner


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


@pytest.fixture
def clock():
    return Clock()


def make_item(inbox, name, files):
    item = inbox / name
    item.mkdir(parents=True, exist_ok=True)
    for fname, content in files.items():
        (item / fname).write_bytes(content)
    return item


def scanner(inbox, clock):
    return InboxScanner(inbox, quiet_seconds=120, hint_grace_seconds=1800, clock=clock)


def test_folder_ready_only_after_quiet_period(tmp_path, clock):
    item = make_item(tmp_path, "levis-501", {"a.jpg": b"x", "hint.txt": b"size: M"})
    sc = scanner(tmp_path, clock)
    assert sc.scan().waiting == [(item, "uploading")]
    clock.t = 119
    assert sc.scan().ready == []
    clock.t = 121
    ready = sc.scan().ready
    assert [b.folder for b in ready] == [item] and ready[0].hint_file == item / "hint.txt"
    assert len(ready[0].snapshot_hash) == 16


def test_without_txt_waits_for_grace_period(tmp_path, clock):
    item = make_item(tmp_path, "toy", {"a.jpg": b"x"})
    sc = scanner(tmp_path, clock)
    sc.scan()
    clock.t = 121
    assert sc.scan().waiting == [(item, "waiting for hint.txt")]
    clock.t = 1801
    ready = sc.scan().ready
    assert [b.folder for b in ready] == [item] and ready[0].hint_file is None


def test_late_hint_resets_quiet_period(tmp_path, clock):
    item = make_item(tmp_path, "toy", {"a.jpg": b"x"})
    sc = scanner(tmp_path, clock)
    sc.scan()
    clock.t = 600
    (item / "hint.txt").write_text("brand: LEGO")
    assert sc.scan().waiting == [(item, "uploading")]
    clock.t = 719
    assert sc.scan().ready == []
    clock.t = 721
    ready = sc.scan().ready
    assert ready[0].hint_file == item / "hint.txt"


def test_new_file_resets_quiet_period(tmp_path, clock):
    item = make_item(tmp_path, "levis", {"a.jpg": b"x", "hint.txt": b"size: M"})
    sc = scanner(tmp_path, clock)
    sc.scan()
    clock.t = 100
    (item / "b.jpg").write_bytes(b"y")
    sc.scan()
    clock.t = 121
    assert sc.scan().ready == []
    clock.t = 221
    assert [p.name for p in sc.scan().ready[0].photos] == ["a.jpg", "b.jpg"]


def test_growing_file_resets_quiet_period(tmp_path, clock):
    item = make_item(tmp_path, "levis", {"a.jpg": b"x", "hint.txt": b"size: M"})
    sc = scanner(tmp_path, clock)
    sc.scan()
    clock.t = 100
    (item / "a.jpg").write_bytes(b"xxxx")  # still uploading: size changed
    sc.scan()
    clock.t = 121
    assert sc.scan().ready == []


def test_zero_byte_file_keeps_waiting(tmp_path, clock):
    item = make_item(tmp_path, "levis", {"a.jpg": b"", "hint.txt": b"size: M"})
    sc = scanner(tmp_path, clock)
    sc.scan()
    clock.t = 5000
    assert sc.scan().waiting == [(item, "uploading")]


def test_ignores_junk_and_sorts_photos(tmp_path, clock):
    item = make_item(tmp_path, "levis", {"desktop.ini": b"x", "x.tmp": b"x", "error.txt": b"step: x",
                                         "B.JPG": b"x", "a.heic": b"x", "hint.txt": b"size: M"})
    sc = scanner(tmp_path, clock)
    sc.scan()
    (item / "y.tmp").write_bytes(b"temp file appearing must not reset the timer")
    clock.t = 121
    batch = sc.scan().ready[0]
    assert [p.name for p in batch.photos] == ["a.heic", "B.JPG"] and batch.hint_file.name == "hint.txt"


def test_any_single_txt_is_the_hint(tmp_path, clock):
    item = make_item(tmp_path, "levis", {"a.jpg": b"x", "Note 2026-10-26.txt": b"brand: x"})
    sc = scanner(tmp_path, clock)
    sc.scan()
    clock.t = 121
    assert sc.scan().ready[0].hint_file == item / "Note 2026-10-26.txt"


def test_gdoc_without_txt_warns(tmp_path, clock):
    make_item(tmp_path, "levis", {"a.jpg": b"x", "hint.gdoc": b"{}"})
    sc = scanner(tmp_path, clock)
    sc.scan()
    clock.t = 1801
    batch = sc.scan().ready[0]
    assert batch.hint_file is None and any("Google Doc" in w for w in batch.warnings)


def test_several_txt_warns(tmp_path, clock):
    make_item(tmp_path, "levis", {"a.jpg": b"x", "a.txt": b"x", "b.txt": b"y"})
    sc = scanner(tmp_path, clock)
    sc.scan()
    clock.t = 121
    batch = sc.scan().ready[0]
    assert batch.hint_file is None and batch.warnings == ["several .txt files; name the hint hint.txt"]


def test_folder_without_photos_is_empty_not_ready(tmp_path, clock):
    item = make_item(tmp_path, "clip", {"clip.mp4": b"x"})
    sc = scanner(tmp_path, clock)
    sc.scan()
    clock.t = 121
    result = sc.scan()
    assert result.empty == [item] and result.ready == []


def test_loose_files_reported(tmp_path, clock):
    (tmp_path / "stray.jpg").write_bytes(b"x")
    (tmp_path / "desktop.ini").write_bytes(b"x")
    assert scanner(tmp_path, clock).scan().loose_files == [tmp_path / "stray.jpg"]


def test_forget_restarts_the_quiet_period(tmp_path, clock):
    item = make_item(tmp_path, "levis", {"a.jpg": b"x", "hint.txt": b"size: M"})
    sc = scanner(tmp_path, clock)
    sc.scan()
    clock.t = 121
    assert sc.scan().ready
    sc.forget(item)
    assert sc.scan().waiting == [(item, "uploading")]


def test_snapshot_hash_ignores_junk_and_changes_with_content(tmp_path, clock):
    item = make_item(tmp_path, "levis", {"a.jpg": b"x", "hint.txt": b"size: M"})
    sc = scanner(tmp_path, clock)
    sc.scan()
    clock.t = 121
    first = sc.scan().ready[0].snapshot_hash
    (item / "error.txt").write_text("step: research")
    clock.t = 300
    assert sc.scan().ready[0].snapshot_hash == first
    (item / "b.jpg").write_bytes(b"y")
    sc.scan()
    clock.t = 500
    assert sc.scan().ready[0].snapshot_hash != first


def test_missing_inbox_is_empty_result(tmp_path, clock):
    result = scanner(tmp_path / "nope", clock).scan()
    assert (result.ready, result.empty, result.waiting, result.loose_files) == ([], [], [], [])
