# Design

This document explains how vinted-listing is built and why. For setup and usage see the [README](../README.md).

## Goals and non-goals

**Goal:** turn a folder of product photos (plus an optional short hint typed from the item's tag) into a ready-to-post
second-hand listing package, with as little typing on the phone as possible.

**Non-goals:**

- No interaction with the marketplace: no API calls, scraping, automated posting or statistics. The marketplace's
  terms restrict third-party automation, and the slow part of listing (writing, researching, pricing, cleaning photos)
  does not need it. Posting a prepared package by hand takes a few minutes.
- No reselling workflow; this is built for selling one's own items.
- No generative photo "enhancement": edits must never change or hide the product.

## Architecture

```
src/vinted_listing/
  config.py      Settings from <state_dir>/.env; model profiles and prices
  models.py      Pydantic models: hint, visual description, sourced values, specs, listing
  hints.py       hint.txt parser (never raises; bad values become warnings)
  imaging.py     photo loading (HEIC, EXIF rotation), background removal, colour, cache
  llm.py         Claude client: structured parsing, web research, refusal fallback, usage recording
  usage.py       SQLite usage ledger, cost estimates, budget guard
  describe.py    what the photos show
  specs.py       spec research, comparables, parsing, source enforcement
  pricing.py     deterministic price suggestion
  listing.py     draft (LLM), finalize (deterministic), render listing.txt
  pipeline.py    build one package; Drive orchestration (ready/failed, retries, moves)
  scanner.py     inbox scanner with upload-stability check
  store.py       per-folder outcome history (prevents reprocess loops)
  status.py      status.txt for the phone
  evaluation.py  golden-set scoring and run comparison
  cli.py         run / watch / eval / eval-compare / costs
```

### Ingestion

Google Drive for desktop syncs the Drive folder to the local disk; the watcher polls `inbox/` every 30 seconds. No
Drive API or OAuth app is involved. One subfolder per product; the folder name is a free-text hint.

Uploads from a phone arrive one file at a time, and file modification times on Drive can carry the photo's capture
time, so they cannot be trusted. A folder's **snapshot** is the set of `(file name, size)` pairs of its non-ignored
files. A folder is handed over once its snapshot has not changed for `QUIET_SECONDS` (120) and no file is empty.
A folder without any `.txt` file waits up to `HINT_GRACE_SECONDS` (1800) so a hint uploaded a few minutes after the
photos is not missed. Junk files (`desktop.ini`, `.tmp`, Google Docs shortcuts, `error.txt`) are ignored.

### Photo processing

For each photo, in order: load upright (EXIF transpose, HEIC via pillow-heif), resize to 1600 px on the long edge
(before background removal: faster, same result), compute the foreground mask with rembg, optionally correct the colour
cast, apply gentle CLAHE on the L channel of LAB, composite onto the background colour, save as JPEG without EXIF.

Colour-cast correction estimates per-channel gains from **background pixels only** (gray-world on the whole image
would neutralise a single-colour garment), clamps them to [0.87, 1.15], and skips the step when less than 15% of the
image is background. It assumes a neutral background: on a coloured surface (terracotta tiles were the real-world
counter-example) it shifts garment colours, so it can be turned off with `WHITE_BALANCE=false`. A planned improvement
is to skip it automatically when the background itself is strongly coloured.

Processed photos are cached under `<state_dir>/preprocessed/<item>-<key>/`, where `key` hashes the photo names and
sizes and the processing settings. The cache is written to a `.tmp` folder and renamed into place only when complete,
deleted when the item is delivered, and pruned after 30 days.

**The model always reads the original photos; buyers see the processed ones.** Processing artefacts (a strap cut off by
the mask, a colour shift) can therefore never leak into the listing text.

### Claude calls

| Stage | Default model (`mixed`) | Input | Output |
|---|---|---|---|
| describe | Haiku 5.5, effort low | up to 4 original photos at 1092 px | `VisualDescription` (type, colours, visible text, wear) |
| research | Sonnet 5.5, effort medium | cover photo + hint + description | free text with citations (web search + fetch) |
| comparables | Sonnet 5.5, effort medium | hint + description + identification | free text with citations |
| parse | Haiku 5.5, effort low | research notes + allowed URLs | `ProductSpecs` (schema-validated) |
| listing | Sonnet 5.5, effort medium | cover photo + hint + description + specs | `ListingDraft` (schema-validated) |

- Structured outputs and web tools are never combined in one call (structured output and citations are incompatible),
  so research is free text and a separate cheap call parses it.
- Web fetch blocks marketplace domains, so no request is ever made to the marketplace; search results may still
  surface its listings as comparables.
- Research is skipped when the hint already has brand, model and price (or says `research: no`); the specs are then
  built from the hint with no LLM call. Comparables are skipped whenever an asking price is given.
- Per-item web caps (searches, fetches) hold across `pause_turn` resumes by summing `usage.server_tool_use`.
- Opus and Sonnet calls enable the server-side refusal fallback; Haiku has none, so a refusal there fails the step.
- Effort is always sent explicitly, because each model has a different default.

### Grounding and review flags

Every spec value is a `SourcedValue` with a `source` (`hint`, `photo`, `web`, `inferred`), an optional `source_url`
and a confidence. After parsing, `enforce_sources` checks only `web` values: if the URL is not among the URLs the web
tools actually returned (search results, fetched pages) or cited, the value is demoted to `inferred` with low confidence
and the field is flagged. Hint values are never demoted.

`finalize_listing` is deterministic: hint values override the draft (brand, size, condition, colour, category,
composition), and `needs_review` collects the price (unless given), condition (unless given), weak or missing brand,
missing size, demoted fields, a low-confidence category, and a cut title.

### Pricing

Prices are computed in code so they are explainable and tunable:

1. `price` from the hint, unchanged.
2. Median of trusted (`web`) comparables × a condition factor (1.15 new with tags … 0.7 satisfactory).
3. Retail price (hint first, then research) × a condition factor (0.6 … 0.2).
4. Otherwise no price ("set manually").

Rounding: nearest €0.50 below €10, nearest euro above, minimum €1. The rationale (e.g. `median of 3 comparables
(12, 15, 18) × 0.85 for Good = 12.75 → 13`) is printed under the price.

### Cost control

Every API response is written to a SQLite ledger (`state.db`) with tokens, web searches and an estimated cost at list
prices (calls involving a refusal fallback or an unknown model are marked approximate). Before an item's first API call
the budget guard checks the month-to-date cost against `MONTHLY_BUDGET_USD` and the number of items started today
against `DAILY_ITEM_LIMIT`; when either is reached, items stay in the inbox as *deferred* and `status.txt` says why.
An item that has started always finishes.

### Delivery and failure handling

Each item is built in a local temp folder, never directly in Drive. On success the package is copied to
`ready/<item>.partial`, the originals are moved into `.partial/originals/`, and the folder is renamed into place.
Names that already exist get `-2`, `-3`, ….

| Situation | Behaviour |
|---|---|
| Temporary API error (rate limit, overload, 5xx, connection) | Item stays in the inbox, retried next cycle, up to 3 attempts, then treated as permanent |
| Permanent error in a step | Folder moved to `failed/<item>/` with `error.txt` naming the step; move it back to retry |
| Folder with no photos | Moved to `failed/` with `error.txt` = `no photos` |
| Drive file lock during a move | Retried 5 times with exponential backoff |
| Move still failing (package built) | Recorded as *stuck* for that folder path, never reprocessed, shown in `status.txt`; the record clears once the folder leaves the inbox |
| Budget reached | Item deferred, inbox untouched |
| Drive not mounted yet (e.g. at logon) | Watcher waits and retries each poll |

The stuck check is per folder path rather than per snapshot: a half-finished move changes the snapshot, and a
per-snapshot check would reprocess the leftover files as a new item.

### Runtime

`watch` holds an exclusive lock on `<state_dir>/watch.lock` (msvcrt on Windows, fcntl elsewhere), prunes the photo
cache daily, survives a failing cycle, and writes `status.txt` atomically (temp file + replace) so Drive never syncs a
half-written file. On Windows it runs at logon as a scheduled task with `pythonw.exe`.

## Testing

- Offline suite (pytest): fakes for the Claude client (`FakeLLM`, a fake messages API) and for background removal;
  temp directories stand in for the Drive folder. It covers the scanner's timing rules, colour math, caching, URL
  enforcement, pricing, rendering, the full package build and the Drive orchestration failure modes.
- Live tests (`-m live`, excluded by default): one real background removal and one real Claude call.
- Golden-set evaluation: `eval` builds every item of a golden set and scores brand, size, category, colours and price
  range against `expected.json`; `eval-compare` puts two runs (e.g. `--profile opus` vs `--profile mixed`) side by
  side with cost per item.

## Roadmap

- Skip colour correction automatically on coloured backgrounds.
- Toy-specific fields (age range, completeness).
- Reverse image match (Google Cloud Vision Web Detection) for items with no tag, if evaluation shows it is needed.
- Container image (with graceful SIGTERM shutdown and a health check) for an always-on machine.
- Optional OpenTelemetry tracing of API calls (metadata only) to a local Phoenix or Jaeger.
