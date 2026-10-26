"""Settings, loaded from the environment and `<state_dir>/.env`."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

import dotenv

HAIKU = "claude-haiku-5-5"
SONNET = "claude-sonnet-5-5"
OPUS = "claude-opus-5-5"

STAGES = ("describe", "research", "parse", "listing")

MODEL_PROFILES: dict[str, dict[str, str]] = {
    "mixed": {"describe": HAIKU, "research": SONNET, "parse": HAIKU, "listing": SONNET},
    "opus": {stage: OPUS for stage in STAGES},
}


class ModelPrice(NamedTuple):
    """USD per million tokens."""

    input: float
    output: float
    cache_read: float


# Haiku's cache-read price is an assumption (0.1x input); verify against the pricing page.
MODEL_PRICES: dict[str, ModelPrice] = {
    OPUS: ModelPrice(4.0, 20.0, 0.20),
    SONNET: ModelPrice(2.0, 10.0, 0.20),
    HAIKU: ModelPrice(0.10, 0.50, 0.01),
}

# Models that accept the server-side refusal fallback (`fallbacks="default"`).
SERVER_FALLBACK_MODELS = frozenset({OPUS, SONNET})


@dataclass(frozen=True)
class Settings:
    drive_root: Path
    state_dir: Path
    model_describe: str
    model_research: str
    model_parse: str
    model_listing: str
    quiet_seconds: int = 120
    hint_grace_seconds: int = 1800
    poll_seconds: int = 30
    model_profile: str = "mixed"
    effort_describe: str = "low"
    effort_research: str = "medium"
    effort_parse: str = "low"
    effort_listing: str = "medium"
    listing_language: str = "pt-PT"
    rembg_model: str = "birefnet-general"
    background_color: tuple[int, int, int] = (236, 236, 236)
    white_balance: bool = True
    llm_image_edge: int = 1092
    describe_max_photos: int = 4
    draft_max_photos: int = 1
    max_searches_per_item: int = 6
    max_fetches_per_item: int = 4
    fetch_max_content_tokens: int = 8000
    monthly_budget_usd: float = 20.0
    daily_item_limit: int = 30
    price_per_search: float = 0.01
    title_max_chars: int | None = None
    google_credentials: Path | None = None

    @property
    def inbox(self) -> Path:
        return self.drive_root / "inbox"

    @property
    def ready(self) -> Path:
        return self.drive_root / "ready"

    @property
    def failed(self) -> Path:
        return self.drive_root / "failed"

    @property
    def status_file(self) -> Path:
        return self.drive_root / "status.txt"

    @property
    def db_path(self) -> Path:
        return self.state_dir / "state.db"

    @property
    def log_dir(self) -> Path:
        return self.state_dir / "logs"

    @property
    def runs_dir(self) -> Path:
        return self.state_dir / "runs"

    @property
    def preprocessed_dir(self) -> Path:
        return self.state_dir / "preprocessed"

    def model_for(self, stage: str) -> str:
        """The model for a pipeline stage. Comparables research uses the research model."""
        models = {
            "describe": self.model_describe,
            "research": self.model_research,
            "comparables": self.model_research,
            "parse": self.model_parse,
            "listing": self.model_listing,
        }
        if stage not in models:
            raise ValueError(f"unknown stage: {stage!r}")
        return models[stage]

    def effort_for(self, stage: str) -> str:
        efforts = {
            "describe": self.effort_describe,
            "research": self.effort_research,
            "comparables": self.effort_research,
            "parse": self.effort_parse,
            "listing": self.effort_listing,
        }
        if stage not in efforts:
            raise ValueError(f"unknown stage: {stage!r}")
        return efforts[stage]


def default_state_dir(env: Mapping[str, str]) -> Path:
    if env.get("VINTED_LISTING_HOME"):
        return Path(env["VINTED_LISTING_HOME"])
    base = env.get("LOCALAPPDATA") or os.environ.get("LOCALAPPDATA")
    return (Path(base) if base else Path.home() / ".local" / "share") / "vinted-listing"


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    """Build Settings from `env`, or from `<state_dir>/.env` plus the process environment."""
    if env is None:
        dotenv.load_dotenv(default_state_dir(os.environ) / ".env")
        env = os.environ

    if not env.get("DRIVE_ROOT"):
        raise ValueError("DRIVE_ROOT is not set: point it at the Vinted folder in Google Drive")

    profile = env.get("MODEL_PROFILE", "mixed")
    if profile not in MODEL_PROFILES:
        raise ValueError(f"MODEL_PROFILE must be one of {sorted(MODEL_PROFILES)}, got {profile!r}")
    models = {stage: env.get(f"MODEL_{stage.upper()}") or MODEL_PROFILES[profile][stage] for stage in STAGES}
    if "haiku" in models["research"].lower():
        raise ValueError("MODEL_RESEARCH cannot be a Haiku model: Haiku can't use the web search tools")

    defaults = Settings(drive_root=Path(), state_dir=Path(), **{f"model_{s}": "" for s in STAGES})
    google = env.get("GOOGLE_APPLICATION_CREDENTIALS")

    return Settings(
        drive_root=Path(env["DRIVE_ROOT"]),
        state_dir=default_state_dir(env),
        model_profile=profile,
        **{f"model_{stage}": model for stage, model in models.items()},
        quiet_seconds=_int(env, "QUIET_SECONDS", defaults.quiet_seconds),
        hint_grace_seconds=_int(env, "HINT_GRACE_SECONDS", defaults.hint_grace_seconds),
        poll_seconds=_int(env, "POLL_SECONDS", defaults.poll_seconds),
        effort_describe=env.get("EFFORT_DESCRIBE", defaults.effort_describe),
        effort_research=env.get("EFFORT_RESEARCH", defaults.effort_research),
        effort_parse=env.get("EFFORT_PARSE", defaults.effort_parse),
        effort_listing=env.get("EFFORT_LISTING", defaults.effort_listing),
        listing_language=env.get("LISTING_LANGUAGE", defaults.listing_language),
        rembg_model=env.get("REMBG_MODEL", defaults.rembg_model),
        background_color=_color(env, "BACKGROUND_COLOR", defaults.background_color),
        white_balance=_bool(env, "WHITE_BALANCE", defaults.white_balance),
        llm_image_edge=_int(env, "LLM_IMAGE_EDGE", defaults.llm_image_edge),
        describe_max_photos=_int(env, "DESCRIBE_MAX_PHOTOS", defaults.describe_max_photos),
        draft_max_photos=_int(env, "DRAFT_MAX_PHOTOS", defaults.draft_max_photos),
        max_searches_per_item=_int(env, "MAX_SEARCHES_PER_ITEM", defaults.max_searches_per_item),
        max_fetches_per_item=_int(env, "MAX_FETCHES_PER_ITEM", defaults.max_fetches_per_item),
        fetch_max_content_tokens=_int(env, "FETCH_MAX_CONTENT_TOKENS", defaults.fetch_max_content_tokens),
        monthly_budget_usd=_float(env, "MONTHLY_BUDGET_USD", defaults.monthly_budget_usd),
        daily_item_limit=_int(env, "DAILY_ITEM_LIMIT", defaults.daily_item_limit),
        price_per_search=_float(env, "PRICE_PER_SEARCH", defaults.price_per_search),
        title_max_chars=_int(env, "TITLE_MAX_CHARS", None),
        google_credentials=Path(google) if google else None,
    )


def _int(env: Mapping[str, str], name: str, default: int | None) -> int | None:
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        raise ValueError(f"{name} must be a whole number, got {raw!r}") from None


def _float(env: Mapping[str, str], name: str, default: float) -> float:
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        raise ValueError(f"{name} must be a number, got {raw!r}") from None


def _bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false, got {raw!r}")


def _color(env: Mapping[str, str], name: str, default: tuple[int, int, int]) -> tuple[int, int, int]:
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    value = raw.strip().lstrip("#")
    if len(value) != 6:
        raise ValueError(f"{name} must look like #RRGGBB, got {raw!r}")
    try:
        return (int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16))
    except ValueError:
        raise ValueError(f"{name} must look like #RRGGBB, got {raw!r}") from None
