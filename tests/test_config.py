import pytest

from vinted_listing.config import load_settings


def test_load_settings_defaults(tmp_path):
    s = load_settings({"DRIVE_ROOT": str(tmp_path), "VINTED_LISTING_HOME": str(tmp_path / "home")})
    assert s.quiet_seconds == 120 and s.model_describe == "claude-haiku-5-5" and s.model_listing == "claude-sonnet-5-5"
    assert s.inbox == tmp_path / "inbox" and s.db_path == tmp_path / "home" / "state.db"
    assert s.background_color == (236, 236, 236) and s.google_credentials is None
    assert s.preprocessed_dir == tmp_path / "home" / "preprocessed"


def test_opus_profile_and_stage_override(tmp_path):
    s = load_settings({"DRIVE_ROOT": str(tmp_path), "MODEL_PROFILE": "opus", "MODEL_PARSE": "claude-haiku-5-5"})
    assert s.model_research == "claude-opus-5-5" and s.model_parse == "claude-haiku-5-5"
    assert s.model_for("comparables") == s.model_research


def test_unknown_profile_names_variable(tmp_path):
    with pytest.raises(ValueError, match="MODEL_PROFILE"):
        load_settings({"DRIVE_ROOT": str(tmp_path), "MODEL_PROFILE": "cheap"})


def test_haiku_cannot_research(tmp_path):
    with pytest.raises(ValueError, match="MODEL_RESEARCH"):
        load_settings({"DRIVE_ROOT": str(tmp_path), "MODEL_RESEARCH": "claude-haiku-5-5"})


def test_load_settings_requires_drive_root():
    with pytest.raises(ValueError, match="DRIVE_ROOT"):
        load_settings({})


def test_bad_background_color_names_variable(tmp_path):
    with pytest.raises(ValueError, match="BACKGROUND_COLOR"):
        load_settings({"DRIVE_ROOT": str(tmp_path), "BACKGROUND_COLOR": "grey"})


def test_numeric_overrides_are_parsed(tmp_path):
    s = load_settings({"DRIVE_ROOT": str(tmp_path), "MONTHLY_BUDGET_USD": "35.5", "TITLE_MAX_CHARS": "60",
                       "WHITE_BALANCE": "false", "BACKGROUND_COLOR": "#EEEBE6"})
    assert s.monthly_budget_usd == 35.5 and s.title_max_chars == 60 and s.white_balance is False
    assert s.background_color == (238, 235, 230)
