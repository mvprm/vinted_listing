"""Claude client: structured parsing and web research, with refusal fallback and usage recording."""

from __future__ import annotations

import base64
import io
from pathlib import Path
from typing import Any, Protocol, TypeVar

from vinted_listing.config import SERVER_FALLBACK_MODELS, Settings
from vinted_listing.imaging import load_photo
from vinted_listing.models import ResearchResult
from vinted_listing.usage import UsageLedger, server_tool_counts

T = TypeVar("T")

MAX_TOKENS = 16000
MAX_RESUMES = 3
FALLBACK_BETA = "server-side-fallback-2026-07-01"
VINTED_DOMAINS = (
    "vinted.pt", "vinted.es", "vinted.fr", "vinted.it", "vinted.de", "vinted.nl", "vinted.be",
    "vinted.co.uk", "vinted.com", "vinted.at", "vinted.pl", "vinted.lt", "vinted.cz", "vinted.lu",
)


class LLMError(Exception):
    pass


class LLMRefusal(LLMError):
    pass


class LLMClient(Protocol):
    def parse(self, output_type: type[T], system: str, content: list[dict], *, stage: str, effort: str) -> T: ...

    def research(
        self, system: str, content: list[dict], *, stage: str, effort: str, max_searches: int, max_fetches: int
    ) -> ResearchResult: ...


def image_block(path: Path, max_edge: int) -> dict:
    img = load_photo(path)
    img.thumbnail((max_edge, max_edge))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=85)
    data = base64.b64encode(buf.getvalue()).decode("ascii")
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": data}}


def _get(obj: Any, name: str) -> Any:
    return obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)


def extract_urls(content_blocks) -> tuple[list[str], list[str]]:
    """(cited, retrieved): URLs cited in text, and URLs returned by web search / fetch results."""
    cited: dict[str, None] = {}
    retrieved: dict[str, None] = {}
    for block in content_blocks:
        kind = _get(block, "type")
        content = _get(block, "content")
        if kind == "text":
            for citation in _get(block, "citations") or []:
                if url := _get(citation, "url"):
                    cited[url] = None
        elif kind == "web_search_tool_result" and isinstance(content, list):  # an error result is an object
            for result in content:
                if url := _get(result, "url"):
                    retrieved[url] = None
        elif kind == "web_fetch_tool_result" and content is not None:
            if url := _get(content, "url"):
                retrieved[url] = None
    return list(cited), list(retrieved)


def _text(content_blocks) -> str:
    return "\n".join(_get(b, "text") for b in content_blocks if _get(b, "type") == "text" and _get(b, "text"))


def _check_refusal(response: Any, stage: str) -> None:
    if response.stop_reason == "refusal":
        details = getattr(response, "stop_details", None)
        category = getattr(details, "category", None)
        raise LLMRefusal(f"{stage}: Claude declined the request (category: {category or 'unknown'})")


class ClaudeClient:
    def __init__(self, settings: Settings, ledger: UsageLedger, api: Any = None) -> None:
        self.settings = settings
        self.ledger = ledger
        if api is None:
            import anthropic

            api = anthropic.Anthropic().beta.messages
        self._api = api

    def _common(self, stage: str, effort: str) -> dict:
        model = self.settings.model_for(stage)
        kwargs: dict[str, Any] = {"model": model, "max_tokens": MAX_TOKENS, "output_config": {"effort": effort}}
        if model in SERVER_FALLBACK_MODELS:  # Haiku has no server-side fallback
            kwargs["betas"] = [FALLBACK_BETA]
            kwargs["fallbacks"] = "default"
        return kwargs

    def parse(self, output_type: type[T], system: str, content: list[dict], *, stage: str, effort: str) -> T:
        kwargs = self._common(stage, effort)
        response = self._api.parse(
            system=system, messages=[{"role": "user", "content": content}], output_format=output_type, **kwargs
        )
        self.ledger.record(stage, kwargs["model"], response.usage)
        _check_refusal(response, stage)
        if response.parsed_output is None:
            raise LLMError(f"{stage}: no structured output (stop_reason={response.stop_reason})")
        return response.parsed_output

    def research(
        self, system: str, content: list[dict], *, stage: str, effort: str, max_searches: int, max_fetches: int
    ) -> ResearchResult:
        kwargs = self._common(stage, effort)
        tools: list[dict] = [{"type": "web_search_20260209", "name": "web_search", "max_uses": max_searches}]
        if max_fetches > 0:
            tools.append({
                "type": "web_fetch_20260209", "name": "web_fetch", "max_uses": max_fetches,
                "citations": {"enabled": True}, "blocked_domains": list(VINTED_DOMAINS),
                "max_content_tokens": self.settings.fetch_max_content_tokens,
            })

        assistant: list = []
        searches = fetches = 0
        for _ in range(1 + MAX_RESUMES):
            messages = [{"role": "user", "content": content}]
            if assistant:  # resume a paused turn: resend what the assistant produced so far
                messages.append({"role": "assistant", "content": assistant})
            with self._api.stream(system=system, messages=messages, tools=tools, **kwargs) as stream:
                response = stream.get_final_message()
            self.ledger.record(stage, kwargs["model"], response.usage)
            _check_refusal(response, stage)
            assistant = assistant + list(response.content)
            used_searches, used_fetches = server_tool_counts(response.usage)
            searches += used_searches
            fetches += used_fetches
            if response.stop_reason != "pause_turn" or searches >= max_searches or fetches >= max_fetches:
                break

        cited, retrieved = extract_urls(assistant)
        return ResearchResult(text=_text(assistant), cited_urls=cited, retrieved_urls=retrieved,
                              searches=searches, fetches=fetches)
