"""
무안군 일로읍·삼향읍 아파트·주상복합 (전세+월세) 매물 일일 리포트.

매 실행마다:
  1. dacgle.com 의 전세/월세 매물 리스트 페이지를 fetch
  2. 매물 카드를 파싱 (offer_id, 제목, 가격, 면적, 층, 등록일, 중개사 등)
  3. 현재 등록된 매물 전체를 Telegram 으로 전송 (0건이어도 발송)
  4. 별도로 네이버 부동산 모바일 API 에서 동일 지역 매물을 받아 별도 메시지로 발송
  5. 3일 연속 0건(dacgle 기준)이면 silent-failure 경고를 추가 발송
  6. state.json 의 last_run/zero_streak 갱신

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
    "일로읍 전세": "https://land.dacgle.com/offer/?cateid_group=0000&trade=2&areaid=001208&areaid2=005",
    "일로읍 월세": "https://land.dacgle.com/offer/?cateid_group=0000&trade=3&areaid=001208&areaid2=005",
    "삼향읍 전세": "https://land.dacgle.com/offer/?cateid_group=0000&trade=2&areaid=001208&areaid2=003",
    "삼향읍 월세": "https://land.dacgle.com/offer/?cateid_group=0000&trade=3&areaid=001208&areaid2=003",
}

# 네이버 부동산 모바일 API — 인증 토큰 없이 동작.
# cortarNo 는 통계청 행정구역 코드 10자리 (읍/면/동 단위 뒤 2자리는 00).
NAVER_API_URL = "https://m.land.naver.com/cluster/ajax/articleList"
NAVER_MOBILE_UA = (
    "Mozilla/5.0 (Linux; Android 12; SM-G991N) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Mobile Safari/537.36"
)
# cortarNo + 지도 bbox 한 쌍. m.land cluster API 는 bbox 도 함께 요구함.
# 무안 일로읍/삼향읍의 대략적인 행정구역 외접 박스 (위/경도 마진 여유 있게).
NAVER_REGIONS = {
    "일로읍": {
        "cortarNo": "4684033000",
        "lat": "34.876", "lon": "126.485",
        "btm": "34.820", "lft": "126.400",
        "top": "34.920", "rgt": "126.570",
    },
    "삼향읍": {
        "cortarNo": "4684032000",
        "lat": "34.811", "lon": "126.466",
        "btm": "34.770", "lft": "126.410",
        "top": "34.850", "rgt": "126.520",
    },
}
# B1=전세, B2=월세 — 한 번에 묶어 호출.
NAVER_TRADE_CODES = "B1:B2"
NAVER_MAX_PAGES = 10  # safety cap

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


def fetch_naver_articles(session: requests.Session, region_name: str, region_info: dict) -> list[dict]:
    """네이버 모바일 cluster API 로 한 지역의 전세+월세 매물을 페이지네이션 수집."""
    headers = {
        "User-Agent": NAVER_MOBILE_UA,
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://m.land.naver.com/",
        "X-Requested-With": "XMLHttpRequest",
    }
    collected: list[dict] = []
    for page in range(1, NAVER_MAX_PAGES + 1):
        params = {
            "rletTpCd": "APT",
            "tradTpCd": NAVER_TRADE_CODES,
            "z": "13",
            "lat": region_info["lat"],
            "lon": region_info["lon"],
            "btm": region_info["btm"],
            "lft": region_info["lft"],
            "top": region_info["top"],
            "rgt": region_info["rgt"],
            "cortarNo": region_info["cortarNo"],
            "showR0": "",
            "sort": "rank",
            "page": str(page),
        }
        r = session.get(NAVER_API_URL, params=params, headers=headers, timeout=30)
        if r.status_code != 200:
            raise RuntimeError(
                f"HTTP {r.status_code} (params={params}): {r.text[:200]}"
            )
        try:
            data = r.json()
        except ValueError as e:
            raise RuntimeError(
                f"Naver API non-JSON (HTTP {r.status_code}): {r.text[:200]}"
            ) from e
        body = data.get("body") or []
        for it in body:
            it["_region"] = region_name
        collected.extend(body)
        if not data.get("more") or not body:
            break
    return collected


def _naver_price(item: dict) -> str:
    """전세는 hanPrc 그대로, 월세는 보증금/월세 만원 포맷."""
    trade = item.get("tradTpCd")
    han = (item.get("hanPrc") or "").strip()
    prc = item.get("prc") or 0
    rent = item.get("rentPrc") or 0
    if trade == "B2":  # 월세
        return f"{prc:,}/{rent:,}만원" if (prc or rent) else (han or "")
    return han or (f"{prc:,}만원" if prc else "")


def format_naver_listing(item: dict) -> str:
    region = item.get("_region", "")
    trade_nm = item.get("tradTpNm", "")
    price = _naver_price(item)
    header = f"[{region} {trade_nm}" + (f" · {price}" if price else "") + "]"

    name = item.get("atclNm") or "(단지명 없음)"
    rlet = item.get("rletTpNm") or ""
    title = f"{name}" + (f" ({rlet})" if rlet else "")

    meta = []
    spc1 = item.get("spc1")  # 공급면적
    spc2 = item.get("spc2")  # 전용면적
    if spc1 or spc2:
        area_parts = []
        if spc1:
            area_parts.append(f"{spc1}㎡")
        if spc2:
            area_parts.append(f"전용 {spc2}㎡")
        meta.append("/".join(area_parts))
    flr = item.get("flrInfo")
    if flr:
        meta.append(f"{flr}층")
    direction = item.get("direction")
    if direction:
        meta.append(direction)
    cfm = item.get("atclCfmYmd")
    if cfm:
        meta.append(cfm)

    lines = [header, title]
    if meta:
        lines.append(" · ".join(meta))
    desc = (item.get("atclFetrDesc") or "").strip()
    if desc:
        lines.append(desc)
    rltr = (item.get("rltrNm") or "").strip()
    if rltr:
        lines.append(f"중개: {rltr}")
    atcl_no = item.get("atclNo")
    if atcl_no:
        lines.append(f"https://new.land.naver.com/articles/{atcl_no}")
    return "\n".join(lines)


def chunk_naver_messages(intro: str, items: list[dict], limit: int = 3800) -> list[str]:
    chunks: list[str] = []
    buf = intro
    for it in items:
        block = "\n\n" + format_naver_listing(it)
        if len(buf) + len(block) > limit and buf != intro:
            chunks.append(buf)
            buf = intro + block
        else:
            buf += block
    if buf:
        chunks.append(buf)
    return chunks


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


ZERO_STREAK_WARN = 3  # 연속 N회 양쪽 0건이면 1회 silent-failure 경고


def main() -> int:
    sess = requests.Session()
    sess.headers.update(HEADERS)

    items_by_trade: dict[str, list[dict]] = {}
    fetch_errors: list[str] = []
    for label, url in SOURCES.items():
        try:
            html = fetch_html(sess, url)
        except requests.RequestException as e:
            print(f"[ERROR] fetch {label}: {e}", file=sys.stderr)
            fetch_errors.append(label)
            continue
        items = parse_listings(html, label)
        print(f"[fetch] {label}: {len(items)}건")
        items_by_trade[label] = items

    if len(fetch_errors) == len(SOURCES):
        print("[ERROR] 모든 소스 fetch 실패 — 종료.", file=sys.stderr)
        return 1

    all_items: list[dict] = [it for lst in items_by_trade.values() for it in lst]

    state = load_state()
    state.pop("seen", None)  # 더 이상 사용하지 않음 — 누적된 키 정리
    now = datetime.now(KST).isoformat(timespec="seconds")

    zero_streak = int(state.get("zero_streak", 0))
    if not all_items and not fetch_errors:
        zero_streak += 1
    else:
        zero_streak = 0
    state["zero_streak"] = zero_streak
    state["last_run"] = now
    if fetch_errors:
        state["last_partial_failure"] = {"at": now, "sources": fetch_errors}
    else:
        state.pop("last_partial_failure", None)

    silent_warning = None
    if zero_streak == ZERO_STREAK_WARN:
        silent_warning = (
            f"⚠️ 무안 모니터 경고: {ZERO_STREAK_WARN}일 연속 매물 0건입니다.\n"
            "사이트 구조 변경 또는 차단 가능성이 있습니다.\n"
            "https://github.com/shway81-droid/muanwolse/actions"
        )

    counts = " / ".join(f"{lbl} {len(items_by_trade.get(lbl, []))}건" for lbl in SOURCES)
    partial_note = f" (※ {', '.join(fetch_errors)} fetch 실패)" if fetch_errors else ""
    intro = f"🏠 무안 일로읍·삼향읍 매물 현황 — {counts}{partial_note}\n({now})"

    if all_items:
        messages = chunk_messages(intro, all_items)
    else:
        messages = [intro + "\n\n현재 등록된 매물이 없습니다."]

    # ---- 네이버 부동산: 별도 메시지로 분리 발송 ----
    naver_by_region: dict[str, list[dict]] = {}
    naver_errors: list[str] = []
    naver_error_details: list[str] = []
    for region_name, region_info in NAVER_REGIONS.items():
        try:
            naver_by_region[region_name] = fetch_naver_articles(sess, region_name, region_info)
            print(f"[fetch] 네이버 {region_name}: {len(naver_by_region[region_name])}건")
        except Exception as e:
            msg = str(e)
            print(f"[ERROR] 네이버 fetch {region_name}: {msg}", file=sys.stderr)
            naver_errors.append(region_name)
            naver_error_details.append(f"• {region_name}: {msg[:300]}")
    naver_all = [it for lst in naver_by_region.values() for it in lst]

    naver_counts = " / ".join(
        f"{rn} {len(naver_by_region.get(rn, []))}건" for rn in NAVER_REGIONS
    )
    naver_partial = f" (※ {', '.join(naver_errors)} fetch 실패)" if naver_errors else ""
    naver_intro = (
        f"🏘️ [네이버 부동산] 무안 일로읍·삼향읍 아파트 전세/월세 — "
        f"{naver_counts}{naver_partial}\n({now})"
    )
    if naver_all:
        naver_messages = chunk_naver_messages(naver_intro, naver_all)
    elif len(naver_errors) == len(NAVER_REGIONS):
        detail = "\n".join(naver_error_details)
        naver_messages = [
            naver_intro + "\n\n네이버 부동산 fetch 에 실패했습니다.\n" + detail
        ]
    else:
        naver_messages = [naver_intro + "\n\n현재 등록된 매물이 없습니다."]
    messages.extend(naver_messages)

    if silent_warning:
        messages.append(silent_warning)

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
    print(f"[sent] 매물 {len(all_items)}건 ({len(messages)}개 메시지) 발송 완료.")
    if silent_warning:
        print("[sent] silent-failure 경고 포함.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
