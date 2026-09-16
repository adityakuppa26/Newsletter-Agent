import argparse
import calendar
import concurrent.futures as cf
import html
import re
import smtplib
import json
import sys
import tomllib
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formatdate, make_msgid
from pathlib import Path
from urllib.parse import urlparse

import feedparser
import requests

UA = {"User-Agent": "Mozilla/5.0 (compatible; DailyBriefing/1.0)"}
cfg_lookback = 26
cfg_max_items = 10

# Domains with hard/metered paywalls — articles from these are dropped.
PAYWALLED_DOMAINS = {
    "wsj.com", "nytimes.com", "bloomberg.com", "ft.com", "economist.com",
    "wired.com", "washingtonpost.com", "theathletic.com", "theinformation.com",
    "technologyreview.com", "newyorker.com", "theatlantic.com", "hbr.org",
    "barrons.com", "marketwatch.com", "fortune.com", "businessinsider.com",
    "seekingalpha.com", "latimes.com", "chicagotribune.com", "theage.com.au",
    "smh.com.au", "telegraph.co.uk", "thetimes.co.uk", "newstatesman.com",
    "foreignpolicy.com", "foreignaffairs.com", "nationalreview.com",
}


def is_paywalled(link: str) -> bool:
    host = urlparse(link).netloc.lower().replace("www.", "")
    return any(host == d or host.endswith("." + d) for d in PAYWALLED_DOMAINS)


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
        if is_paywalled(link):
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


def dedup_key(it: dict) -> str:
    return re.sub(r"[^a-z0-9 ]", "", it["title"].lower())[:60]


def link_key(link: str) -> str:
    p = urlparse(link)
    return (p.netloc.lower().replace("www.", "") + p.path.rstrip("/")).lower()


def load_sent(state_path: Path, days: int = 3) -> set:
    """Links/keys sent in the last `days` days. Prunes older entries."""
    try:
        data = json.loads(state_path.read_text())
    except Exception:
        return set()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    data = {k: d for k, d in data.items() if d >= cutoff}
    state_path.write_text(json.dumps(data, indent=0))
    return set(data)


def mark_sent(state_path: Path, sections: dict):
    try:
        data = json.loads(state_path.read_text())
    except Exception:
        data = {}
    today = datetime.now(timezone.utc).date().isoformat()
    for items in sections.values():
        for it in items:
            data[link_key(it["link"])] = today
            data["t:" + dedup_key(it)] = today
    state_path.write_text(json.dumps(data, indent=0))


def collect(cfg: dict, sent: set) -> dict:
    sections = {}
    seen = set()  # global across sections: a story goes to its first section only
    for sec in cfg["sections"]:
        pool = cf.ThreadPoolExecutor(max_workers=8)
        results = list(pool.map(fetch_feed, sec["feeds"]))
        pool.shutdown()
        merged = []
        for it in sorted((it for chunk in results for it in chunk),
                         key=lambda x: x["when"], reverse=True):
            key = dedup_key(it)
            lk = link_key(it["link"])
            if key in seen or lk in seen or key in sent or lk in sent or ("t:" + key) in sent:
                continue
            seen.add(key)
            seen.add(lk)
            merged.append(it)
        sections[sec["name"]] = merged[: cfg["max_items_per_section"]]
    return sections


def rank_stories(cfg: dict, sections: dict, per_section: int = 5) -> dict:
    """Separate LLM call: pick the most newsworthy items per section.
    Falls back to most-recent N on any failure."""
    listing = []
    for name, items in sections.items():
        if not items:
            continue
        listing.append(f"## {name}")
        for i, it in enumerate(items):
            listing.append(f"{i}. [{it['source']}] {it['title']}")
    prompt = (
        "You are a news editor building a daily briefing. Below are candidate headlines "
        "per section, numbered. Pick the 5 MOST NEWSWORTHY per section — prefer significant, "
        "impactful stories over opinion pieces, listicles, product deals, and minor updates. "
        "Order them by importance. Reply with ONLY a JSON object mapping section name to a "
        f"list of {per_section} numbers, e.g. {{\"Tech\": [3, 0, 7, 2, 5]}}.\n\n" + "\n".join(listing)
    )
    try:
        content = llm_call(cfg, "", prompt, 0, 500)
        picks = json.loads(re.search(r"\{.*\}", content, re.S).group(0))
        ranked = {}
        for name, items in sections.items():
            idxs = [i for i in picks.get(name, []) if isinstance(i, int) and 0 <= i < len(items)]
            if len(idxs) < min(per_section, len(items)):
                raise ValueError(f"incomplete picks for {name}")
            ranked[name] = [items[i] for i in idxs[:per_section]]
        return ranked
    except Exception as e:
        print(f"  ! ranking failed ({e}), using most-recent", file=sys.stderr)
        return {name: items[:per_section] for name, items in sections.items()}


def tag_ongoing(sections: dict, sent: set):
    """Mark items whose topic matches a story sent in recent days."""
    sent_titles = [k[2:].split() for k in sent if k.startswith("t:")]
    if not sent_titles:
        return
    for items in sections.values():
        for it in items:
            words = set(dedup_key(it).split())
            if not words:
                continue
            for st in sent_titles:
                overlap = len(words & set(st)) / max(len(words | set(st)), 1)
                if overlap >= 0.4:
                    it["ongoing"] = True
                    break


def build_prompt(sections: dict, date_str: str) -> str:
    lines = [
        f"Today is {date_str}. You are writing a daily news briefing.",
        "Below is a list of fresh articles per section.",
        "Write a SHORT, punchy briefing in plain text. Rules:",
        "- Output EVERY section listed below, in the same order, with its ## heading.",
        "  Never drop a section that has articles. Max 5 bullets per section.",
        "- Each bullet: one plain-language sentence stating the news, then a short",
        "  'why it matters' clause (one factual consequence or who it affects — no",
        "  speculation stated as fact), then a link. Under 30 words total.",
        "- Items marked [ONGOING] are follow-ups to stories from recent days:",
        "  start those bullets with 'Ongoing:'.",
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
            tag = " [ONGOING]" if it.get("ongoing") else ""
            lines.append(f"- [{it['source']}]{tag} {it['title']} | {it['link']} | {it['summary']}")
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


def load_riddle_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def save_riddle_state(path: Path, riddle: str, answer: str, word: str = ""):
    state = load_riddle_state(path)
    used = (state.get("used", []) + [riddle])[-30:]
    used_words = (state.get("used_words", []) + ([word.lower()] if word else []))[-60:]
    path.write_text(json.dumps({"riddle": riddle, "answer": answer,
                                "used": used, "used_words": used_words},
                               indent=2, ensure_ascii=False))


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


RIDDLE_BANK = [
    ("A man pushes his car up to a hotel and says, \"I'm bankrupt.\" Why?",
     "He's playing Monopoly."),
    ("You have two ropes. Each takes exactly 60 minutes to burn, but they burn unevenly. How do you measure 45 minutes?",
     "Light rope 1 at both ends and rope 2 at one end. When rope 1 burns out (30 min), light the other end of rope 2 — it finishes 15 minutes later."),
    ("A woman shoots her husband, then holds him underwater for five minutes. Later they go out to dinner together. How?",
     "She took a photo of him and developed it."),
    ("What 8-letter word can have a letter removed and still form a word, all the way down to a single letter?",
     "Starting → staring → string → sting → sing → sin → in → I."),
    ("What English word has three consecutive double letters?", "Bookkeeper."),
    ("How many times does the digit 9 appear between 1 and 100?",
     "20 — nine units digits (9, 19, ... 89) plus eleven in the 90s (90–99, with 99 counting twice)."),
    ("A bat and a ball cost $1.10 in total. The bat costs $1 more than the ball. How much does the ball cost?",
     "5 cents — the bat costs $1.05."),
    ("The person who makes it doesn't need it; the person who buys it doesn't use it; the person who uses it doesn't know it. What is it?",
     "A coffin."),
    ("I speak without a mouth and hear without ears. I have no body, but I come alive with wind. What am I?",
     "An echo."),
    ("What gets wetter the more it dries?", "A towel."),
    ("A plane crashes exactly on the border of the US and Canada. Where do they bury the survivors?",
     "Nowhere — you don't bury survivors."),
    ("What can you catch but never throw?", "A cold."),
    ("Forward I'm heavy, but backward I'm not. What am I?", "The word 'ton'."),
    ("What has many keys but can't open a single lock?", "A piano."),
    ("If you're running a race and you pass the person in second place, what place are you in?",
     "Second place."),
]

RIDDLE_PROMPT = """Invent ONE original riddle for a daily adult newsletter. Medium difficulty: solvable in a minute or two of thought, not a children's riddle, not an obscure trivia question. Logic, lateral thinking, wordplay, or light math are all fine.

Good examples of the target style and difficulty:
- "A bat and a ball cost $1.10 total. The bat costs $1 more than the ball. How much does the ball cost?" (answer: 5 cents)
- "What English word has three consecutive double letters?" (answer: bookkeeper)
- "A woman shoots her husband, then holds him underwater for five minutes. Later they go out to dinner. How?" (answer: she photographed him and developed the photo)

Rules:
- The riddle must have exactly ONE clear, verifiable answer.
- The answer must actually be correct — double-check it.
- No riddles about current events, and none of the example riddles above.
- Output EXACTLY two lines, nothing else:

RIDDLE: <the riddle>
ANSWER: <the answer, one sentence>"""


def llm_call(cfg: dict, system: str, prompt: str, temperature: float,
             max_tokens: int, models: list = None) -> str:
    """Try each model in order; return content from the first that succeeds."""
    if models is None:
        models = [cfg.get("fallback_model", "openai/gpt-4o-mini"), cfg["model"]]
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
                        *([{"role": "system", "content": system}] if system else []),
                        {"role": "user", "content": prompt},
                    ],
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                },
                timeout=60,
            )
            r.raise_for_status()
            content = (r.json()["choices"][0]["message"].get("content") or "").strip()
            if not content:
                raise ValueError("empty response")
            return content
        except Exception as e:
            print(f"  ! {model} failed: {e}", file=sys.stderr)
            last_err = e
    raise last_err


def generate_riddle(cfg: dict, avoid: list) -> tuple:
    """Return (riddle, answer). LLM-generated with validation; bank fallback."""
    try:
        content = llm_call(cfg, "You write excellent riddles. Output only what is asked.",
                           RIDDLE_PROMPT, 0.9, 1200)
        m_r = re.search(r"^RIDDLE:\s*(.+)$", content, re.M)
        m_a = re.search(r"^ANSWER:\s*(.+)$", content, re.M)
        if m_r and m_a:
            riddle, answer = m_r.group(1).strip(), m_a.group(1).strip()
            # sanity checks: reasonable lengths, answer not echoing the riddle
            if 20 <= len(riddle) <= 400 and 2 <= len(answer) <= 200 \
                    and answer.lower() not in riddle.lower():
                return riddle, answer
        raise ValueError(f"bad riddle format: {content[:100]!r}")
    except Exception as e:
        print(f"  ! riddle generation failed ({e}), using bank", file=sys.stderr)
        import random
        pool = [rb for rb in RIDDLE_BANK if rb[0] not in avoid] or RIDDLE_BANK
        return random.choice(pool)


WORD_BANK = [
    ("perspicacious", "adjective", "having keen insight; able to notice and understand things quickly",
     "Her perspicacious analysis of the market impressed the board."),
    ("obfuscate", "verb", "to deliberately make something unclear or confusing",
     "The company obfuscated the price hike behind a maze of new fees."),
    ("sanguine", "adjective", "optimistic, especially in a difficult situation",
     "Despite the losses, he remained sanguine about the quarter ahead."),
    ("equivocal", "adjective", "open to more than one interpretation; deliberately ambiguous",
     "The senator gave an equivocal answer when asked about the bill."),
    ("parsimonious", "adjective", "extremely unwilling to spend money or use resources",
     "The parsimonious budget left no room for new hires."),
    ("ineffable", "adjective", "too great or extreme to be expressed in words",
     "The view from the summit left them in ineffable awe."),
    ("recalcitrant", "adjective", "stubbornly resistant to authority or control",
     "The recalcitrant committee refused to bring the bill to a vote."),
    ("spurious", "adjective", "false or fake, though appearing genuine; based on false reasoning",
     "The report was based on spurious correlations in the data."),
    ("laconic", "adjective", "using very few words; terse",
     "His laconic reply — 'noted' — ended the discussion."),
    ("perfunctory", "adjective", "done with minimal effort or interest, as a mere duty",
     "She gave the report a perfunctory glance before signing it."),
    ("quixotic", "adjective", "extremely idealistic; unrealistic and impractical",
     "His quixotic plan to fix the city's traffic in a month drew skepticism."),
    ("trenchant", "adjective", "vigorous and incisive in expression; sharply effective",
     "The columnist's trenchant critique of the policy went viral."),
    ("intransigent", "adjective", "unwilling to change one's views; uncompromising",
     "Both sides remained intransigent as the deadline approached."),
    ("ephemeral", "adjective", "lasting for a very short time",
     "The app's ephemeral popularity faded within weeks."),
    ("assiduous", "adjective", "showing great care, attention, and perseverance",
     "Her assiduous fact-checking caught three errors in the draft."),
]

WORD_PROMPT = """Pick ONE excellent 'word of the day' for educated adults who want to build vocabulary: a genuinely useful word that appears in quality journalism and books — sophisticated but not archaic or unpronounceable. Avoid extremely common words and avoid words only useful in one narrow technical field.

Good examples of the target level: perspicacious, obfuscate, sanguine, spurious, trenchant, laconic.

Rules:
- The definition must be accurate. The example sentence must use the word correctly and naturally.
- Do not pick any of the example words above.
- Output EXACTLY four lines, nothing else:

WORD: <the word>
POS: <part of speech>
DEFINITION: <one concise definition>
EXAMPLE: <one natural example sentence>"""


def generate_word(cfg: dict, avoid: list) -> tuple:
    """Return (word, pos, definition, example). LLM with validation; bank fallback."""
    try:
        content = llm_call(cfg, "You are a precise lexicographer. Output only what is asked.",
                           WORD_PROMPT, 0.9, 1200)
        fields = {}
        for key in ("WORD", "POS", "DEFINITION", "EXAMPLE"):
            m = re.search(rf"^{key}:\s*(.+)$", content, re.M)
            if m:
                fields[key] = m.group(1).strip()
        if len(fields) == 4:
            w = fields["WORD"].lower()
            if w.isalpha() and w not in avoid and 10 <= len(fields["DEFINITION"]) <= 200 \
                    and w in fields["EXAMPLE"].lower():
                return fields["WORD"], fields["POS"], fields["DEFINITION"], fields["EXAMPLE"]
        raise ValueError(f"bad word format: {content[:100]!r}")
    except Exception as e:
        print(f"  ! word generation failed ({e}), using bank", file=sys.stderr)
        import random
        pool = [wb for wb in WORD_BANK if wb[0] not in avoid] or WORD_BANK
        return random.choice(pool)


SECTION_EMOJI = {"Tech": "💻", "Politics": "🏛️", "Finance": "💰", "World": "🌍"}


def render_html(text: str, date_str: str, markets: str = "",
                riddle: str = "", yesterday_answer: str = "",
                word: tuple = ()) -> str:
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

    word_html = ""
    if word:
        w, pos, definition, example = word
        word_html = (f'<div style="background:#eff6ff;border-left:4px solid #3b82f6;'
                     f'padding:12px 16px;border-radius:6px;margin-top:24px">'
                     f'<b>📖 WORD OF THE DAY</b><br>'
                     f'<p style="margin:8px 0 0;font-size:14px;line-height:1.45">'
                     f'<b>{html.escape(w)}</b> <span style="color:#6b7280">({html.escape(pos)})</span>'
                     f' — {html.escape(definition)}<br>'
                     f'<i style="color:#4b5563">"{html.escape(example)}"</i></p></div>')

    riddle_html = ""
    if riddle:
        ya = (f'<p style="margin:8px 0 0;color:#6b7280;font-size:13px">'
              f'Yesterday\'s answer: <i>{html.escape(yesterday_answer)}</i></p>') if yesterday_answer else ""
        riddle_html = (f'<div style="background:#fffbeb;border-left:4px solid #f59e0b;'
                       f'padding:12px 16px;border-radius:6px;margin-top:24px">'
                       f'<b>🧩 RIDDLE OF THE DAY</b> <span style="color:#9ca3af;font-size:12px">'
                       f'(answer in tomorrow\'s briefing)</span><br>'
                       f'<p style="margin:8px 0 0;font-size:14px;line-height:1.45">{html.escape(riddle)}</p>{ya}</div>')

    return f"""<html><body style="margin:0;background:#f9fafb;padding:24px 0">
<div style="max-width:620px;margin:0 auto;background:#ffffff;border-radius:10px;
padding:28px;font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif">
<h1 style="font-size:22px;margin:0 0 2px">☕ Your Morning Briefing</h1>
<p style="color:#6b7280;font-size:13px;margin:0 0 20px">{html.escape(date_str)}</p>
{f'<p style="color:#374151;font-size:13px;background:#f3f4f6;border-radius:6px;padding:8px 12px;margin:0 0 20px">📈 {html.escape(markets)}</p>' if markets else ''}
{top_html}{sec_html}
{word_html}
{riddle_html}
<p style="color:#9ca3af;font-size:11px;margin-top:28px;border-top:1px solid #e5e7eb;padding-top:10px">
Brewed daily at 5am ET by your Lightsail agent 🤖</p>
</div></body></html>"""


def send(cfg: dict, subject: str, body: str, html_body: str = ""):
    em = cfg["email"]
    from_addr = em["from_addr"]
    domain = from_addr.split("@")[-1]
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"{em.get('from_name', 'Daily Briefing')} <{from_addr}>"
    msg["To"] = ", ".join(em["to_addr"])
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid("daily-briefing", domain)
    msg["List-Unsubscribe"] = f"<mailto:{from_addr}?subject=unsubscribe>"
    msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    msg["Auto-Submitted"] = "auto-generated"
    msg.attach(MIMEText(body, "plain", "utf-8"))
    if html_body:
        msg.attach(MIMEText(html_body, "html", "utf-8"))
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
    state_path = Path(__file__).parent / ".sent_state.json"
    sent = load_sent(state_path)
    print(f"[{now.isoformat()}] collecting news... ({len(sent)} previously-sent keys)")
    sections = collect(cfg, sent)
    for name, items in sections.items():
        print(f"  {name}: {len(items)} items")

    if args.preview:
        print(build_prompt(sections, date_str))
        return

    print("ranking stories...")
    sections = rank_stories(cfg, sections)
    tag_ongoing(sections, sent)
    for name, items in sections.items():
        print(f"  {name}: {len(items)} selected, {sum(1 for i in items if i.get('ongoing'))} ongoing")

    prompt = build_prompt(sections, date_str)
    print("summarizing...")
    body = summarize(cfg, prompt)
    mark_sent(state_path, sections)

    riddle_path = Path(__file__).parent / ".riddle_state.json"
    rstate = load_riddle_state(riddle_path)
    yesterday_answer = rstate.get("answer", "")
    riddle, answer = generate_riddle(cfg, rstate.get("used", []))
    word = generate_word(cfg, rstate.get("used_words", []))
    save_riddle_state(riddle_path, riddle, answer, word[0])
    print(f"  riddle: {riddle[:60]}...")
    print(f"  word: {word[0]}")

    markets = fetch_markets()
    if markets:
        body = f"MARKETS: {markets}\n\n{body}"
    body += f"\n\nWORD OF THE DAY: {word[0]} ({word[1]}) — {word[2]}\nExample: \"{word[3]}\""
    body += f"\n\nRIDDLE OF THE DAY (answer in tomorrow's briefing):\n{riddle}"
    if yesterday_answer:
        body += f"\nYesterday's answer: {yesterday_answer}"
    subject = f"Daily Briefing - {date_str}"
    print("sending email...")
    send(cfg, subject, body, render_html(body, date_str, markets, riddle, yesterday_answer, word))
    print("done.")


if __name__ == "__main__":
    main()
