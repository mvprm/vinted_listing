# Changelog

All notable changes to this project are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- Docker image and `docker-compose.yml` for running the watcher in a container (non-root, health check, model cache
  volume), with a CI job that builds and smoke-tests the image.
- Graceful shutdown on SIGTERM: the item in progress finishes and the rest wait for the next start.
- `health` command, backed by a heartbeat file the watcher refreshes on every poll and after every item.
- Security policy, contributing guide, changelog, issue templates and Dependabot configuration.

## [0.1.0] - 2026-10-28

First public version.

### Added

- Photo processing: HEIC and EXIF-rotation support, background removal (rembg/BiRefNet), optional colour-cast
  correction from the background, gentle contrast, neutral background, EXIF stripped, processed-photo cache.
- Claude pipeline with a model per stage (`mixed` and `opus` profiles): item description from the original photos,
  web research on the brand's site first, second-hand comparables, schema-validated specs with source checking,
  listing draft in the configured language.
- Deterministic price suggestion from comparables or retail price, with a printed rationale.
- `hint.txt` support for tag information typed on the phone; research is skipped when the hint is complete.
- SQLite usage ledger with cost estimates, monthly budget, daily item limit and per-item web search caps.
- Google Drive inbox watcher: upload-stability check, late-hint grace period, `ready/` and `failed/` delivery with
  retries for Drive file locks, no reprocess loops, `status.txt` readable from the phone, single-instance lock.
- `run`, `watch`, `costs`, `eval` and `eval-compare` commands; golden-set evaluation comparing model profiles.
- Offline test suite on Ubuntu and Windows in CI.

[Unreleased]: https://github.com/mvprm/vinted_listing/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/mvprm/vinted_listing/releases/tag/v0.1.0
