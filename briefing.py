import argparse
import calendar
import concurrent.futures as cf
import html
import re
import smtplib
import sys
import tomllib
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from urllib.parse import urlparse

import feedparser
import requests

UA = {"User-Agent": "Mozilla/5.0 (compatible; DailyBriefing/1.0)"}
cfg_lookback = 26
cfg_max_items = 10


def load_config(path: Path) -> dict:
    with path.open("rb") as f:
        return tomllib.load(f)


def fetch_feed(url: str):
    try:
        r = requests.get(url, headers=UA, timeout=20)
        r.raise_for_status()
        parsed = feedparser.parse(r.content)
    except Exception as e:
        print(f"  ! {url}: {e}", file=sys.stderr)
        return []
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=cfg_lookback)
    items = []
    for e in parsed.entries:
        published = None
        for key in ("published_parsed", "updated_parsed"):
            if e.get(key):
                published = datetime.fromtimestamp(calendar.timegm(e[key]), tz=timezone.utc)
                break
        if published and published < cutoff:
            continue
        title = (e.get("title") or "").strip()
        link = (e.get("link") or "").strip()
        if not title or not link:
            continue
        domain = urlparse(link).netloc.replace("www.", "")
        raw = html.unescape(re.sub(r"<[^>]+>", " ", e.get("summary") or ""))
        summary = re.sub(r"\s+", " ", raw).strip()[:220]
        if summary.startswith("Article URL"):
            summary = ""
        items.append(
            {
                "title": title,
                "link": link,
                "source": domain,
                "when": published.isoformat() if published else "",
                "summary": summary,
            }
        )
    items.sort(key=lambda x: x["when"], reverse=True)
    return items[:cfg_max_items]


def collect(cfg: dict) -> dict:
    sections = {}
    for sec in cfg["sections"]:
        pool = cf.ThreadPoolExecutor(max_workers=8)
        results = list(pool.map(fetch_feed, sec["feeds"]))
        pool.shutdown()
        merged, seen = [], set()
        for it in sorted((it for chunk in results for it in chunk),
                         key=lambda x: x["when"], reverse=True):
            key = re.sub(r"[^a-z0-9 ]", "", it["title"].lower())[:60]
            if key in seen:
                continue
            seen.add(key)
            merged.append(it)
        sections[sec["name"]] = merged[: cfg["max_items_per_section"]]
    return sections


def build_prompt(sections: dict, date_str: str) -> str:
    lines = [
        f"Today is {date_str}. You are writing a daily news briefing.",
        "Below is a list of fresh articles per section.",
        "Write a SHORT, punchy briefing in plain text. Rules:",
        "- Output EVERY section listed below, in the same order, with its ## heading.",
        "  Never drop a section that has articles. Max 5 bullets per section.",
        "- Each bullet: one plain-language sentence (<25 words) + a link.",
        "- No hype, no adjectives, no emoji. Sound like a smart human briefing a friend.",
        "- Start with a single 'Top story' line overall.",
        "- Format exactly:",
        "",
        "TOP: <one sentence> — <link>",
        "",
        "## Tech",
        "- <sentence> — <link>",
        "",
        "Sections:",
        "",
    ]
    for name, items in sections.items():
        lines.append(f"### {name}")
        for it in items:
            lines.append(f"- [{it['source']}] {it['title']} | {it['link']} | {it['summary']}")
        lines.append("")
    return "\n".join(lines)


def summarize(cfg: dict, prompt: str) -> str:
    models = [cfg["model"], cfg.get("fallback_model", "openai/gpt-4o-mini")]
    last_err = None
    for model in models:
        try:
            r = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {cfg['openrouter_api_key']}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
                    "messages": [
                        {"role": "system",
                         "content": "You write ultra-concise plain-text news briefings. Output only the briefing."},
                        {"role": "user", "content": prompt},
                    ],
                    "temperature": 0.3,
                    "max_tokens": 2000,
                },
                timeout=120,
            )
            r.raise_for_status()
            content = (r.json()["choices"][0]["message"].get("content") or "").strip()
            if not content:
                raise ValueError("empty response")
            if model != models[0]:
                print(f"  (used fallback model: {model})")
            return content
        except Exception as e:
            print(f"  ! {model} failed: {e}", file=sys.stderr)
            last_err = e
    raise last_err


MARKET_SYMBOLS = [("^GSPC", "S&P 500"), ("^IXIC", "Nasdaq"), ("^DJI", "Dow"), ("BTC-USD", "BTC")]


def fetch_markets() -> str:
    """One-line market snapshot from Yahoo Finance. Empty string on failure."""
    parts = []
    for symbol, label in MARKET_SYMBOLS:
        try:
            r = requests.get(
                f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
                params={"range": "2d", "interval": "1d"},
                headers=UA, timeout=15,
            )
            meta = r.json()["chart"]["result"][0]["meta"]
            price, prev = meta["regularMarketPrice"], meta["chartPreviousClose"]
            pct = (price - prev) / prev * 100
            parts.append(f"{label} {pct:+.1f}%")
        except Exception as e:
            print(f"  ! markets {symbol}: {e}", file=sys.stderr)
    return " · ".join(parts)


SECTION_EMOJI = {"Tech": "💻", "Politics": "🏛️", "Finance": "💰", "World": "🌍"}


def render_html(text: str, date_str: str, markets: str = "") -> str:
    """Turn the plain-text briefing into a styled HTML email."""
    top, sections, cur = "", [], None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("TOP:"):
            top = line[4:].strip()
        elif line.startswith("## "):
            cur = {"name": line[3:].strip(), "items": []}
            sections.append(cur)
        elif line.startswith("- ") and cur is not None:
            body, _, link = line[2:].rpartition(" — ")
            if not link.startswith("http"):
                body, link = line[2:], ""
            cur["items"].append((html.escape(body), link))

    def bullet(body, link):
        a = (f' <a href="{link}" style="color:#2563eb;text-decoration:none;'
             f'font-size:13px;white-space:nowrap">Read →</a>') if link else ""
        return f'<li style="margin:0 0 10px;line-height:1.45">{body}{a}</li>'

    t_body, _, t_link = top.rpartition(" — ")
    top_html = ""
    if top:
        tl = (f'<a href="{t_link}" style="color:#b91c1c;text-decoration:none;font-weight:600"> Read →</a>'
              if t_link.startswith("http") else "")
        top_html = (f'<div style="background:#fef2f2;border-left:4px solid #dc2626;'
                    f'padding:12px 16px;border-radius:6px;margin-bottom:24px">'
                    f'<b>🔥 TOP STORY</b><br>{html.escape(t_body)}{tl}</div>')

    sec_html = ""
    for s in sections:
        emo = SECTION_EMOJI.get(s["name"], "📰")
        sec_html += (f'<h2 style="font-size:17px;margin:22px 0 8px;padding-bottom:4px;'
                     f'border-bottom:2px solid #e5e7eb">{emo} {html.escape(s["name"])}</h2>'
                     f'<ul style="margin:0;padding-left:20px;color:#111827;font-size:14px">'
                     + "".join(bullet(b, l) for b, l in s["items"]) + "</ul>")

    return f"""<html><body style="margin:0;background:#f9fafb;padding:24px 0">
<div style="max-width:620px;margin:0 auto;background:#ffffff;border-radius:10px;
padding:28px;font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif">
<h1 style="font-size:22px;margin:0 0 2px">☕ Your Morning Briefing</h1>
<p style="color:#6b7280;font-size:13px;margin:0 0 20px">{html.escape(date_str)}</p>
{f'<p style="color:#374151;font-size:13px;background:#f3f4f6;border-radius:6px;padding:8px 12px;margin:0 0 20px">📈 {html.escape(markets)}</p>' if markets else ''}
{top_html}{sec_html}
<p style="color:#9ca3af;font-size:11px;margin-top:28px;border-top:1px solid #e5e7eb;padding-top:10px">
Brewed daily at 7am ET by your Lightsail agent 🤖</p>
</div></body></html>"""


def send(cfg: dict, subject: str, body: str, html_body: str = ""):
    em = cfg["email"]
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = em["from_addr"]
    msg["To"] = ", ".join(em["to_addr"])
    msg.attach(MIMEText(body, "plain"))
    if html_body:
        msg.attach(MIMEText(html_body, "html"))
    with smtplib.SMTP(em["smtp_host"], em["smtp_port"], timeout=30) as s:
        s.starttls()
        s.login(em["smtp_user"], em["smtp_password"])
        s.sendmail(em["from_addr"], em["to_addr"], msg.as_string())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(Path(__file__).parent / "config.toml"))
    ap.add_argument("--preview", action="store_true", help="print briefing, skip LLM+email")
    args = ap.parse_args()

    cfg = load_config(Path(args.config))
    globals()["cfg_lookback"] = cfg.get("lookback_hours", 26)
    globals()["cfg_max_items"] = cfg.get("max_items_per_section", 10)

    now = datetime.now(timezone.utc)
    date_str = now.strftime("%A, %B %d, %Y")
    print(f"[{now.isoformat()}] collecting news...")
    sections = collect(cfg)
    for name, items in sections.items():
        print(f"  {name}: {len(items)} items")

    if args.preview:
        print(build_prompt(sections, date_str))
        return

    prompt = build_prompt(sections, date_str)
    print("summarizing...")
    body = summarize(cfg, prompt)
    markets = fetch_markets()
    if markets:
        body = f"MARKETS: {markets}\n\n{body}"
    subject = f"Daily Briefing - {date_str}"
    print("sending email...")
    send(cfg, subject, body, render_html(body, date_str, markets))
    print("done.")


if __name__ == "__main__":
    main()
