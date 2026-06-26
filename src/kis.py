"""한국투자증권(KIS) 해외주식 시세 — 데이마켓(주간거래) 포함 전 세션 현재가.

무료 공개 소스(CNBC/Finnhub/yfinance)는 미국 오버나잇 세션(데이마켓,
ET 20:00~04:00 ≒ 한국 낮시간)을 제공하지 않는다. KIS는 미국 주간거래
거래소 코드(나스닥 BAQ / 뉴욕 BAY)를 지원하므로, 현재 미국 세션에 맞춰
거래소 코드를 골라 호출하면 데이장/프리/정규/애프터 전 세션 가격을 받는다.

  · 인증: App Key/Secret → OAuth 토큰(24h) 발급 후 파일 캐시 재사용
  · 현재가: /uapi/overseas-price/v1/quotations/price (TR HHDFS00000300)
  · 거래소: 정규 NAS/NYS/AMS ↔ 주간거래 BAQ/BAY/BAA

KIS_APP_KEY / KIS_APP_SECRET 가 없으면 enabled()=False → 호출부에서
기존 무료 소스로 폴백한다.
"""
from __future__ import annotations

import os

import requests

from cache import load_cache, save_cache
from utils import current_session, now_notify

# 실전투자 도메인 (모의투자는 별도 도메인이나 해외 시세는 실전 기준)
_BASE = os.environ.get(
    "KIS_BASE_URL", "https://openapi.koreainvestment.com:9443"
)
_TOKEN_CACHE = "kis_token.json"

# 정규 거래소 ↔ 주간거래(데이마켓) 거래소 코드
_REGULAR = ("NAS", "NYS", "AMS")     # 나스닥 / 뉴욕 / 아멕스
_DAYTIME = ("BAQ", "BAY", "BAA")     # 주간거래: 나스닥 / 뉴욕 / 아멕스

# 심볼 → 직전에 성공한 거래소 코드(그룹 무관) 캐시. 호출 수 절약.
_EXCD_HINT: dict[str, str] = {}


def _key() -> str | None:
    return os.environ.get("KIS_APP_KEY") or None


def _secret() -> str | None:
    return os.environ.get("KIS_APP_SECRET") or None


def enabled() -> bool:
    return bool(_key() and _secret())


# ---------- OAuth 토큰 ----------

def _get_token(force: bool = False) -> str | None:
    """유효한 access token 반환. 파일 캐시(24h) 우선, 만료 임박 시 재발급."""
    if not enabled():
        return None

    if not force:
        wrap = load_cache(_TOKEN_CACHE)
        if wrap and isinstance(wrap.get("data"), dict):
            tok = wrap["data"]
            exp = tok.get("expires_at", "")
            try:
                from datetime import datetime
                if exp and datetime.fromisoformat(exp) > now_notify():
                    return tok.get("access_token")
            except (ValueError, TypeError):
                pass

    # 재발급 (KIS는 분당 1회 발급 제한 → 캐시 필수)
    try:
        r = requests.post(
            f"{_BASE}/oauth2/tokenP",
            json={"grant_type": "client_credentials",
                  "appkey": _key(), "appsecret": _secret()},
            timeout=10,
        )
        r.raise_for_status()
        j = r.json()
        token = j.get("access_token")
        if not token:
            print(f"[KIS] 토큰 발급 응답 이상: {str(j)[:200]}")
            return None
        # expires_in(초) 기준 만료 5분 전으로 캐시
        from datetime import timedelta
        expires_in = int(j.get("expires_in", 86400))
        expires_at = now_notify() + timedelta(seconds=max(60, expires_in - 300))
        save_cache(_TOKEN_CACHE,
                   {"access_token": token, "expires_at": expires_at.isoformat()})
        return token
    except Exception as e:  # noqa: BLE001
        print(f"[KIS] 토큰 발급 실패: {e}")
        return None


# ---------- 세션 판별 ----------

def session_kind(dt=None) -> str:
    """KIS 거래소 선택용 세션 구분. 'overnight'(데이마켓) | 'regular' | 'closed'.

    세션 판별은 utils.current_session()에 일원화한다.
      · daymarket            → overnight(BAQ/BAY 주간거래)
      · pre/regular/after    → regular(NAS/NYS, last가 시간외 반영)
      · closed               → closed
    """
    s = current_session(dt)
    if s == "daymarket":
        return "overnight"
    if s == "closed":
        return "closed"
    return "regular"


def _excd_order(symbol: str) -> list[str]:
    """현재 세션에 맞춰 시도할 거래소 코드 순서."""
    kind = session_kind()
    if kind == "overnight":
        primary, secondary = _DAYTIME, _REGULAR
    else:  # regular / closed → 정규 우선
        primary, secondary = _REGULAR, _DAYTIME

    hint = _EXCD_HINT.get(symbol)
    order: list[str] = []
    # 직전 성공 코드가 현재 우선 그룹에 있으면 맨 앞으로
    if hint in primary:
        order.append(hint)
    order += [c for c in primary if c not in order]
    order += [c for c in secondary if c not in order]
    return order


# ---------- 현재가 조회 ----------

def _quote_one(symbol: str, excd: str, token: str,
               timeout: float = 8.0) -> dict | None:
    """단일 거래소 코드로 현재가 조회. 유효가(last>0)면 dict, 아니면 None."""
    headers = {
        "content-type": "application/json; charset=utf-8",
        "authorization": f"Bearer {token}",
        "appkey": _key(),
        "appsecret": _secret(),
        "tr_id": "HHDFS00000300",
        "custtype": "P",
    }
    params = {"AUTH": "", "EXCD": excd, "SYMB": symbol.upper()}
    try:
        r = requests.get(
            f"{_BASE}/uapi/overseas-price/v1/quotations/price",
            headers=headers, params=params, timeout=timeout,
        )
        r.raise_for_status()
        j = r.json()
        if j.get("rt_cd") != "0":
            return None
        out = j.get("output") or {}
        last = _f(out.get("last"))
        if not last or last <= 0:
            return None
        return {
            "price": last,
            "prev_close": _f(out.get("base")),
            "change_pct": _f(out.get("rate")),
            "volume": _f(out.get("tvol")),
            "excd": excd,
        }
    except Exception as e:  # noqa: BLE001
        print(f"[KIS] {symbol}/{excd} 시세 실패: {e}")
        return None


def quote(symbol: str) -> dict | None:
    """세션 인지 현재가. {price, prev_close, change_pct, volume, excd} 또는 None."""
    token = _get_token()
    if not token:
        return None
    for excd in _excd_order(symbol):
        q = _quote_one(symbol, excd, token)
        if q:
            _EXCD_HINT[symbol] = excd
            return q
    return None


def realtime(symbol: str) -> tuple[float | None, float | None, float | None]:
    """price_monitor 호환 튜플: (현재가, 거래량, 평균거래량).

    KIS 현재가는 20일 평균거래량을 주지 않으므로 avg_volume은 None
    (호출부에서 거래량비는 무료 소스 폴백으로 보완).
    """
    q = quote(symbol)
    if not q:
        return None, None, None
    return q["price"], q.get("volume"), None


def _f(v) -> float | None:
    """KIS 문자열 숫자('194.77','1,234')를 float로. 실패 시 None."""
    if v is None:
        return None
    try:
        return float(str(v).replace(",", "").strip())
    except (ValueError, TypeError):
        return None
