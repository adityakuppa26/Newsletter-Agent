# Daily Briefing

A small agent that fetches RSS feeds, summarizes the day's news with an LLM,
and emails you a styled HTML briefing (plus a market snapshot).

## How it works

1. Fetches articles from configured RSS feeds (deduped, filtered by recency).
2. Builds a prompt and summarizes via OpenRouter with a fallback model.
3. Grabs a one-line market snapshot from Yahoo Finance (S&P 500, Nasdaq, Dow, BTC).
4. Sends a plain-text + HTML email over SMTP.

## Setup

Requires Python 3.12+.

```bash
uv sync          # or: pip install feedparser requests
cp config.example.toml config.toml
```

Edit `config.toml`: set your OpenRouter API key, SMTP credentials, recipients,
and the sections/feeds you want.

## Usage

```bash
# Full run: collect, summarize, send email
uv run python briefing.py

# Preview: just print the collected prompt, no LLM call or email
uv run python briefing.py --preview

# Use a different config file
uv run python briefing.py --config /path/to/config.toml
```

## Configuration

See `config.example.toml` for a full annotated example. Key options:

| Option | Description |
|---|---|
| `model` | Primary OpenRouter model |
| `fallback_model` | Used if the primary fails |
| `openrouter_api_key` | OpenRouter API key |
| `lookback_hours` | Only include articles newer than this |
| `max_items_per_section` | Cap of fetched items per section |
| `[email]` | SMTP host/port/credentials and from/to addresses |
| `[[sections]]` | Named sections, each with a list of RSS feed URLs |

`config.toml` is gitignored -- never commit credentials.

## Scheduling

Example cron job for a 7am daily email:

```cron
0 7 * * * cd /path/to/daily-briefing && uv run python briefing.py
```
