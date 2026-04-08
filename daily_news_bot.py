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
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable, List, Optional


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
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        body = resp.read().decode("utf-8")
        return json.loads(body)


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
        f"오늘 날짜는 {date_str} 입니다.",
        "아래 뉴스 10개를 한국어로 요약해 주세요.",
        "",
        "요구사항:",
        "1) 각 뉴스는 2~3문장으로 핵심만 간결하게 요약",
        "2) 과장/추측 금지, 제목 기반으로만 요약",
        "3) 출력 형식:",
        "   1. [제목]",
        "   - 요약: ...",
        "   - 링크: ...",
        "",
        "뉴스 목록:",
    ]
    for idx, item in enumerate(items, start=1):
        lines.append(f"{idx}) 제목: {item.title}")
        lines.append(f"   링크: {item.link}")
        if item.source:
            lines.append(f"   출처: {item.source}")
    return "\n".join(lines)


def summarize_with_gemini(api_key: str, prompt: str, model: str) -> str:
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{urllib.parse.quote(model)}:generateContent?key={urllib.parse.quote(api_key)}"
    )
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.2,
            "topP": 0.9,
            "maxOutputTokens": 2048,
        },
    }
    data = http_post_json(url, body, timeout=40)
    candidates = data.get("candidates") or []
    if not candidates:
        raise RuntimeError(f"No Gemini candidates returned: {data}")
    parts = candidates[0].get("content", {}).get("parts", [])
    text = "".join(part.get("text", "") for part in parts if isinstance(part, dict)).strip()
    if not text:
        raise RuntimeError(f"Gemini returned empty text: {data}")
    return text


def format_fallback(items: List[NewsItem], date_str: str) -> str:
    lines = [f"[{date_str}] 오늘의 뉴스 10선 (요약 실패로 링크만 전송)"]
    for i, item in enumerate(items, start=1):
        lines.append(f"{i}. {item.title}")
        lines.append(f"링크: {item.link}")
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


def main() -> int:
    telegram_bot_token = getenv_required("TELEGRAM_BOT_TOKEN")
    telegram_chat_id = getenv_required("TELEGRAM_CHAT_ID")
    gemini_api_key = getenv_required("GEMINI_API_KEY")

    gemini_model = getenv_with_default("GEMINI_MODEL", "gemini-2.0-flash")
    max_news = int(getenv_with_default("MAX_NEWS", "10"))
    rss_feeds_env = getenv_with_default(
        "RSS_FEEDS",
        "https://news.google.com/rss?hl=ko&gl=KR&ceid=KR:ko",
    )
    rss_feeds = [x.strip() for x in rss_feeds_env.split(",") if x.strip()]

    collected: List[NewsItem] = []
    for feed in rss_feeds:
        try:
            collected.extend(parse_rss_feed(feed, max_items=40))
        except Exception as e:
            print(f"[WARN] failed feed {feed}: {e}", file=sys.stderr)

    if not collected:
        raise RuntimeError("No news collected from RSS feeds.")

    selected = unique_latest(collected, limit=max_news)
    date_str = datetime.now(KST).strftime("%Y-%m-%d")
    prompt = build_prompt(selected, date_str)

    summary_text: Optional[str] = None
    try:
        summary_text = summarize_with_gemini(gemini_api_key, prompt, gemini_model)
    except Exception as e:
        print(f"[WARN] Gemini summarize failed: {e}", file=sys.stderr)
        summary_text = format_fallback(selected, date_str)

    header = f"[{date_str}] 오늘의 뉴스 {len(selected)}개 요약\n"
    final_text = f"{header}\n{summary_text}".strip()

    for part in chunk_text(final_text, max_len=3500):
        telegram_send(telegram_bot_token, telegram_chat_id, part)
        time.sleep(0.5)

    print("Done: sent daily news digest.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
