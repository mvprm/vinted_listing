# Security policy

## Reporting a vulnerability

Please do not open a public issue for security problems. Use GitHub's private vulnerability reporting instead:
**Security → Report a vulnerability** on this repository. You should get a reply within a week.

## Secrets and personal data

- The Anthropic API key and other settings live in a `.env` file **outside the repository** (`<state_dir>/.env`, see
  the README) or, for Docker, in a git-ignored `.env` next to `docker-compose.yml`. Never commit either file.
- Processed photos are written without EXIF metadata, so published listing photos carry no GPS location. The original
  photos are kept unchanged in `ready/<item>/originals/`; they still contain whatever metadata your phone recorded.
- The application sends photos and item details to the Claude API (Anthropic) and lets Claude search the web. It never
  connects to Vinted.
- Costs are bounded by `MONTHLY_BUDGET_USD`, `DAILY_ITEM_LIMIT` and per-item web search caps. Setting a spend limit in
  the Anthropic Console as well is recommended.

## Supported versions

Only the latest version on `main` receives fixes.
