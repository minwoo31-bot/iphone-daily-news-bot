#!/usr/bin/env python3
"""
NaraJangter near-real-time notifier (Telegram)

Runs frequently (e.g., every 5 minutes) and sends only recent
data/bigdata-related bid notices from 나라장터.
"""

from __future__ import annotations

import json
import os
import ssl
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import List, Optional


KST = timezone(timedelta(hours=9))


@dataclass
class Notice:
    title: str
    link: str
    reg_dt: str


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
        headers={"User-Agent": "nara-realtime-bot/1.0"},
    )
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        return resp.read()


def http_post_json(url: str, data: dict, timeout: int = 20) -> dict:
    payload = json.dumps(data).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "nara-realtime-bot/1.0",
        },
        method="POST",
    )
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        return json.loads(resp.read().decode("utf-8"))


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


def parse_chat_ids() -> List[str]:
    ids_env = os.getenv("TELEGRAM_CHAT_IDS", "").strip()
    if ids_env:
        ids = [x.strip() for x in ids_env.split(",") if x.strip()]
        if ids:
            return ids
    return [getenv_required("TELEGRAM_CHAT_ID")]


def telegram_send(token: str, chat_id: str, text: str) -> None:
    endpoint = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": False,
    }
    _ = http_post_json(endpoint, payload, timeout=20)


def _extract_items_from_bid_api(data: dict) -> List[dict]:
    response = data.get("response", {})
    body = response.get("body", response)
    items = body.get("items", {})
    if isinstance(items, dict):
        item = items.get("item", [])
        if isinstance(item, list):
            return item
        if isinstance(item, dict):
            return [item]
    if isinstance(items, list):
        return items
    return []


def parse_reg_dt_to_kst(reg_dt: str) -> Optional[datetime]:
    reg_dt = (reg_dt or "").strip()
    if not reg_dt:
        return None
    # Common format from API: YYYYMMDDHHMMSS
    if reg_dt.isdigit() and len(reg_dt) >= 14:
        try:
            dt = datetime.strptime(reg_dt[:14], "%Y%m%d%H%M%S")
            return dt.replace(tzinfo=KST)
        except Exception:
            return None
    # Fallback for ISO-ish values
    try:
        dt = datetime.fromisoformat(reg_dt.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            return dt.replace(tzinfo=KST)
        return dt.astimezone(KST)
    except Exception:
        return None


def _extract_response_header(data: dict) -> dict:
    response = data.get("response", {})
    header = response.get("header", {})
    return header if isinstance(header, dict) else {}


def fetch_recent_nara_notices(
    api_key: str,
    keywords: List[str],
    lookback_minutes: int,
    limit: int,
) -> tuple[List[Notice], int, List[str], List[str]]:
    endpoints = [
        "getBidPblancListInfoServcPPSSrch",   # 용역
        "getBidPblancListInfoThngPPSSrch",    # 물품
        "getBidPblancListInfoCnstwkPPSSrch",  # 공사
    ]
    base = "https://apis.data.go.kr/1230000/ad/BidPublicInfoService"
    now = datetime.now(KST)
    cutoff = now - timedelta(minutes=lookback_minutes)
    rows: List[Notice] = []
    total_seen = 0
    sample_titles: List[str] = []
    debug_status: List[str] = []
    now = datetime.now(KST)
    # API sample spec uses YYYYMMDDHHMM format.
    inqry_end = now.strftime("%Y%m%d%H%M")
    inqry_bgn = (now - timedelta(minutes=max(lookback_minutes, 1440))).strftime("%Y%m%d%H%M")
    seen: set[str] = set()

    for ep in endpoints:
        url = (
            f"{base}/{ep}?serviceKey={urllib.parse.quote(api_key)}"
            f"&pageNo=1&numOfRows=200&type=json&inqryDiv=1&inqryBgnDt={inqry_bgn}&inqryEndDt={inqry_end}"
        )
        try:
            data = json.loads(http_get(url, timeout=20).decode("utf-8", errors="replace"))
            header = _extract_response_header(data)
            result_code = str(header.get("resultCode", "")).strip()
            result_msg = str(header.get("resultMsg", "")).strip()
            debug_status.append(f"{ep}: code={result_code or 'N/A'}, msg={result_msg or 'N/A'}")
            items = _extract_items_from_bid_api(data)
            total_seen += len(items)
            for it in items:
                title = str(it.get("bidNtceNm", "")).strip()
                if not title:
                    continue
                if len(sample_titles) < 5:
                    sample_titles.append(title)
                t_lower = title.lower()
                if not any(k.lower() in t_lower for k in keywords):
                    continue
                reg_dt = str(it.get("rgstDt", "")).strip()
                reg_ts = parse_reg_dt_to_kst(reg_dt)
                if reg_ts is not None and reg_ts < cutoff:
                    continue

                bid_no = str(it.get("bidNtceNo", "")).strip()
                bid_ord = str(it.get("bidNtceOrd", "000")).strip() or "000"
                detail = str(it.get("bidNtceDtlUrl", "")).strip()
                link = detail
                if not link and bid_no:
                    link = (
                        f"https://www.g2b.go.kr:8101/ep/tbid/tbidFwd.do?"
                        f"bidno={urllib.parse.quote(bid_no)}&bidseq={urllib.parse.quote(bid_ord)}"
                    )
                if not link:
                    link = "https://www.g2b.go.kr/"

                key = f"{title}|{link}"
                if key in seen:
                    continue
                seen.add(key)
                rows.append(Notice(title=title, link=link, reg_dt=reg_dt))
        except Exception as e:
            err = f"{ep}: EXCEPTION {e}"
            debug_status.append(err)
            print(f"[WARN] 나라장터 API failed ({ep}): {e}", file=sys.stderr)

    rows = rows[:limit]
    return rows, total_seen, sample_titles, debug_status


def main() -> int:
    token = getenv_required("TELEGRAM_BOT_TOKEN")
    chat_ids = parse_chat_ids()
    api_key = getenv_required("NARA_BID_API_KEY")
    lookback_minutes = int(getenv_with_default("NARA_LOOKBACK_MIN", "1440"))  # 테스트: 24시간으로 확대
    max_items = int(getenv_with_default("MAX_NARA_REALTIME", "10"))
    event_name = getenv_with_default("GITHUB_EVENT_NAME", "manual")
    trigger_name = "schedule" if event_name == "schedule" else "manual"
    keywords = [
        x.strip()
        for x in getenv_with_default(
            "NARA_KEYWORDS",
            "데이터,빅데이터,인공지능,AI,데이터플랫폼,데이터분석,데이터 구축,바우처",
        ).split(",")
        if x.strip()
    ]

    notices, total_seen, sample_titles, debug_status = fetch_recent_nara_notices(
        api_key=api_key,
        keywords=keywords,
        lookback_minutes=lookback_minutes,
        limit=max_items,
    )

    now = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S KST")
    if not notices and trigger_name == "schedule":
        print("No recent NaraJangter notices. Skip send.")
        return 0

    lines = [
        f"[나라장터 실시간] {now}",
        f"- Trigger: {trigger_name}",
        f"- API fetched items: {total_seen}",
        f"- 최근 {lookback_minutes}분 데이터/빅데이터 공고 {len(notices)}건",
        "",
    ]
    if notices:
        for i, n in enumerate(notices, start=1):
            lines.append(f"{i}. {n.title} ({shorten_link(n.link)})")
            lines.append("")
    else:
        lines.append("테스트 실행 결과: 조건에 맞는 공고가 없어 0건입니다.")
        if sample_titles:
            lines.append("")
            lines.append("샘플 공고 제목(필터 전):")
            for i, t in enumerate(sample_titles, start=1):
                lines.append(f"{i}. {t}")
        if debug_status:
            lines.append("")
            lines.append("API 응답 상태:")
            for s in debug_status:
                lines.append(f"- {s}")
    text = "\n".join(lines).strip()

    for chat_id in chat_ids:
        telegram_send(token, chat_id, text)
    print(f"Done: sent {len(notices)} notices.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
