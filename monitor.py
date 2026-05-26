"""
무안군 일로읍 아파트·주상복합 (전세+월세) 신규 매물 모니터.

매 실행마다:
  1. dacgle.com 의 전세/월세 매물 리스트 페이지를 fetch
  2. 매물 카드를 파싱 (offer_id, 제목, 가격, 면적, 층, 등록일, 중개사 등)
  3. state.json 의 이전 offer_id 집합과 비교 → 신규만 추출
  4. 신규가 있고, state.json 이 이전부터 존재하면 Telegram 으로 전송
  5. state.json 업데이트

환경변수:
  TELEGRAM_BOT_TOKEN  — BotFather 가 발급한 봇 토큰
  TELEGRAM_CHAT_ID    — 알림 받을 채팅의 chat_id (본인과의 채팅이면 본인 user id)
  DRY_RUN             — "1" 이면 Telegram 발송 생략하고 stdout 으로만 출력

종료 코드: 0 정상, 1 fetch/파싱 실패, 2 Telegram 전송 실패.
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Iterable

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
STATE_PATH = ROOT / "state.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
}

SOURCES = {
    "전세": "https://land.dacgle.com/offer/?cateid_group=0000&trade=2&areaid=001208&areaid2=005",
    "월세": "https://land.dacgle.com/offer/?cateid_group=0000&trade=3&areaid=001208&areaid2=005",
}

KST = timezone(timedelta(hours=9))


def fetch_html(session: requests.Session, url: str) -> str:
    r = session.get(url, allow_redirects=True, timeout=30)
    r.raise_for_status()
    return r.text


OFFER_HREF_RE = re.compile(r"/offer/(\d+)")


def _text(node) -> str:
    return re.sub(r"\s+", " ", node.get_text(" ", strip=True)) if node else ""


def parse_listings(html: str, trade_label: str) -> list[dict]:
    """top + bottom row 한 쌍을 한 매물로 묶어서 반환."""
    soup = BeautifulSoup(html, "html.parser")
    listings: list[dict] = []
    for top in soup.select("tr.table_top"):
        link = top.select_one("td.title a[href*='/offer/']")
        if not link:
            continue
        m = OFFER_HREF_RE.search(link.get("href", ""))
        if not m:
            continue
        offer_id = m.group(1)

        cates = top.select("td.cateaddress div.cate")
        kind = cates[0].get("title", "").strip() if cates else ""

        title_node = top.select_one("span.title_txt")
        title = (title_node.get("title") or _text(title_node)).strip() if title_node else ""

        addr_node = top.select_one("div.address")
        address = (addr_node.get("title") or _text(addr_node)).strip() if addr_node else ""

        date_node = top.select_one("td.trade div.data")
        date_raw = _text(date_node)

        area_node = top.select_one("td.area span.cof-tooltip")
        area = ""
        if area_node:
            for dl in area_node.find_all("dl"):
                dl.extract()
            area = _text(area_node)

        floor_node = top.select_one("td.floor span.cof-tooltip")
        floor = ""
        if floor_node:
            for dl in floor_node.find_all("dl"):
                dl.extract()
            floor = _text(floor_node)

        price_node = top.select_one("td.price div.priceview")
        price_attr = (price_node.get("title") or _text(price_node)).strip() if price_node else ""

        broker_node = top.select_one("td.contact_us div.coname")
        broker = (broker_node.get("title") or _text(broker_node)).strip() if broker_node else ""
        tel_node = top.select_one("td.contact_us div.tel")
        tel = _text(tel_node)

        bottom = top.find_next_sibling("tr", class_="table_bottom")
        desc = ""
        if bottom:
            d = bottom.select_one("td.detail_txt")
            if d:
                a = d.select_one("a span")
                desc = (a.get("title") or _text(a)).strip() if a else _text(d)

        listings.append(
            {
                "offer_id": offer_id,
                "trade": trade_label,
                "kind": kind,
                "title": title,
                "address": address,
                "date": date_raw,
                "area": area,
                "floor": floor,
                "price": price_attr,
                "broker": broker,
                "tel": tel,
                "desc": desc,
                "url": f"https://land.dacgle.com/offer/{offer_id}",
            }
        )
    return listings


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _clean_price(raw: str) -> str:
    # "전세금:55,000" / "월세금:1,000/50" → "55,000만원" / "1,000/50만원"
    if not raw:
        return ""
    s = re.sub(r"^(전세금|월세금|매매가|보증금)\s*:\s*", "", raw)
    return f"{s}만원"


def format_listing(item: dict) -> str:
    price = _clean_price(item["price"])
    parts = [f"[{item['trade']} · {price}]" if price else f"[{item['trade']}]"]
    parts.append(item["title"] or "(제목 없음)")
    meta = []
    if item["area"]:
        meta.append(f"면적 {item['area']}㎡")
    if item["floor"]:
        meta.append(item["floor"])
    if item["date"]:
        meta.append(item["date"])
    if meta:
        parts.append(" · ".join(meta))
    if item["broker"] or item["tel"]:
        parts.append(f"중개: {item['broker']} {item['tel']}".strip())
    if item["desc"]:
        parts.append(item["desc"])
    parts.append(item["url"])
    return "\n".join(parts)


def chunk_messages(intro: str, items: list[dict], limit: int = 3800) -> list[str]:
    """Telegram 메시지 4096자 제한을 피해 안전하게 나눠 보냄."""
    chunks: list[str] = []
    buf = intro
    for it in items:
        block = "\n\n" + format_listing(it)
        if len(buf) + len(block) > limit and buf != intro:
            chunks.append(buf)
            buf = intro + block
        else:
            buf += block
    if buf:
        chunks.append(buf)
    return chunks


def send_telegram(token: str, chat_id: str, text: str) -> None:
    r = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": True,
        },
        timeout=30,
    )
    if not r.ok:
        raise RuntimeError(f"Telegram API {r.status_code}: {r.text[:300]}")


def main() -> int:
    sess = requests.Session()
    sess.headers.update(HEADERS)

    all_items: list[dict] = []
    for label, url in SOURCES.items():
        try:
            html = fetch_html(sess, url)
        except requests.RequestException as e:
            print(f"[ERROR] fetch {label}: {e}", file=sys.stderr)
            return 1
        items = parse_listings(html, label)
        print(f"[fetch] {label}: {len(items)}건")
        all_items.extend(items)

    if not all_items:
        print("[WARN] 매물 0건 — 사이트 구조가 바뀌었을 수 있음. 종료.", file=sys.stderr)
        return 1

    state = load_state()
    seen: dict = state.get("seen", {})
    is_first_run = not state  # state.json 자체가 없거나 비어있으면 첫 실행

    now = datetime.now(KST).isoformat(timespec="seconds")
    new_items: list[dict] = []
    for it in all_items:
        if it["offer_id"] not in seen:
            new_items.append(it)
            seen[it["offer_id"]] = {
                "first_seen": now,
                "trade": it["trade"],
                "title": it["title"],
            }

    state["seen"] = seen
    state["last_run"] = now

    if is_first_run:
        save_state(state)
        print(f"[baseline] state.json 생성. 총 {len(all_items)}건을 기준선으로 저장. 알림 미발송.")
        return 0

    if not new_items:
        save_state(state)
        print("[ok] 신규 매물 없음.")
        return 0

    intro = f"🏠 무안 일로읍 신규 매물 {len(new_items)}건 ({now})"
    messages = chunk_messages(intro, new_items)

    if os.environ.get("DRY_RUN") == "1":
        for m in messages:
            print("\n----- DRY_RUN MESSAGE -----")
            print(m)
        save_state(state)
        return 0

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        print("[ERROR] TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 미설정", file=sys.stderr)
        return 2

    try:
        for m in messages:
            send_telegram(token, chat_id, m)
    except Exception as e:
        print(f"[ERROR] Telegram 전송 실패: {e}", file=sys.stderr)
        return 2

    save_state(state)
    print(f"[sent] 신규 {len(new_items)}건 발송 완료.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
