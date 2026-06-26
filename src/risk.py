"""리스크 분석 (이벤트 기반 감점).

감점 항목
  어닝 발표 ±3일      -10
  FOMC ±2일           -5
  CPI/PPI 발표 ±1일   -5
  옵션 만기일 당일    -3   (매월 3째주 금)
  공매도 비중 과다    -7   (float 대비 15% 초과)
  규제 관련 뉴스       -5
  배당락일 당일        -3

FOMC/CPI 일정은 2026년 기준 하드코딩(분기마다 갱신 필요).
"""
from __future__ import annotations

from datetime import date, datetime, timezone

from utils import is_options_expiry, now_market

# 2026 FOMC 결정일 (둘째 날 기준, 대략치 — 분기별 갱신 권장)
FOMC_DATES_2026 = [
    date(2026, 1, 28), date(2026, 3, 18), date(2026, 4, 29), date(2026, 6, 17),
    date(2026, 7, 29), date(2026, 9, 16), date(2026, 10, 28), date(2026, 12, 9),
]
# 2026 CPI 발표일 (대략치 — 매월 중순)
CPI_DATES_2026 = [
    date(2026, 1, 13), date(2026, 2, 11), date(2026, 3, 11), date(2026, 4, 10),
    date(2026, 5, 12), date(2026, 6, 10), date(2026, 7, 14), date(2026, 8, 12),
    date(2026, 9, 11), date(2026, 10, 13), date(2026, 11, 12), date(2026, 12, 10),
]

REG_KEYWORDS = ("regulat", "antitrust", "probe", "investigation", "lawsuit",
                "ban", "tariff", "sanction", "sec ", "ftc", "doj", "subpoena")


def _within(target: date, today: date, days: int) -> bool:
    return abs((target - today).days) <= days


def _epoch_to_date(v):
    try:
        if v is None:
            return None
        return datetime.fromtimestamp(float(v), tz=timezone.utc).date()
    except (TypeError, ValueError, OSError):
        return None


def analyze_risk(data, fundamental: dict, news: list,
                 today: date | None = None) -> dict:
    today = today or now_market().date()
    raw = data.raw or {}
    items = []

    # 어닝 ±3일
    days = (fundamental.get("metrics", {}) or {}).get("days_to_earnings")
    if days is not None and abs(int(days)) <= 3:
        items.append({"name": f"어닝 발표 D{int(days):+d}", "points": -10})

    # FOMC ±2일
    if any(_within(d, today, 2) for d in FOMC_DATES_2026):
        items.append({"name": "FOMC 임박(±2일)", "points": -5})

    # CPI/PPI ±1일
    if any(_within(d, today, 1) for d in CPI_DATES_2026):
        items.append({"name": "CPI/PPI 발표 임박(±1일)", "points": -5})

    # 옵션 만기일 당일
    if is_options_expiry(today):
        items.append({"name": "옵션 만기일", "points": -3})

    # 공매도 비중 과다 (float 대비 15% 초과)
    sf = raw.get("shortPercentOfFloat")
    try:
        if sf is not None and float(sf) > 0.15:
            items.append({"name": f"공매도 비중 {float(sf)*100:.0f}%", "points": -7})
    except (TypeError, ValueError):
        pass

    # 배당락일 당일
    exd = _epoch_to_date(raw.get("exDividendDate"))
    if exd is not None and exd == today:
        items.append({"name": "배당락일", "points": -3})

    # 규제 관련 뉴스
    for n in news or []:
        hl = (getattr(n, "headline", "") or "").lower()
        if any(k in hl for k in REG_KEYWORDS):
            items.append({"name": "규제 관련 뉴스", "points": -5})
            break

    deduction = sum(it["points"] for it in items)
    return {"deduction": deduction, "items": items}
