"""실시간 시세 보조 (Finviz quote 스냅샷 + yfinance 폴백).

일배치 운영에서는 일봉 마지막 봉이 곧 '현재가'이므로 기본적으로 수집된
일봉 DataFrame에서 시세를 계산한다(추가 네트워크/실패 위험 없음).
Finviz 스냅샷은 best-effort 보조이며 실패하면 조용히 무시한다.
"""
from __future__ import annotations

import os
import re

import pandas as pd
import requests

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
}
_QUOTE_URL = "https://finviz.com/quote.ashx?t={t}"
_PRICE_RE = re.compile(r'"price"\s*:\s*"?([\d.]+)"?', re.I)


def quote_from_df(daily: pd.DataFrame) -> dict:
    """수집된 일봉 DataFrame에서 시세 요약 계산."""
    out = {"price": None, "change_pct": None, "volume": None,
           "volume_ratio": None}
    if daily is None or daily.empty:
        return out
    close = daily["Close"].dropna()
    if close.empty:
        return out
    out["price"] = float(close.iloc[-1])
    if len(close) >= 2:
        out["change_pct"] = float(close.iloc[-1] / close.iloc[-2] - 1) * 100
    if "Volume" in daily.columns:
        vol = daily["Volume"].dropna()
        if not vol.empty:
            out["volume"] = float(vol.iloc[-1])
            if len(vol) >= 20:
                v20 = float(vol.tail(20).mean())
                out["volume_ratio"] = (float(vol.iloc[-1]) / v20) if v20 else None
    return out


def fetch_finviz_quote(ticker: str, timeout: float = 8.0) -> dict | None:
    """Finviz quote 페이지에서 현재가 best-effort 추출. 실패 시 None."""
    try:
        r = requests.get(_QUOTE_URL.format(t=ticker.upper()),
                         headers=_HEADERS, timeout=timeout)
        r.raise_for_status()
        m = _PRICE_RE.search(r.text)
        if m:
            return {"price": float(m.group(1))}
    except Exception:  # noqa: BLE001
        return None
    return None


def get_quote(ticker: str, daily: pd.DataFrame, use_finviz: bool = False) -> dict:
    """시세 요약. 기본은 일봉 기반, use_finviz면 Finviz 현재가로 price만 보정."""
    q = quote_from_df(daily)
    if use_finviz:
        fv = fetch_finviz_quote(ticker)
        if fv and fv.get("price"):
            q["price"] = fv["price"]
    return q


# ---------- 장중 실시간 시세 (price_monitor 전용) ----------

# Finnhub 실시간 quote (무료 티어, 미국 주식 실시간). FINNHUB_API_KEY 필요.
_FINNHUB_URL = "https://finnhub.io/api/v1/quote"


def _finnhub_key() -> str | None:
    return os.environ.get("FINNHUB_API_KEY") or None


def finnhub_enabled() -> bool:
    return bool(_finnhub_key())


def finnhub_quote(symbol: str, timeout: float = 8.0) -> dict | None:
    """Finnhub 실시간 quote → {price, prev_close, change_pct} 또는 None.

    무료 티어는 미국 주식 실시간 체결가를 제공한다(거래량은 미포함).
    한국 종목 등 미지원 심볼은 c=0으로 와서 None 처리 → yfinance로 폴백된다.
    """
    key = _finnhub_key()
    if not key:
        return None
    try:
        r = requests.get(_FINNHUB_URL,
                         params={"symbol": symbol.upper(), "token": key},
                         timeout=timeout)
        r.raise_for_status()
        j = r.json()
        c = j.get("c")
        if not c or float(c) <= 0:
            return None
        return {"price": float(c), "prev_close": j.get("pc"),
                "change_pct": j.get("dp")}
    except Exception as e:  # noqa: BLE001
        print(f"[시세] {symbol} Finnhub 실패: {e}")
        return None


# CNBC 시세 (무료, 프리/애프터마켓 포함 — 무료 소스 중 가장 신선).
_CNBC_URL = (
    "https://quote.cnbc.com/quote-html-webservice/restQuote/symbolType/symbol"
    "?symbols={t}&requestMethod=itv&noform=1&fund=1&exthrs=1&output=json"
)


def cnbc_quote(symbol: str, timeout: float = 8.0) -> dict | None:
    """CNBC 시세 → {regular, extended, ext_type, ext_time} 또는 None.

    regular  : 정규장 마지막가(장중엔 실시간급)
    extended : 프리/애프터마켓 마지막 체결가 (있을 때만)
    무료 소스 중 시간외를 가장 잘 잡지만, 오버나잇(8PM~4AM) 세션은 미포함.
    """
    try:
        r = requests.get(_CNBC_URL.format(t=symbol.upper()),
                         headers=_HEADERS, timeout=timeout)
        r.raise_for_status()
        q = r.json()["FormattedQuoteResult"]["FormattedQuote"][0]
        ext = q.get("ExtendedMktQuote") or {}
        return {
            "regular": _to_num(q.get("last")),
            "extended": _to_num(ext.get("last")),
            "ext_type": ext.get("type"),
            "ext_time": ext.get("last_timedate"),
        }
    except Exception as e:  # noqa: BLE001
        print(f"[시세] {symbol} CNBC 실패: {e}")
        return None


# Finviz 스냅샷 테이블 셀: 라벨/값이 번갈아 나온다.
_CELL_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.S)
_TAG_RE = re.compile(r"<[^>]+>")


def _to_num(text: str) -> float | None:
    """'45.67M' / '12,345,678' / '134.20' 등을 숫자로 변환."""
    if not text:
        return None
    t = text.strip().replace(",", "")
    if t in ("-", "", "N/A"):
        return None
    mult = 1.0
    if t[-1] in "KMBT":
        mult = {"K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}[t[-1]]
        t = t[:-1]
    try:
        return float(t) * mult
    except ValueError:
        return None


def _parse_finviz_snapshot(htmltext: str) -> dict:
    """Finviz quote 페이지 스냅샷 테이블에서 라벨→값 dict 추출."""
    cells = [_TAG_RE.sub("", c).strip() for c in _CELL_RE.findall(htmltext)]
    out: dict[str, str] = {}
    for i in range(0, len(cells) - 1, 2):
        label = cells[i]
        if label:
            out[label] = cells[i + 1]
    return out


def _yf_intraday_price(symbol: str, prepost: bool = True) -> float | None:
    """yfinance 시간외 포함 1분봉 마지막 체결가 → 실패 시 일봉 종가."""
    try:
        import yfinance as yf
        tk = yf.Ticker(symbol)
        m = tk.history(period="2d", interval="1m", prepost=prepost,
                       auto_adjust=True)
        if m is not None and not m.empty:
            c = m["Close"].dropna()
            if not c.empty:
                return float(c.iloc[-1])
        d = tk.history(period="5d", interval="1d", auto_adjust=True)
        if d is not None and not d.empty:
            c = d["Close"].dropna()
            if not c.empty:
                return float(c.iloc[-1])
    except Exception as e:  # noqa: BLE001
        print(f"[시세] {symbol} yfinance 최신가 실패: {e}")
    return None


def latest_price(symbol: str, prepost: bool = True) -> float | None:
    """가장 신선한 체결가 (KIS 데이마켓 우선, 시간대 인지).

    0) KIS: 데이장/프리/정규/애프터 전 세션 (키 있을 때만, 오버나잇 유일 소스).
    1) CNBC 크롤: 장 외엔 시간외(extended), 장중엔 정규(regular) 가격.
    2) 폴백: 정규장이면 Finnhub 실시간, 장 외면 yfinance 시간외 1분봉.
    3) 최후: Finnhub 마지막가.
    """
    # 0) KIS (데이마켓 포함 전 세션) — 무료 소스가 못 잡는 오버나잇 커버
    try:
        import kis
        if kis.enabled():
            q = kis.quote(symbol)
            if q and q.get("price"):
                return q["price"]
    except Exception as e:  # noqa: BLE001
        print(f"[시세] {symbol} KIS 실패: {e}")

    try:
        from utils import is_market_open
        open_now = is_market_open()
    except Exception:  # noqa: BLE001
        open_now = True

    # 1) 크롤 (CNBC) 우선
    c = cnbc_quote(symbol)
    if c:
        if not open_now and c["extended"]:
            return c["extended"]
        if c["regular"]:
            return c["regular"]
        if c["extended"]:
            return c["extended"]

    # 2) 폴백
    if open_now:
        q = finnhub_quote(symbol)
        if q:
            return q["price"]
    yp = _yf_intraday_price(symbol, prepost)
    if yp is not None:
        return yp

    # 3) 최후
    q = finnhub_quote(symbol)
    return q["price"] if q else None


def base_price(symbol: str, prepost: bool = True) -> float | None:
    """풀 분석 기준가(장 시작가급): 파이낸셜(yfinance) 최신가 — 크롤(CNBC) 미사용.

    yfinance(시간외 포함 1분봉 최신가 → 일봉 종가) 우선, 실패 시 Finnhub.
    Finnhub 무료 quote는 시간외를 안 잡아 정규 종가만 주므로(예: 195.74),
    시간외까지 반영하는 yfinance 최신가(예: 194.80)를 기준가로 쓴다.
    실시간 감시(get_realtime)만 크롤(CNBC)을 쓴다(사용자 정책).
    """
    p = _yf_intraday_price(symbol, prepost)
    if p is not None:
        return p
    q = finnhub_quote(symbol)
    return q["price"] if q else None


def _yf_realtime(symbol: str) -> tuple[float | None, float | None, float | None]:
    """yfinance 폴백: (현재가, 당일 거래량, 20일 평균 거래량).

    현재가는 시간외 포함 1분봉으로 최대한 신선하게, 거래량은 일봉 기준.
    """
    try:
        import yfinance as yf
        tk = yf.Ticker(symbol)
        price = latest_price(symbol)
        df = tk.history(period="40d", interval="1d", auto_adjust=True)
        volume = avg_volume = None
        if df is not None and not df.empty:
            if price is None:
                price = float(df["Close"].dropna().iloc[-1])
            vol = df["Volume"].dropna() if "Volume" in df.columns else pd.Series(dtype=float)
            volume = float(vol.iloc[-1]) if not vol.empty else None
            avg_volume = float(vol.tail(20).mean()) if len(vol) >= 5 else None
        return price, volume, avg_volume
    except Exception as e:  # noqa: BLE001
        print(f"[시세] {symbol} yfinance 폴백 실패: {e}")
        return None, None, None


def get_realtime(symbol: str, timeout: float = 8.0
                 ) -> tuple[float | None, float | None, float | None]:
    """장중 실시간 시세. (현재가, 거래량, 20일 평균거래량) 반환.

    가격: latest_price(크롤 우선, 시간대 인지). 거래량/평균거래량: Finviz → yfinance.
    """
    price = latest_price(symbol)
    volume = avg_volume = None
    try:
        r = requests.get(_QUOTE_URL.format(t=symbol.upper()),
                         headers=_HEADERS, timeout=timeout)
        r.raise_for_status()
        snap = _parse_finviz_snapshot(r.text)
        if price is None:          # 가격 폴백
            price = _to_num(snap.get("Price", ""))
        volume = _to_num(snap.get("Volume", ""))
        avg_volume = _to_num(snap.get("Avg Volume", ""))
    except Exception as e:  # noqa: BLE001
        print(f"[시세] {symbol} Finviz 실패: {e}")

    if price is None or volume is None or avg_volume is None:
        yp, yv, ya = _yf_realtime(symbol)
        price = price if price is not None else yp
        volume = volume if volume is not None else yv
        avg_volume = avg_volume if avg_volume is not None else ya
    return price, volume, avg_volume
