"""토스증권 Open API 데이터 레이어 (OAuth2 · 한미 통일 심볼).

베이스 https://openapi.tossinvest.com · 인증 OAuth2 Client Credentials.
심볼은 KR 6자리(005930)·US 영문티커(AAPL)로 통일 — 내부 '.KS/.KQ'는 벗겨서 전달.

  ohlcv(symbol, "D"|"W", count)  → OHLCV DataFrame(오름차순, collector 호환)
  prices(symbols)                → {원본심볼: 현재가}  (최대 200종목 1콜)
  price(symbol)                  → 단일 현재가
  market_calendar("US"|"KR")     → 세션별 운영시간(휴장 시 null) — DST/공휴일 자동

TOSS_CLIENT_ID/SECRET 없으면 enabled()=False, 모든 호출은 None/[]/{} 폴백.
토큰은 24h 유효 — 메모리 + 디스크 캐시로 재사용.
"""
from __future__ import annotations

import os
import time
from datetime import datetime

import pandas as pd
import requests

from cache import load_data, save_cache

BASE = "https://openapi.tossinvest.com"
_TOKEN_CACHE = "toss_token.json"
_OHLCV_COLS = ["Open", "High", "Low", "Close", "Volume"]

_token: tuple[str, float] | None = None   # (access_token, expiry_epoch)


def _cid() -> str | None:
    return os.environ.get("TOSS_CLIENT_ID") or None


def _sec() -> str | None:
    return os.environ.get("TOSS_CLIENT_SECRET") or None


def enabled() -> bool:
    return bool(_cid() and _sec())


def _is_kr(symbol: str) -> bool:
    return symbol.upper().endswith((".KS", ".KQ"))


def _symbol(symbol: str) -> str:
    """내부 심볼 → 토스 심볼. 005930.KS→005930, AAPL→AAPL."""
    return symbol.split(".")[0] if _is_kr(symbol) else symbol


def _num(v):
    if v in (None, "", "-"):
        return None
    try:
        return float(str(v).replace(",", "").strip())
    except (ValueError, TypeError):
        return None


def _access_token() -> str | None:
    """유효 토큰 반환(메모리→디스크 캐시→신규발급). 실패 시 None."""
    global _token
    now = time.time()
    if _token and _token[1] - 60 > now:
        return _token[0]
    cached = load_data(_TOKEN_CACHE) or {}
    if cached.get("access_token") and float(cached.get("expiry", 0)) - 60 > now:
        _token = (cached["access_token"], float(cached["expiry"]))
        return _token[0]
    cid, sec = _cid(), _sec()
    if not (cid and sec):
        return None
    try:
        r = requests.post(BASE + "/oauth2/token", timeout=15, data={
            "grant_type": "client_credentials",
            "client_id": cid, "client_secret": sec})
        r.raise_for_status()
        j = r.json()
        at = j.get("access_token")
        if not at:
            return None
        exp = now + float(j.get("expires_in", 86400))
        _token = (at, exp)
        try:
            save_cache(_TOKEN_CACHE, {"access_token": at, "expiry": exp})
        except Exception:  # noqa: BLE001
            pass
        return at
    except Exception as e:  # noqa: BLE001
        print(f"[토스] 토큰 발급 실패: {e}")
        return None


def _get(path: str, params: dict | None = None):
    at = _access_token()
    if not at:
        return None
    try:
        r = requests.get(BASE + path, params=params or {}, timeout=15,
                         headers={"Authorization": f"Bearer {at}"})
        r.raise_for_status()
        return r.json()
    except Exception as e:  # noqa: BLE001
        print(f"[토스] {path} 요청 실패: {e}")
        return None


# ── 캔들(OHLCV) ────────────────────────────────────────────────────────

def candles(symbol: str, interval: str = "1d", count: int = 200) -> list[dict]:
    """원시 캔들 리스트(최신순). 200봉/콜 한계 → nextBefore로 페이지네이션."""
    sym = _symbol(symbol)
    out: list[dict] = []
    before: str | None = None
    while len(out) < count:
        params = {"symbol": sym, "interval": interval,
                  "count": min(200, count - len(out)), "adjusted": "true"}
        if before:
            params["before"] = before
        res = (_get("/api/v1/candles", params) or {}).get("result") or {}
        batch = res.get("candles") or []
        if not batch:
            break
        out.extend(batch)
        before = res.get("nextBefore")
        if not before:
            break
    return out[:count]


def ohlcv(symbol: str, period: str = "D", count: int = 220) -> pd.DataFrame:
    """OHLCV DataFrame(오름차순 DatetimeIndex). period='W'는 일봉을 주봉 리샘플.

    naver.ohlcv와 동일 포맷(오름차순 DatetimeIndex + OHLCV). 실패 시 빈 DataFrame.
    """
    if not enabled():
        return pd.DataFrame(columns=_OHLCV_COLS)
    need = count * 6 if period == "W" else count   # 주봉 52개 ≈ 일봉 ~260
    raw = candles(symbol, "1d", min(need, 2000))
    if not raw:
        return pd.DataFrame(columns=_OHLCV_COLS)
    rows: list[tuple] = []
    for c in raw:
        try:
            d = datetime.fromisoformat(str(c["timestamp"])).date()
            rows.append((d, float(c["openPrice"]), float(c["highPrice"]),
                         float(c["lowPrice"]), float(c["closePrice"]),
                         float(c.get("volume") or 0)))
        except (KeyError, TypeError, ValueError):
            continue
    if not rows:
        return pd.DataFrame(columns=_OHLCV_COLS)
    rows = sorted(set(rows), key=lambda r: r[0])
    idx = pd.to_datetime([r[0] for r in rows])
    df = pd.DataFrame(
        {"Open": [r[1] for r in rows], "High": [r[2] for r in rows],
         "Low": [r[3] for r in rows], "Close": [r[4] for r in rows],
         "Volume": [r[5] for r in rows]}, index=idx)
    if period == "W":
        df = (df.resample("W-FRI").agg(
            {"Open": "first", "High": "max", "Low": "min",
             "Close": "last", "Volume": "sum"}).dropna(subset=["Close"]))
    return df.tail(count)


# ── 현재가 ─────────────────────────────────────────────────────────────

def prices(symbols: list[str]) -> dict[str, float]:
    """배치 현재가 {원본심볼: 현재가}. 최대 200종목 1콜."""
    if not enabled() or not symbols:
        return {}
    rev = {_symbol(s): s for s in symbols}          # 토스심볼 → 원본심볼
    res = (_get("/api/v1/prices", {"symbols": ",".join(rev)}) or {}).get("result")
    out: dict[str, float] = {}
    for it in (res or []):
        ts, lp = it.get("symbol"), _num(it.get("lastPrice"))
        if ts in rev and lp is not None:
            out[rev[ts]] = lp
    return out


def price(symbol: str) -> float | None:
    return prices([symbol]).get(symbol)


# ── 장 운영 캘린더 (DST·공휴일 자동) ────────────────────────────────────

def market_calendar(market: str = "US", date: str | None = None) -> dict | None:
    """전일/당일/익일 3영업일 세션 운영시간. 휴장 시 세션 null. 실패 시 None.

    반환: {today:{date, dayMarket, preMarket, regularMarket, afterMarket}, ...}
    각 세션은 {startTime, endTime}(ISO8601, KST) 또는 null.
    """
    if not enabled():
        return None
    params = {"date": date} if date else {}
    return (_get(f"/api/v1/market-calendar/{market.upper()}", params) or {}).get("result")
