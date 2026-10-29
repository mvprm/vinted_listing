import json

from fakes import make_settings
from vinted_listing.health import check_health, write_heartbeat


def test_fresh_heartbeat_is_healthy(tmp_path):
    path = tmp_path / "heartbeat.json"
    write_heartbeat(path, drive_ready=True, now=1000.0)
    ok, message = check_health(path, max_age=600, now=1300.0)
    assert ok and "300s ago" in message
    assert json.loads(path.read_text(encoding="utf-8"))["drive_ready"] is True


def test_stale_heartbeat_is_unhealthy(tmp_path):
    path = tmp_path / "heartbeat.json"
    write_heartbeat(path, drive_ready=True, now=1000.0)
    ok, message = check_health(path, max_age=600, now=1601.0)
    assert not ok and "601s ago" in message


def test_missing_heartbeat_is_unhealthy(tmp_path):
    ok, message = check_health(tmp_path / "nope.json", max_age=600, now=0.0)
    assert not ok and "no heartbeat" in message


def test_unreachable_drive_is_unhealthy(tmp_path):
    path = tmp_path / "heartbeat.json"
    write_heartbeat(path, drive_ready=False, now=1000.0)
    ok, message = check_health(path, max_age=600, now=1010.0)
    assert not ok and "Drive folder not reachable" in message


def test_corrupt_heartbeat_is_unhealthy(tmp_path):
    path = tmp_path / "heartbeat.json"
    path.write_text("{half", encoding="utf-8")
    assert check_health(path, max_age=600, now=0.0)[0] is False


def test_settings_heartbeat_location(tmp_path):
    settings = make_settings(tmp_path)
    assert settings.heartbeat_file == settings.state_dir / "heartbeat.json"
