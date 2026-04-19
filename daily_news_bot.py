#!/usr/bin/env python3
"""
Daily News Telegram Bot

Features:
- Fetches latest news from RSS feeds
- Picks top N unique articles
- Summarizes in Korean with Gemini API
- Sends final digest to Telegram
"""

from __future__ import annotations

import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable, List, Optional, Set


KST = timezone(timedelta(hours=9))


@dataclass
class NewsItem:
    title: str
    link: str
    source: str
    pub_date: str


def getenv_required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def getenv_with_default(name: str, default: str) -> str:
    value = os.getenv(name, "").strip()
    return value if value else default


def http_get(url: str, timeout: int = 20) -> bytes:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "daily-news-bot/1.0 (+telegram digest)",
            "Accept": "application/json, text/plain, */*",
        },
    )
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        return resp.read()


def http_post_json(url: str, data: dict, timeout: int = 30) -> dict:
    payload = json.dumps(data).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "daily-news-bot/1.0 (+telegram digest)",
        },
        method="POST",
    )
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body)
    except urllib.error.HTTPError as e:
        err_body = ""
        try:
            err_body = e.read().decode("utf-8", errors="replace")
        except Exception:
            err_body = str(e)
        raise RuntimeError(f"HTTP {e.code} error from API: {err_body[:800]}") from e


def strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text or "").strip()


def normalize_link(link: str) -> str:
    if not link:
        return ""
    parsed = urllib.parse.urlparse(link.strip())
    if not parsed.scheme:
        return link.strip()
    clean_query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    clean_query = [(k, v) for (k, v) in clean_query if not k.startswith("utm_")]
    parsed = parsed._replace(query=urllib.parse.urlencode(clean_query), fragment="")
    return urllib.parse.urlunparse(parsed)


def shorten_link(link: str) -> str:
    encoded = urllib.parse.quote(link, safe="")
    providers = [
        f"https://is.gd/create.php?format=simple&url={encoded}",
        f"https://tinyurl.com/api-create.php?url={encoded}",
    ]
    for url in providers:
        try:
            short = http_get(url, timeout=10).decode("utf-8", errors="replace").strip()
            if short.startswith("http://") or short.startswith("https://"):
                return short
        except Exception:
            continue
    return link


def parse_rss_feed(feed_url: str, max_items: int = 30) -> List[NewsItem]:
    raw = http_get(feed_url)
    root = ET.fromstring(raw)
    items: List[NewsItem] = []

    channel = root.find("channel")
    if channel is None:
        return items

    for node in channel.findall("item")[:max_items]:
        title = strip_html((node.findtext("title") or "").strip())
        link = normalize_link((node.findtext("link") or "").strip())
        pub_date = (node.findtext("pubDate") or "").strip()
        source_node = node.find("source")
        source = (source_node.text or "").strip() if source_node is not None else ""

        if title and link:
            items.append(
                NewsItem(
                    title=title,
                    link=link,
                    source=source or "Unknown",
                    pub_date=pub_date,
                )
            )
    return items


def unique_latest(items: Iterable[NewsItem], limit: int) -> List[NewsItem]:
    seen_titles = set()
    seen_links = set()
    result: List[NewsItem] = []

    for item in items:
        title_key = re.sub(r"\s+", " ", item.title.lower()).strip()
        link_key = item.link.lower().strip()
        if title_key in seen_titles or link_key in seen_links:
            continue
        seen_titles.add(title_key)
        seen_links.add(link_key)
        result.append(item)
        if len(result) >= limit:
            break
    return result


def build_prompt(items: List[NewsItem], date_str: str) -> str:
    lines = [
        f"Today is {date_str}.",
        "Please summarize the following 10 news items in Korean.",
        "",
        "Requirements:",
        "1) For each item, write 2-3 concise Korean sentences.",
        "2) No speculation. Use title/link context only.",
        "3) Output format:",
        "   1. [Title]",
        "   - Summary: ...",
        "   - Link: ...",
        "",
        "News list:",
    ]
    for idx, item in enumerate(items, start=1):
        lines.append(f"{idx}) Title: {item.title}")
        lines.append(f"   Link: {item.link}")
        if item.source:
            lines.append(f"   Source: {item.source}")
    return "\n".join(lines)


def build_single_item_prompt(item: NewsItem, index: int, date_str: str) -> str:
    return "\n".join(
        [
            f"Today is {date_str}.",
            "Summarize this single news item in Korean.",
            "Write exactly 1 concise Korean sentence.",
            "No speculation. Use the provided title/link/source only.",
            "Do not include numbering or bullet symbols.",
            "Focus only on the most important facts, changes, impacts, and numbers.",
            "Do not mention who announced/reported it or where it was reported.",
            "Avoid expressions like 諛쒗몴?덈떎, 蹂대룄?덈떎, ?꾪뻽?? 諛앺삍??",
            "",
            f"Index: {index}",
            f"Title: {item.title}",
            f"Link: {item.link}",
            f"Source: {item.source}",
            "",
            "Output only that 1 sentence.",
        ]
    )


def summarize_with_gemini(api_key: str, prompt: str, model: str) -> str:
    model = model.strip()
    if model.startswith("models/"):
        model = model.split("/", 1)[1]
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.2,
            "topP": 0.9,
            "maxOutputTokens": 2048,
        },
    }
    last_error: Optional[Exception] = None
    for api_ver in ("v1beta", "v1"):
        url = (
            f"https://generativelanguage.googleapis.com/{api_ver}/models/"
            f"{urllib.parse.quote(model)}:generateContent?key={urllib.parse.quote(api_key)}"
        )
        try:
            data = http_post_json(url, body, timeout=40)
            candidates = data.get("candidates") or []
            if not candidates:
                raise RuntimeError(f"No Gemini candidates returned: {data}")
            parts = candidates[0].get("content", {}).get("parts", [])
            text = "".join(
                part.get("text", "") for part in parts if isinstance(part, dict)
            ).strip()
            if not text:
                raise RuntimeError(f"Gemini returned empty text: {data}")
            return text
        except Exception as e:
            last_error = e
    raise RuntimeError(f"Gemini generateContent failed for model={model}: {last_error}")


def list_gemini_models(api_key: str) -> List[str]:
    for api_ver in ("v1beta", "v1"):
        try:
            url = (
                f"https://generativelanguage.googleapis.com/{api_ver}/models"
                f"?key={urllib.parse.quote(api_key)}"
            )
            raw = http_get(url, timeout=20)
            data = json.loads(raw.decode("utf-8"))
            models = []
            for m in data.get("models", []):
                methods = m.get("supportedGenerationMethods", [])
                if "generateContent" in methods:
                    name = str(m.get("name", "")).strip()
                    if name.startswith("models/"):
                        name = name.split("/", 1)[1]
                    if name:
                        models.append(name)
            if models:
                return models
        except Exception as e:
            print(f"[WARN] list models failed ({api_ver}): {e}", file=sys.stderr)
    return []


def build_model_candidates(configured: List[str], available: List[str]) -> List[str]:
    candidates: List[str] = []
    seen: Set[str] = set()

    def add(model: str) -> None:
        key = model.strip().lower()
        if key and key not in seen:
            seen.add(key)
            candidates.append(model.strip())

    for m in configured:
        add(m)
    for m in ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-1.5-flash"]:
        add(m)
    for m in available:
        if "flash" in m.lower():
            add(m)
    for m in available:
        add(m)
    return candidates


def summarize_with_gemini_any_model(api_key: str, prompt: str, models: List[str]) -> str:
    last_error: Optional[Exception] = None
    for model in models:
        try:
            return summarize_with_gemini(api_key, prompt, model)
        except Exception as e:
            last_error = e
            print(f"[WARN] Gemini model failed ({model}): {e}", file=sys.stderr)
    if last_error:
        raise RuntimeError(f"All Gemini models failed. Last error: {last_error}") from last_error
    raise RuntimeError("No Gemini model available.")


def force_single_line(text: str) -> str:
    raw_lines = [ln.strip(" -\t") for ln in text.splitlines() if ln.strip()]
    merged = " ".join(raw_lines).strip()
    if not merged:
        return "?붿빟???앹꽦?섏? 紐삵뻽?듬땲??"

    chunks = [c.strip() for c in re.split(r"(?<=[.!?])\s+|(?<=[?ㅼ슂])\s+", merged) if c.strip()]
    line = chunks[0] if chunks else merged
    # Drop low-signal reporting verbs if they leaked into output.
    banned_patterns = [
        r"\b(諛쒗몴?덈떎|蹂대룄?덈떎|?꾪뻽??諛앺삍??\b",
        r"(???곕Ⅴ硫????섑븯硫?",
    ]
    for p in banned_patterns:
        line = re.sub(p, "", line).strip()
    line = re.sub(r"\s+", " ", line).strip(" ,.;")
    return line or "愿???댁슜??留곹겕?먯꽌 ?뺤씤??二쇱꽭??"

def summarize_items_individually(
    api_key: str, models: List[str], items: List[NewsItem], date_str: str
) -> str:
    lines: List[str] = []
    for i, item in enumerate(items, start=1):
        short = shorten_link(item.link)
        lines.append(f"{i}. {item.title} ({short})")
    return "\n".join(lines).strip()

def format_fallback(items: List[NewsItem], date_str: str, reason: str = "") -> str:
    lines = [f"[{date_str}] Today news 10 (summary failed, links only)"]
    if reason:
        lines.append(f"Reason: {reason[:200]}")
    for i, item in enumerate(items, start=1):
        lines.append(f"{i}. {item.title}")
        lines.append(f"Link: {shorten_link(item.link)}")
    return "\n".join(lines)


def chunk_text(text: str, max_len: int = 3500) -> List[str]:
    text = text.strip()
    if len(text) <= max_len:
        return [text]
    chunks = []
    current = []
    current_len = 0
    for line in text.splitlines(keepends=True):
        if current_len + len(line) > max_len and current:
            chunks.append("".join(current).strip())
            current = [line]
            current_len = len(line)
        else:
            current.append(line)
            current_len += len(line)
    if current:
        chunks.append("".join(current).strip())
    return chunks


def telegram_send(token: str, chat_id: str, text: str) -> None:
    endpoint = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": False,
    }
    _ = http_post_json(endpoint, payload, timeout=20)


def parse_chat_ids() -> List[str]:
    ids_env = os.getenv("TELEGRAM_CHAT_IDS", "").strip()
    if ids_env:
        ids = [x.strip() for x in ids_env.split(",") if x.strip()]
        if ids:
            return ids
    return [getenv_required("TELEGRAM_CHAT_ID")]


def main() -> int:
    telegram_bot_token = getenv_required("TELEGRAM_BOT_TOKEN")
    telegram_chat_ids = parse_chat_ids()
    gemini_api_key = getenv_with_default("GEMINI_API_KEY", "")

    gemini_models_env = getenv_with_default(
        "GEMINI_MODELS",
        getenv_with_default("GEMINI_MODEL", "gemini-2.0-flash,gemini-1.5-flash"),
    )
    configured_models = [m.strip() for m in gemini_models_env.split(",") if m.strip()]
    max_news = int(getenv_with_default("MAX_NEWS", "10"))
    max_sports = int(getenv_with_default("MAX_SPORTS", "10"))
    max_ent = int(getenv_with_default("MAX_ENTERTAINMENT", "10"))
    rss_feeds_env = getenv_with_default(
        "RSS_FEEDS",
        "https://news.google.com/rss?hl=ko&gl=KR&ceid=KR:ko",
    )
    sports_rss_feeds_env = getenv_with_default(
        "SPORTS_RSS_FEEDS",
        "https://news.google.com/rss/headlines/section/topic/SPORTS?hl=ko&gl=KR&ceid=KR:ko",
    )
    ent_rss_feeds_env = getenv_with_default(
        "ENTERTAINMENT_RSS_FEEDS",
        "https://news.google.com/rss/headlines/section/topic/ENTERTAINMENT?hl=ko&gl=KR&ceid=KR:ko",
    )
    rss_feeds = [x.strip() for x in rss_feeds_env.split(",") if x.strip()]
    sports_rss_feeds = [x.strip() for x in sports_rss_feeds_env.split(",") if x.strip()]
    ent_rss_feeds = [x.strip() for x in ent_rss_feeds_env.split(",") if x.strip()]

    collected: List[NewsItem] = []
    for feed in rss_feeds:
        try:
            collected.extend(parse_rss_feed(feed, max_items=40))
        except Exception as e:
            print(f"[WARN] failed feed {feed}: {e}", file=sys.stderr)

    if not collected:
        raise RuntimeError("No news collected from RSS feeds.")

    sports_collected: List[NewsItem] = []
    for feed in sports_rss_feeds:
        try:
            sports_collected.extend(parse_rss_feed(feed, max_items=40))
        except Exception as e:
            print(f"[WARN] failed sports feed {feed}: {e}", file=sys.stderr)

    ent_collected: List[NewsItem] = []
    for feed in ent_rss_feeds:
        try:
            ent_collected.extend(parse_rss_feed(feed, max_items=40))
        except Exception as e:
            print(f"[WARN] failed entertainment feed {feed}: {e}", file=sys.stderr)

    selected = unique_latest(collected, limit=max_news)
    sports_selected = unique_latest(sports_collected, limit=max_sports)
    ent_selected = unique_latest(ent_collected, limit=max_ent)
    now_kst = datetime.now(KST)
    date_str = now_kst.strftime("%Y-%m-%d")
    run_time_kst = now_kst.strftime("%Y-%m-%d %H:%M:%S KST")
    event_name = getenv_with_default("GITHUB_EVENT_NAME", "manual")
    trigger_name = "schedule" if event_name == "schedule" else "manual"

    if gemini_api_key:
        print("[INFO] GEMINI_API_KEY is set but summary mode is disabled (title-only mode).", file=sys.stderr)

    general_summary_text = summarize_items_individually(
        gemini_api_key, configured_models, selected, date_str
    )
    sports_summary_text = (
        summarize_items_individually(gemini_api_key, configured_models, sports_selected, date_str)
        if sports_selected
        else "No sports news collected."
    )
    ent_summary_text = (
        summarize_items_individually(gemini_api_key, configured_models, ent_selected, date_str)
        if ent_selected
        else "No entertainment news collected."
    )

    header = (
        f"[{date_str}] Daily news summary\n"
        f"- Run time: {run_time_kst}\n"
        f"- Trigger: {trigger_name}\n"
        f"- General: {len(selected)} items\n"
        f"- Sports: {len(sports_selected)} items\n"
        f"- Entertainment/Broadcast: {len(ent_selected)} items\n"
    )
    final_text = (
        f"{header}\n"
        "[GENERAL NEWS]\n"
        f"{general_summary_text}\n\n"
        "[SPORTS NEWS]\n"
        f"{sports_summary_text}\n\n"
        "[ENTERTAINMENT/BROADCAST NEWS]\n"
        f"{ent_summary_text}"
    ).strip()

    for chat_id in telegram_chat_ids:
        for part in chunk_text(final_text, max_len=3500):
            telegram_send(telegram_bot_token, chat_id, part)
            time.sleep(0.5)

    print("Done: sent daily news digest.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

