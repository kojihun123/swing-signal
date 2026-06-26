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


def trading_day_key(dt: datetime | None = None) -> str:
    """인트라데이 누적의 '거래일' 키 = 미국 동부(ET) 날짜 YYYYMMDD.

    미국 세션(KST 밤~아침)이 한 키로 묶이고, ET 날짜가 바뀌면 자동으로
    새 키가 되어 인트라데이 시계열이 초기화된다.
    """
    return (dt or now_market()).astimezone(MARKET_TZ).strftime("%Y%m%d")


def load_watchlist(path: str = "watchlist.json") -> list[dict]:
    """watchlist.json에서 종목 항목을 로드.

    세 가지 형식을 모두 지원한다.
      1) 신규: {"tickers": [{"symbol": "MU", "profile": {"country": "US",
                              "industry": "Memory Semiconductor"}}, ...]}
      2) 레거시: {"tickers": [{"symbol": "NVDA", "sector_etf": "SOXX",
                               "sector_name": "반도체"}, ...]}
      3) 최소: ["NVDA", "AAPL", ...]  (정보 없음 → None)

    반환: [{"symbol": str, "profile": dict|None,
            "sector_etf": str|None, "sector_name": str|None}, ...]
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
                out.append({"symbol": sym, "profile": None,
                            "sector_etf": None, "sector_name": None})
        elif isinstance(it, dict):
            sym = str(it.get("symbol", "")).strip().upper()
            if sym:
                prof = it.get("profile") if isinstance(it.get("profile"), dict) else None
                out.append({
                    "symbol": sym,
                    "name": (str(it["name"]).strip() if it.get("name") else None),
                    "profile": prof,
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


# ---------- 미국 전 세션 (데이마켓/프리/정규/애프터) ----------
#
# 우선순위: 토스증권 장운영 캘린더(공휴일·서머타임 자동) → 실패 시 ET 하드코딩.
# 세션 매핑: dayMarket→daymarket, preMarket→pre, regularMarket→regular,
#            afterMarket→after. 휴장이면 4세션 모두 null → closed.

_TOSS_SESS_MAP = {"dayMarket": "daymarket", "preMarket": "pre",
                  "regularMarket": "regular", "afterMarket": "after"}
_cal_cache: dict = {}    # market → (fetch_epoch, [(session, start_dt, end_dt)])


def _toss_sessions(market: str = "US"):
    """토스 캘린더 → [(session, start_dt, end_dt)] (1시간 캐시). 실패 시 None."""
    import time as _t
    now = _t.time()
    hit = _cal_cache.get(market)
    if hit and now - hit[0] < 3600:
        return hit[1]
    try:
        import toss
        if not toss.enabled():
            return None
        cal = toss.market_calendar(market)
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(cal, dict):
        return None
    out = []
    for day in cal.values():                      # 전일/당일/익일
        if not isinstance(day, dict):
            continue
        for key, sess in _TOSS_SESS_MAP.items():
            w = day.get(key)
            if isinstance(w, dict) and w.get("startTime") and w.get("endTime"):
                try:
                    out.append((sess, datetime.fromisoformat(w["startTime"]),
                                datetime.fromisoformat(w["endTime"])))
                except ValueError:
                    continue
    _cal_cache[market] = (now, out)
    return out


def _session_hardcoded(dt: datetime) -> str:
    """ET 하드코딩 세션(폴백, 공휴일 미고려)."""
    dt = dt.astimezone(MARKET_TZ)
    wd = dt.weekday()
    m = dt.hour * 60 + dt.minute
    PRE, OPEN, CLOSE, AFT = 4 * 60, 9 * 60 + 30, 16 * 60, 20 * 60
    if wd < 5:
        if PRE <= m < OPEN:
            return "pre"
        if OPEN <= m < CLOSE:
            return "regular"
        if CLOSE <= m < AFT:
            return "after"
    if m >= AFT:
        return "closed" if wd in (4, 5) else "daymarket"
    if m < PRE:
        return "closed" if wd in (5, 6) else "daymarket"
    return "closed"


def current_session(dt: datetime | None = None) -> str:
    """현재 미국 세션. 'pre'|'regular'|'after'|'daymarket'|'closed'.

    토스 캘린더(공휴일·DST 자동) 우선, 사용 불가 시 ET 하드코딩 폴백.
    """
    dt = dt or now_market()
    sessions = _toss_sessions("US")
    if sessions:
        for name, start, end in sessions:
            if start <= dt < end:
                return name
        return "closed"
    return _session_hardcoded(dt)


def is_market_open(dt: datetime | None = None) -> bool:
    """미국 정규장 개장 여부 (토스 캘린더 기준, 공휴일·서머타임 자동)."""
    return current_session(dt) == "regular"


# 세션명 → 한글 라벨
SESSION_KO = {
    "pre": "프리마켓", "regular": "정규장", "after": "애프터마켓",
    "daymarket": "데이마켓", "closed": "휴장",
}


def is_session_open(dt: datetime | None = None) -> bool:
    """미국이 거래 중인지(프리/정규/애프터/데이마켓 중 하나라도)."""
    return current_session(dt) != "closed"
