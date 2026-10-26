"""Live check of item description against the real API (costs a fraction of a cent).

Run: set ANTHROPIC_API_KEY and LIVE_PHOTO, then `pytest -m live tests/test_llm_live.py`.
"""

import os
from pathlib import Path

import pytest

from fakes import make_settings
from vinted_listing.describe import describe_item
from vinted_listing.llm import ClaudeClient
from vinted_listing.usage import UsageLedger

pytestmark = pytest.mark.live


def test_describe_real_photo(tmp_path):
    photo = os.environ.get("LIVE_PHOTO")
    if not photo or not os.environ.get("ANTHROPIC_API_KEY"):
        pytest.skip("set ANTHROPIC_API_KEY and LIVE_PHOTO")
    settings = make_settings(tmp_path)
    ledger = UsageLedger(settings.db_path, settings)
    with ledger.item_scope("live"):
        desc = describe_item([Path(photo)], ClaudeClient(settings, ledger), settings)
    assert desc.item_type.strip()
    assert ledger.month_summary()["calls"] == 1
