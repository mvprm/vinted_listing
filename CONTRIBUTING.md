# Contributing

Thanks for your interest. This is a small personal project, so please open an issue to discuss larger changes before
starting on them.

## Development setup

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv -e ".[dev]"
.venv/Scripts/python -m pytest        # Windows; .venv/bin/python on Linux/macOS
```

The offline suite needs no API key: the Claude client and background removal are replaced by fakes. Live tests
(`pytest -m live`) call the real API and download a model; they need `ANTHROPIC_API_KEY` and `LIVE_PHOTO` and are not
run in CI.

## Guidelines

- Write a failing test first, then the change; keep `pytest` green on Windows and Linux.
- Keep dependencies pinned through `uv.lock` (`uv lock` after editing `pyproject.toml`).
- Never commit `.env` files, API keys, personal paths or photos with location metadata.
- Commit messages follow the `type: summary` style used in the history (`feat`, `fix`, `docs`, `test`, `chore`).
- Add a line to `CHANGELOG.md` under *Unreleased* for user-visible changes.
- Prompt or pricing changes should be checked with `python -m vinted_listing eval` on a golden set.
