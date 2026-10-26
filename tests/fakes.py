"""Shared test doubles and builders."""

from __future__ import annotations

import dataclasses
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
from PIL import Image

from vinted_listing.config import Settings, load_settings
from vinted_listing.models import ResearchResult


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


def usage(input_tokens=1000, output_tokens=100, cache_read=0, searches=0, fetches=0, fallback=False):
    """Duck-typed stand-in for the SDK's BetaUsage."""
    iterations = [SimpleNamespace(type="fallback_message", model="claude-opus-5")] if fallback else None
    return SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_input_tokens=cache_read,
        cache_creation_input_tokens=0,
        server_tool_use=SimpleNamespace(web_search_requests=searches, web_fetch_requests=fetches),
        iterations=iterations,
    )


class FakeLLM:
    """LLMClient double: canned results per output type / research call, records every call."""

    def __init__(self, parse_results: dict[type, Any] | None = None,
                 research_results: list[ResearchResult] | None = None, raise_on: str | None = None) -> None:
        self.parse_results = parse_results or {}
        self.research_results = list(research_results or [])
        self.raise_on = raise_on
        self.calls: list[dict] = []

    def _record(self, kind: str, stage: str, system: str, content: list[dict], **extra) -> None:
        self.calls.append({"kind": kind, "stage": stage, "system": system, "content": content, **extra})
        if self.raise_on in (kind, stage):
            raise RuntimeError(f"FakeLLM raising on {self.raise_on}")

    def parse(self, output_type, system, content, *, stage, effort):
        self._record("parse", stage, system, content, effort=effort, output_type=output_type)
        result = self.parse_results[output_type]
        return result() if callable(result) and not isinstance(result, type) else result

    def research(self, system, content, *, stage, effort, max_searches, max_fetches):
        self._record("research", stage, system, content, effort=effort,
                     max_searches=max_searches, max_fetches=max_fetches)
        if self.research_results:
            return self.research_results.pop(0)
        return ResearchResult(text="", cited_urls=[], retrieved_urls=[], searches=0, fetches=0)
