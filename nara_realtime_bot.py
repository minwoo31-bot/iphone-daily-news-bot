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

SENT_IDS_FILE = "sent_ids.json"
SENT_IDS_EXPIRY_DAYS = 7


@dataclass
class Notice:
    title: str
    link: str
    reg_dt: str
    uid: str = ""
    dminstNm: str = ""
    budget: str = ""        # 사전규격 배정예산액
    opinion_close: str = "" # 사전규격 의견등록마감일시


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
    # 1. da.gd (GET, plain text)
    try:
        encoded = urllib.parse.quote(link, safe="")
        url = f"https://da.gd/s?url={encoded}"
        short = http_get(url, timeout=8).decode("utf-8", errors="replace").strip()
        if short.startswith("http://") or short.startswith("https://"):
            return short
    except Exception:
        pass

    # 2. cleanuri.com (POST, JSON)
    try:
        url = "https://cleanuri.com/api/v1/shorten"
        data = urllib.parse.urlencode({"url": link}).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": "nara-realtime-bot/1.0",
            },
            method="POST",
        )
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(req, timeout=8, context=ctx) as resp:
            res = json.loads(resp.read().decode("utf-8"))
            short = res.get("result_url", "").strip()
            if short.startswith("http://") or short.startswith("https://"):
                return short
    except Exception:
        pass

    # 3. is.gd (GET, plain text)
    try:
        encoded = urllib.parse.quote(link, safe="")
        url = f"https://is.gd/create.php?format=simple&url={encoded}"
        short = http_get(url, timeout=8).decode("utf-8", errors="replace").strip()
        if short.startswith("http://") or short.startswith("https://"):
            return short
    except Exception:
        pass

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


def load_sent_ids() -> dict:
    """캐시 파일에서 기발송 공고 ID 로드."""
    if not os.path.exists(SENT_IDS_FILE):
        return {}
    try:
        with open(SENT_IDS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_sent_ids(sent: dict) -> None:
    """기발송 공고 ID 저장 (7일 지난 항목 자동 삭제)."""
    now = datetime.now(KST)
    cutoff = now - timedelta(days=SENT_IDS_EXPIRY_DAYS)
    pruned = {}
    for k, v in sent.items():
        try:
            dt = datetime.fromisoformat(v)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=KST)
            if dt >= cutoff:
                pruned[k] = v
        except Exception:
            pruned[k] = v
    with open(SENT_IDS_FILE, "w", encoding="utf-8") as f:
        json.dump(pruned, f, ensure_ascii=False, indent=2)


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
    inst_filters: List[str],
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
    # cutoff는 API 쿼리 범위와 동일하게 24시간으로 설정.
    # 실행 간격이 최대 수 시간이므로 lookback_minutes(90분)로 자르면
    # 실행 사이에 올라온 공고가 영구 누락됨. 중복 방지는 sent_ids 캐시가 담당.
    api_window_minutes = max(lookback_minutes, 1440)
    cutoff = now - timedelta(minutes=api_window_minutes)
    rows: List[Notice] = []
    total_seen = 0
    sample_titles: List[str] = []
    debug_status: List[str] = []
    now = datetime.now(KST)
    # API sample spec uses YYYYMMDDHHMM format.
    inqry_end = now.strftime("%Y%m%d%H%M")
    inqry_bgn = (now - timedelta(minutes=api_window_minutes)).strftime("%Y%m%d%H%M")
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
                dminstNm = str(it.get("dminstNm", "")).strip()
                keyword_match = any(k.lower() in t_lower for k in keywords)
                inst_match = any(f in dminstNm for f in inst_filters) if inst_filters else False
                if not keyword_match and not inst_match:
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
                uid = f"{bid_no}-{bid_ord}" if bid_no else link
                rows.append(Notice(title=title, link=link, reg_dt=reg_dt, uid=uid, dminstNm=dminstNm))
        except Exception as e:
            err = f"{ep}: EXCEPTION {e}"
            debug_status.append(err)
            print(f"[WARN] 나라장터 API failed ({ep}): {e}", file=sys.stderr)

    rows = rows[:limit]
    return rows, total_seen, sample_titles, debug_status


PRESPEC_ENDPOINTS = [
    "getPublicPrcureThngInfoServc",   # 용역
    "getPublicPrcureThngInfoThng",    # 물품
    "getPublicPrcureThngInfoCnstwk",  # 공사
]


def _prespec_bases() -> List[str]:
    # 환경변수로 베이스 URL을 강제할 수 있게 함. 미설정 시 gateway 경로 후보를
    # 순서대로 시도(첫 응답이 정상 JSON이면 그 베이스로 고정). 입찰공고 서비스가
    # 'ad/' prefix를 쓰므로 사전규격도 동일 prefix를 우선 시도.
    env = os.getenv("NARA_PRESPEC_BASE", "").strip()
    if env:
        return [env.rstrip("/")]
    # 게이트웨이 경로 prefix는 서비스마다 다름. 입찰공고는 'ad/'이지만 사전규격은
    # 'ao/'가 정상 경로로 확인됨(ad/=404, prefix없음=500, ao/=403=경로존재).
    return ["https://apis.data.go.kr/1230000/ao/HrcspSsstndrdInfoService"]


def _first_nonempty(it: dict, keys: List[str]) -> str:
    for k in keys:
        v = str(it.get(k, "")).strip()
        if v:
            return v
    return ""


def fetch_recent_nara_prespec(
    api_key: str,
    keywords: List[str],
    inst_filters: List[str],
    lookback_minutes: int,
    limit: int,
) -> tuple[List[Notice], int, List[str], List[str]]:
    bases = _prespec_bases()
    now = datetime.now(KST)
    api_window_minutes = max(lookback_minutes, 1440)
    cutoff = now - timedelta(minutes=api_window_minutes)
    inqry_end = now.strftime("%Y%m%d%H%M")
    inqry_bgn = (now - timedelta(minutes=api_window_minutes)).strftime("%Y%m%d%H%M")

    rows: List[Notice] = []
    total_seen = 0
    sample_titles: List[str] = []
    debug_status: List[str] = []
    seen: set[str] = set()
    working_base: Optional[str] = None

    for ep in PRESPEC_ENDPOINTS:
        candidate_bases = [working_base] if working_base else bases
        for base in candidate_bases:
            seg = base.rsplit("/1230000/", 1)[-1]
            prefix = seg.split("/")[0] if "/" in seg else "(none)"
            url = (
                f"{base}/{ep}?serviceKey={urllib.parse.quote(api_key)}"
                f"&pageNo=1&numOfRows=200&type=json&inqryDiv=1&inqryBgnDt={inqry_bgn}&inqryEndDt={inqry_end}"
            )
            try:
                data = json.loads(http_get(url, timeout=20).decode("utf-8", errors="replace"))
            except Exception as e:
                # 잘못된 베이스 URL은 보통 HTML(404)을 반환해 JSON 파싱에서 실패.
                # 다음 후보 베이스로 넘어감.
                debug_status.append(f"{ep}[{prefix}]: EXCEPTION {e}")
                continue

            header = _extract_response_header(data)
            result_code = str(header.get("resultCode", "")).strip()
            result_msg = str(header.get("resultMsg", "")).strip()
            debug_status.append(f"{ep}[{prefix}]: code={result_code or 'N/A'}, msg={result_msg or 'N/A'}")
            # 정상 JSON 응답을 받은 베이스를 이후 모든 오퍼레이션에 재사용.
            if working_base is None:
                working_base = base

            items = _extract_items_from_bid_api(data)
            total_seen += len(items)
            for it in items:
                title = _first_nonempty(it, ["prdctClsfcNoNm", "bfSpecNm", "refNoNm"])
                if not title:
                    continue
                if len(sample_titles) < 5:
                    sample_titles.append(title)
                t_lower = title.lower()
                dminstNm = _first_nonempty(
                    it, ["rlDminsttNm", "dminsttNm", "orderInsttNm", "corpNm"]
                )
                keyword_match = any(k.lower() in t_lower for k in keywords)
                inst_match = any(f in dminstNm for f in inst_filters) if inst_filters else False
                if not keyword_match and not inst_match:
                    continue

                reg_dt = _first_nonempty(it, ["rgstDt", "bfSpecRgstDt"])
                reg_ts = parse_reg_dt_to_kst(reg_dt)
                if reg_ts is not None and reg_ts < cutoff:
                    continue

                spec_no = _first_nonempty(it, ["bfSpecRgstNo", "refNo"])
                link = _first_nonempty(it, ["specDocFileUrl1"])
                if not link:
                    link = "https://www.g2b.go.kr/bs/beffatStndrdSearchSrch.do"

                budget = _first_nonempty(it, ["asignBdgtAmt", "budgetAmount"])
                opinion_close = _first_nonempty(it, ["opninRgstClseDt", "opninRgstClsDt"])

                key = f"{title}|{spec_no}"
                if key in seen:
                    continue
                seen.add(key)
                uid = f"prespec:{spec_no}" if spec_no else f"prespec:{title}"
                rows.append(
                    Notice(
                        title=title,
                        link=link,
                        reg_dt=reg_dt,
                        uid=uid,
                        dminstNm=dminstNm,
                        budget=budget,
                        opinion_close=opinion_close,
                    )
                )
            break  # 이 오퍼레이션은 정상 처리됨 → 다른 후보 베이스 시도 안 함

    rows = rows[:limit]
    return rows, total_seen, sample_titles, debug_status


def _fmt_dt(raw: str) -> str:
    raw = (raw or "").strip()
    digits = "".join(ch for ch in raw if ch.isdigit())
    if len(digits) >= 12:
        return f"{digits[0:4]}-{digits[4:6]}-{digits[6:8]} {digits[8:10]}:{digits[10:12]}"
    return raw


def _fmt_budget(raw: str) -> str:
    raw = (raw or "").strip()
    try:
        amount = int(float(raw))
        return f"{amount:,}원"
    except (ValueError, TypeError):
        return raw


def build_message(
    label: str,
    notices: List[Notice],
    total_seen: int,
    sample_titles: List[str],
    debug_status: List[str],
    lookback_minutes: int,
    trigger_name: str,
    now_str: str,
    is_prespec: bool,
) -> str:
    unit = "사전규격" if is_prespec else "공고"
    lines = [
        f"[{label}] {now_str}",
        f"- Trigger: {trigger_name}",
        f"- API fetched items: {total_seen}",
        f"- 최근 {lookback_minutes}분 신규 {unit} {len(notices)}건",
        "",
    ]
    if notices:
        for i, n in enumerate(notices, start=1):
            inst_label = f"[{n.dminstNm}] " if n.dminstNm else ""
            lines.append(f"{i}. {inst_label}{n.title} ({shorten_link(n.link)})")
            if is_prespec:
                extras = []
                if n.budget:
                    extras.append(f"예산 {_fmt_budget(n.budget)}")
                if n.opinion_close:
                    extras.append(f"의견마감 {_fmt_dt(n.opinion_close)}")
                if extras:
                    lines.append("   " + " | ".join(extras))
            lines.append("")
    else:
        lines.append(f"테스트 실행 결과: 조건에 맞는 {unit}이(가) 없어 0건입니다.")
        if sample_titles:
            lines.append("")
            lines.append(f"샘플 {unit} 제목(필터 전):")
            for i, t in enumerate(sample_titles, start=1):
                lines.append(f"{i}. {t}")
        if debug_status:
            lines.append("")
            lines.append("API 응답 상태:")
            for s in debug_status:
                lines.append(f"- {s}")
    return "\n".join(lines).strip()


def main() -> int:
    token = getenv_required("TELEGRAM_BOT_TOKEN")
    chat_ids = parse_chat_ids()
    api_key = getenv_required("NARA_BID_API_KEY")
    sent_ids = load_sent_ids()
    lookback_minutes = int(getenv_with_default("NARA_LOOKBACK_MIN", "60"))
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
    inst_filters = [
        x.strip()
        for x in getenv_with_default("NARA_INST_FILTER", "울산연구원").split(",")
        if x.strip()
    ]

    now_str = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S KST")
    newly_sent: List[Notice] = []

    # 1) 입찰공고(실공고)
    notices, total_seen, sample_titles, debug_status = fetch_recent_nara_notices(
        api_key=api_key,
        keywords=keywords,
        inst_filters=inst_filters,
        lookback_minutes=lookback_minutes,
        limit=max_items,
    )
    new_notices = [n for n in notices if n.uid not in sent_ids]
    if new_notices or trigger_name != "schedule":
        text = build_message(
            label="나라장터 실시간",
            notices=new_notices,
            total_seen=total_seen,
            sample_titles=sample_titles,
            debug_status=debug_status,
            lookback_minutes=lookback_minutes,
            trigger_name=trigger_name,
            now_str=now_str,
            is_prespec=False,
        )
        for chat_id in chat_ids:
            telegram_send(token, chat_id, text)
        newly_sent.extend(new_notices)
    else:
        print(f"No new bid notices (matched={len(notices)}). Skip bid send.")

    # 2) 사전규격 (별도 메시지)
    pre_notices, pre_total, pre_samples, pre_debug = fetch_recent_nara_prespec(
        api_key=api_key,
        keywords=keywords,
        inst_filters=inst_filters,
        lookback_minutes=lookback_minutes,
        limit=max_items,
    )
    new_pre = [n for n in pre_notices if n.uid not in sent_ids]
    if new_pre or trigger_name != "schedule":
        text = build_message(
            label="나라장터 사전규격",
            notices=new_pre,
            total_seen=pre_total,
            sample_titles=pre_samples,
            debug_status=pre_debug,
            lookback_minutes=lookback_minutes,
            trigger_name=trigger_name,
            now_str=now_str,
            is_prespec=True,
        )
        for chat_id in chat_ids:
            telegram_send(token, chat_id, text)
        newly_sent.extend(new_pre)
    else:
        print(f"No new pre-spec (matched={len(pre_notices)}). Skip pre-spec send.")

    # 발송된 항목 ID 저장 (입찰공고 + 사전규격)
    if newly_sent:
        now_iso = datetime.now(KST).isoformat()
        for n in newly_sent:
            sent_ids[n.uid] = now_iso
        save_sent_ids(sent_ids)

    print(f"Done: sent {len(new_notices)} bid + {len(new_pre)} pre-spec notices.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
