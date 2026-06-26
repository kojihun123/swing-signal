"""공통 유틸리티: 시간대 변환, 파일 로드, 포맷팅."""
import json
import os
from datetime import datetime
from zoneinfo import ZoneInfo

# 분석 기준 시간대(미국 동부)와 출력 표시 시간대(한국)
MARKET_TZ = ZoneInfo("America/New_York")
NOTIFY_TZ = ZoneInfo(os.environ.get("NOTIFY_TZ", "Asia/Seoul"))


def now_market() -> datetime:
    """현재 미국 동부 시간."""
    return datetime.now(MARKET_TZ)


def now_notify() -> datetime:
    """현재 출력 표시 시간대(KST) 기준 시간."""
    return datetime.now(NOTIFY_TZ)


def to_notify(dt: datetime) -> datetime:
    """임의의 datetime을 출력 표시 시간대로 변환."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=MARKET_TZ)
    return dt.astimezone(NOTIFY_TZ)


def fmt_kst(dt: datetime | None = None) -> str:
    """KST 'YYYY-MM-DD HH:MM KST' 포맷 문자열."""
    if dt is None:
        dt = now_notify()
    else:
        dt = to_notify(dt)
    return dt.strftime("%Y-%m-%d %H:%M KST")


def today_stamp() -> str:
    """리포트 파일명용 날짜 스탬프 (미국 장 기준 날짜)."""
    return now_market().strftime("%Y%m%d")


def load_watchlist(path: str = "watchlist.json") -> list[dict]:
    """watchlist.json에서 종목 항목을 로드.

    두 가지 형식을 모두 지원한다.
      1) 신규: {"tickers": [{"symbol": "NVDA", "sector_etf": "SOXX",
                              "sector_name": "반도체"}, ...]}
      2) 구버전: ["NVDA", "AAPL", ...]  (섹터 정보 없음 → None)

    반환: [{"symbol": str, "sector_etf": str|None, "sector_name": str|None}, ...]
    """
    if not os.path.exists(path):
        raise FileNotFoundError(f"watchlist 파일을 찾을 수 없습니다: {path}")
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    if isinstance(data, dict):
        items = data.get("tickers", [])
    elif isinstance(data, list):
        items = data
    else:
        raise ValueError("watchlist.json 형식이 올바르지 않습니다.")

    out: list[dict] = []
    for it in items:
        if isinstance(it, str):
            sym = it.strip().upper()
            if sym:
                out.append({"symbol": sym, "sector_etf": None, "sector_name": None})
        elif isinstance(it, dict):
            sym = str(it.get("symbol", "")).strip().upper()
            if sym:
                out.append({
                    "symbol": sym,
                    "sector_etf": (str(it["sector_etf"]).strip().upper()
                                   if it.get("sector_etf") else None),
                    "sector_name": (str(it["sector_name"]).strip()
                                    if it.get("sector_name") else None),
                })
    return out


def watchlist_symbols(entries: list[dict]) -> list[str]:
    """워치리스트 항목 리스트에서 심볼만 추출."""
    return [e["symbol"] for e in entries]


def pct(value: float, digits: int = 1) -> str:
    """부호 포함 퍼센트 문자열. 예: +5.1%"""
    if value is None:
        return "N/A"
    return f"{value:+.{digits}f}%"


# 통화 코드 -> (기호, 기본 소수자리)
_CURRENCY = {
    "USD": ("$", 2), "KRW": ("₩", 0), "JPY": ("¥", 0), "EUR": ("€", 2),
    "GBP": ("£", 2), "HKD": ("HK$", 2), "CNY": ("¥", 2), "TWD": ("NT$", 1),
}


def money(value: float, currency: str = "USD", digits: int | None = None) -> str:
    """통화 자동 표시. 예: $134.20 / ₩345,500"""
    if value is None or value != value:
        return "N/A"
    sym, dd = _CURRENCY.get((currency or "USD").upper(), ("", 2))
    if digits is None:
        digits = dd
    if sym:
        return f"{sym}{value:,.{digits}f}"
    return f"{value:,.{digits}f} {currency}"


def safe_num(value, digits: int = 1, suffix: str = "") -> str:
    """None/NaN 안전 숫자 포맷."""
    try:
        if value is None:
            return "N/A"
        f = float(value)
        if f != f:  # NaN
            return "N/A"
        return f"{f:.{digits}f}{suffix}"
    except (TypeError, ValueError):
        return "N/A"


# ---------- 등급 / 신호 ----------

# (하한, 상한) -> (별점, 영문등급, 한글)
GRADES = [
    (90, 100, "★★★★★", "Strong Buy", "적극매수"),
    (80, 89, "★★★★☆", "Buy", "매수"),
    (70, 79, "★★★☆☆", "Watch", "관심"),
    (60, 69, "★★☆☆☆", "Neutral", "중립"),
    (0, 59, "★☆☆☆☆", "Avoid", "회피"),
]


def grade_for(score: float) -> dict:
    """점수 -> {stars, en, ko}."""
    s = max(0.0, min(100.0, float(score)))
    for lo, hi, stars, en, ko in GRADES:
        if lo <= s <= hi:
            return {"stars": stars, "en": en, "ko": ko}
    return {"stars": "★☆☆☆☆", "en": "Avoid", "ko": "회피"}


# ---------- 날짜 헬퍼 ----------


def third_friday(year: int, month: int):
    """해당 월의 3째주 금요일(옵션 만기일) date 반환."""
    from datetime import date
    d = date(year, month, 1)
    # 첫 금요일
    first_friday = 1 + (4 - d.weekday()) % 7
    return date(year, month, first_friday + 14)


def is_options_expiry(d=None) -> bool:
    """주어진 날짜(미국장 기준)가 월 옵션 만기일(3째주 금)인지."""
    d = d or now_market().date()
    return d == third_friday(d.year, d.month)


def is_market_open(dt: datetime | None = None) -> bool:
    """미국 정규장 개장 여부 (ET 평일 09:30~16:00). 공휴일은 고려하지 않음."""
    dt = dt or now_market()
    dt = dt.astimezone(MARKET_TZ)
    if dt.weekday() >= 5:            # 주말 제외
        return False
    minutes = dt.hour * 60 + dt.minute
    return 9 * 60 + 30 <= minutes <= 16 * 60


# ---------- 미국 전 세션 (데이마켓/프리/정규/애프터) ----------
#
# ET 기준 하루 흐름 (공휴일 미고려):
#   04:00~09:30  프리마켓(pre)
#   09:30~16:00  정규장(regular)
#   16:00~20:00  애프터마켓(after)
#   20:00~04:00  데이마켓/주간거래(daymarket, Blue Ocean 오버나잇)
# 주말 휴장: 금 20:00 ET ~ 일 20:00 ET.

def current_session(dt: datetime | None = None) -> str:
    """현재 미국 세션. 'pre'|'regular'|'after'|'daymarket'|'closed'."""
    dt = (dt or now_market()).astimezone(MARKET_TZ)
    wd = dt.weekday()                       # 월=0 ... 금=4, 토=5, 일=6
    m = dt.hour * 60 + dt.minute
    PRE, OPEN, CLOSE, AFT = 4 * 60, 9 * 60 + 30, 16 * 60, 20 * 60

    # 평일 주간 세션 (프리/정규/애프터)
    if wd < 5:
        if PRE <= m < OPEN:
            return "pre"
        if OPEN <= m < CLOSE:
            return "regular"
        if CLOSE <= m < AFT:
            return "after"

    # 데이마켓(오버나잇) — 저녁 20:00~24:00
    if m >= AFT:
        # 금(4)·토(5) 저녁은 주말 휴장 시작
        return "closed" if wd in (4, 5) else "daymarket"
    # 데이마켓 — 새벽 00:00~04:00 (전날 저녁의 연장)
    if m < PRE:
        # 토(5)·일(6) 새벽은 휴장
        return "closed" if wd in (5, 6) else "daymarket"
    return "closed"


# 세션명 → 한글 라벨
SESSION_KO = {
    "pre": "프리마켓", "regular": "정규장", "after": "애프터마켓",
    "daymarket": "데이마켓", "closed": "휴장",
}


def is_session_open(dt: datetime | None = None) -> bool:
    """미국이 거래 중인지(프리/정규/애프터/데이마켓 중 하나라도)."""
    return current_session(dt) != "closed"
