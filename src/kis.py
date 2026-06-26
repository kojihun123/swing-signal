"""한국투자증권(KIS) 시세·펀더멘털 데이터 레이어 (KIS 우선).

KIS가 제공하는 것을 1차 소스로 쓴다.
  · 실시간 현재가: 미국 데이마켓(주간거래) 포함 전 세션 + 한국 정규장
  · 일/주봉 OHLCV: 기술적 분석 입력
  · 기본 펀더멘털: PER·PBR·EPS·BPS·시가총액·52주 고저·섹터

KIS에 없는 것(ROE·매출/이익 성장률·배당·애널리스트 목표가·어닝 일정·
상세 재무제표)은 호출부(collector)에서 yfinance로 보완한다.

엔드포인트 (실전 도메인)
  · 미국 현재가      /overseas-price/v1/quotations/price          (HHDFS00000300)
  · 미국 현재가상세  /overseas-price/v1/quotations/price-detail   (HHDFS76200200)
  · 미국 기간별시세  /overseas-price/v1/quotations/dailyprice     (HHDFS76240000)
  · 한국 현재가      /domestic-stock/v1/quotations/inquire-price  (FHKST01010100)
  · 한국 기간별시세  /domestic-stock/v1/quotations/
                       inquire-daily-itemchartprice              (FHKST03010100)

KIS_APP_KEY / KIS_APP_SECRET 가 없으면 enabled()=False → 폴백.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta

import pandas as pd
import requests

from cache import load_cache, save_cache
from utils import current_session, now_notify

_BASE = os.environ.get(
    "KIS_BASE_URL", "https://openapi.koreainvestment.com:9443"
)
_TOKEN_CACHE = "kis_token.json"

# 미국 거래소 코드: 정규 ↔ 주간거래(데이마켓). 인덱스 0=나스닥 1=뉴욕 2=아멕스
_REGULAR = ("NAS", "NYS", "AMS")
_DAYTIME = ("BAQ", "BAY", "BAA")
_EXCD_IDX = {c: i for i, c in enumerate(_REGULAR)}
_EXCD_IDX.update({c: i for i, c in enumerate(_DAYTIME)})

# 심볼 → 거래소 종류 인덱스 캐시(디스크 영속). 6개 거래소 재탐색 방지.
_EXCD_CACHE = "kis_excd.json"
_EXCD_HINT: dict | None = None


# ---------- 키/토큰 ----------

def _key() -> str | None:
    return os.environ.get("KIS_APP_KEY") or None


def _secret() -> str | None:
    return os.environ.get("KIS_APP_SECRET") or None


def enabled() -> bool:
    return bool(_key() and _secret())


def _get_token(force: bool = False) -> str | None:
    """유효 access token. 파일 캐시(24h) 우선, 만료 임박 시 재발급."""
    if not enabled():
        return None
    if not force:
        wrap = load_cache(_TOKEN_CACHE)
        if wrap and isinstance(wrap.get("data"), dict):
            tok = wrap["data"]
            try:
                exp = tok.get("expires_at", "")
                if exp and datetime.fromisoformat(exp) > now_notify():
                    return tok.get("access_token")
            except (ValueError, TypeError):
                pass
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
        expires_in = int(j.get("expires_in", 86400))
        expires_at = now_notify() + timedelta(seconds=max(60, expires_in - 300))
        save_cache(_TOKEN_CACHE,
                   {"access_token": token, "expires_at": expires_at.isoformat()})
        return token
    except Exception as e:  # noqa: BLE001
        print(f"[KIS] 토큰 발급 실패: {e}")
        return None


# ---------- 공통 GET ----------

def _get(path: str, tr_id: str, params: dict, timeout: float = 8.0,
         retries: int = 2) -> dict | None:
    """KIS GET 호출. rt_cd==0이면 전체 JSON, 아니면 None.

    KIS는 순간 부하/속도제한 시 5xx를 종종 반환 → 5xx·타임아웃은 짧게 재시도.
    """
    import time
    token = _get_token()
    if not token:
        return None
    headers = {
        "content-type": "application/json; charset=utf-8",
        "authorization": f"Bearer {token}",
        "appkey": _key(), "appsecret": _secret(),
        "tr_id": tr_id, "custtype": "P",
    }
    last_err = None
    for attempt in range(retries + 1):
        try:
            r = requests.get(f"{_BASE}{path}", headers=headers,
                             params=params, timeout=timeout)
            if r.status_code >= 500:        # 일시적 서버오류 → 재시도
                last_err = f"{r.status_code} Server Error"
                time.sleep(0.6 * (attempt + 1))
                continue
            r.raise_for_status()
            j = r.json()
            return j if str(j.get("rt_cd")) == "0" else None
        except requests.exceptions.Timeout as e:
            last_err = e
            time.sleep(0.5)
        except Exception as e:  # noqa: BLE001
            last_err = e
            break
    if last_err:
        print(f"[KIS] {path.split('/')[-1]} 실패: {last_err}")
    return None


def _f(v) -> float | None:
    """'194.77'/'1,234'/'' → float 또는 None."""
    if v is None:
        return None
    try:
        s = str(v).replace(",", "").strip()
        return float(s) if s not in ("", "-", "N/A") else None
    except (ValueError, TypeError):
        return None


def _is_kr(symbol: str) -> bool:
    return symbol.upper().endswith((".KS", ".KQ"))


def _kr_code(symbol: str) -> str:
    return symbol.split(".")[0]


# ---------- 거래소 힌트 캐시 ----------

def _hints() -> dict:
    global _EXCD_HINT
    if _EXCD_HINT is None:
        wrap = load_cache(_EXCD_CACHE)
        _EXCD_HINT = (wrap or {}).get("data") or {} if wrap else {}
    return _EXCD_HINT


def _remember_excd(symbol: str, excd: str) -> None:
    """거래소 종류 인덱스(0/1/2) 기억. 세션 무관 재사용."""
    idx = _EXCD_IDX.get(excd)
    if idx is None:
        return
    h = _hints()
    if h.get(symbol) != idx:
        h[symbol] = idx
        try:
            save_cache(_EXCD_CACHE, h)
        except Exception:  # noqa: BLE001
            pass


# ---------- 세션 / 거래소 선택 ----------

def session_kind(dt=None) -> str:
    """'overnight'(데이마켓) | 'regular' | 'closed' (utils.current_session 기반)."""
    s = current_session(dt)
    return {"daymarket": "overnight", "closed": "closed"}.get(s, "regular")


def _excd_order(symbol: str) -> list[str]:
    """현재 세션 기준 시도할 거래소 코드 순서(실시간 현재가용)."""
    if session_kind() == "overnight":
        primary, secondary = _DAYTIME, _REGULAR
    else:
        primary, secondary = _REGULAR, _DAYTIME
    idx = _hints().get(symbol)
    order: list[str] = []
    if isinstance(idx, int) and 0 <= idx < len(primary):
        order.append(primary[idx])
    order += [c for c in primary if c not in order]
    order += [c for c in secondary if c not in order]
    return order


def _regular_excd(symbol: str) -> list[str]:
    """정규 거래소만(과거 일봉 조회용 — 주간거래 코드는 1일치만 줌)."""
    idx = _hints().get(symbol)
    order: list[str] = []
    if isinstance(idx, int) and 0 <= idx < len(_REGULAR):
        order.append(_REGULAR[idx])
    order += [c for c in _REGULAR if c not in order]
    return order


# ---------- 실시간 현재가 (경량, price_monitor용) ----------

def _us_price(symbol: str) -> dict | None:
    """미국 경량 현재가(HHDFS00000300). 세션 인지 거래소 자동선택."""
    for excd in _excd_order(symbol):
        j = _get("/uapi/overseas-price/v1/quotations/price", "HHDFS00000300",
                 {"AUTH": "", "EXCD": excd, "SYMB": symbol.upper()})
        out = (j or {}).get("output") or {}
        last = _f(out.get("last"))
        if last and last > 0:
            _remember_excd(symbol, excd)
            return {"price": last, "prev_close": _f(out.get("base")),
                    "change_pct": _f(out.get("rate")),
                    "volume": _f(out.get("tvol")), "currency": "USD",
                    "excd": excd}
    return None


def _kr_quote(symbol: str) -> dict | None:
    """한국 현재가+펀더멘털(FHKST01010100). 시세 한 번에 PER/EPS/52주까지."""
    j = _get("/uapi/domestic-stock/v1/quotations/inquire-price", "FHKST01010100",
             {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": _kr_code(symbol)})
    o = (j or {}).get("output") or {}
    price = _f(o.get("stck_prpr"))
    if not price or price <= 0:
        return None
    mcap = _f(o.get("hts_avls"))          # 단위: 억원 → 원으로 환산
    return {
        "price": price,
        "prev_close": _f(o.get("stck_sdpr")),
        "change_pct": _f(o.get("prdy_ctrt")),
        "volume": _f(o.get("acml_vol")),
        "currency": "KRW",
        "per": _f(o.get("per")), "pbr": _f(o.get("pbr")),
        "eps": _f(o.get("eps")), "bps": _f(o.get("bps")),
        "market_cap": mcap * 1e8 if mcap is not None else None,
        "week52_high": _f(o.get("w52_hgpr")), "week52_low": _f(o.get("w52_lwpr")),
        "sector": o.get("bstp_kor_isnm"),
        "excd": "KRX",
    }


def quote(symbol: str) -> dict | None:
    """세션 인지 실시간 현재가 {price, prev_close, change_pct, volume, ...} 또는 None."""
    if not enabled():
        return None
    return _kr_quote(symbol) if _is_kr(symbol) else _us_price(symbol)


def realtime(symbol: str) -> tuple[float | None, float | None, float | None]:
    """price_monitor 호환 (현재가, 거래량, 평균거래량). 평균거래량은 KIS 미제공→None."""
    q = quote(symbol)
    if not q:
        return None, None, None
    return q["price"], q.get("volume"), None


# ---------- 펀더멘털 (현재가상세, full_analysis용) ----------

def _us_detail(symbol: str) -> dict | None:
    """미국 현재가상세(HHDFS76200200): 시세 + PER/PBR/EPS/BPS/시총/52주/섹터."""
    for excd in _excd_order(symbol):
        j = _get("/uapi/overseas-price/v1/quotations/price-detail", "HHDFS76200200",
                 {"AUTH": "", "EXCD": excd, "SYMB": symbol.upper()})
        out = (j or {}).get("output") or {}
        last = _f(out.get("last"))
        if last and last > 0:
            _remember_excd(symbol, excd)
            return {
                "price": last, "prev_close": _f(out.get("base")),
                "change_pct": _f(out.get("rate")), "volume": _f(out.get("tvol")),
                "open": _f(out.get("open")), "high": _f(out.get("high")),
                "low": _f(out.get("low")),
                "per": _f(out.get("perx")), "pbr": _f(out.get("pbrx")),
                "eps": _f(out.get("epsx")), "bps": _f(out.get("bpsx")),
                "market_cap": _f(out.get("tomv")),
                "week52_high": _f(out.get("h52p")), "week52_low": _f(out.get("l52p")),
                "currency": out.get("curr") or "USD",
                "sector": out.get("e_icod"),
                "excd": excd,
            }
    return None


def fundamentals(symbol: str) -> dict | None:
    """KIS 제공 시세+펀더멘털. 한국=inquire-price, 미국=price-detail.

    포함: price, prev_close, change_pct, volume, per, pbr, eps, bps,
          market_cap, week52_high/low, currency, sector.
    미포함(KIS에 없음): ROE, 성장률, 배당, 애널리스트 목표가, 어닝 일정.
    """
    if not enabled():
        return None
    return _kr_quote(symbol) if _is_kr(symbol) else _us_detail(symbol)


# ---------- 일/주봉 OHLCV ----------

_OHLCV_COLS = ["Open", "High", "Low", "Close", "Volume"]


def _empty_df() -> pd.DataFrame:
    return pd.DataFrame(columns=_OHLCV_COLS)


def _prev_day(yyyymmdd: str) -> str:
    return (datetime.strptime(yyyymmdd, "%Y%m%d") - timedelta(days=1)).strftime("%Y%m%d")


def _today_kst() -> str:
    return now_notify().strftime("%Y%m%d")


def _rows_to_df(rows: list[tuple]) -> pd.DataFrame:
    """[(date, o,h,l,c,v)] → 오름차순 OHLCV DataFrame."""
    if not rows:
        return _empty_df()
    rows = sorted(set(rows), key=lambda r: r[0])
    idx = pd.to_datetime([r[0] for r in rows], format="%Y%m%d")
    df = pd.DataFrame(
        {"Open": [r[1] for r in rows], "High": [r[2] for r in rows],
         "Low": [r[3] for r in rows], "Close": [r[4] for r in rows],
         "Volume": [r[5] for r in rows]}, index=idx)
    return df.dropna(subset=["Close"])


def _us_daily(symbol: str, gubn: str = "0", count: int = 220) -> pd.DataFrame:
    """미국 일/주/월봉(HHDFS76240000). BYMD 페이징으로 count개까지."""
    rows: list[tuple] = []
    seen: set[str] = set()
    working: str | None = None
    bymd = ""
    for _ in range(count // 100 + 2):
        got = None
        cand = [working] if working else _regular_excd(symbol)
        for excd in cand:
            j = _get("/uapi/overseas-price/v1/quotations/dailyprice", "HHDFS76240000",
                     {"AUTH": "", "EXCD": excd, "SYMB": symbol.upper(),
                      "GUBN": gubn, "BYMD": bymd, "MODP": "1", "KEYB": ""})
            o2 = (j or {}).get("output2") or []
            if o2:
                got, working = o2, excd
                _remember_excd(symbol, excd)
                break
        if not got:
            break
        fresh = [r for r in got if r.get("xymd") and r["xymd"] not in seen]
        if not fresh:
            break
        for r in fresh:
            seen.add(r["xymd"])
            rows.append((r["xymd"], _f(r.get("open")), _f(r.get("high")),
                         _f(r.get("low")), _f(r.get("clos")), _f(r.get("tvol"))))
        if len(got) < 100 or len(rows) >= count:
            break
        bymd = _prev_day(min(r["xymd"] for r in got))
    return _rows_to_df(rows)


def _kr_daily(symbol: str, pdiv: str = "D", count: int = 220) -> pd.DataFrame:
    """한국 일/주/월봉(FHKST03010100). 날짜범위 페이징으로 count개까지."""
    code = _kr_code(symbol)
    rows: list[tuple] = []
    seen: set[str] = set()
    end = _today_kst()
    for _ in range(count // 90 + 2):
        d1 = (datetime.strptime(end, "%Y%m%d") - timedelta(days=140)).strftime("%Y%m%d")
        j = _get("/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice",
                 "FHKST03010100",
                 {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code,
                  "FID_INPUT_DATE_1": d1, "FID_INPUT_DATE_2": end,
                  "FID_PERIOD_DIV_CODE": pdiv, "FID_ORG_ADJ_PRC": "0"})
        o2 = (j or {}).get("output2") or []
        fresh = [r for r in o2
                 if r.get("stck_bsop_date") and r["stck_bsop_date"] not in seen
                 and _f(r.get("stck_clpr"))]
        if not fresh:
            break
        for r in fresh:
            seen.add(r["stck_bsop_date"])
            rows.append((r["stck_bsop_date"], _f(r.get("stck_oprc")),
                         _f(r.get("stck_hgpr")), _f(r.get("stck_lwpr")),
                         _f(r.get("stck_clpr")), _f(r.get("acml_vol"))))
        if len(rows) >= count:
            break
        end = _prev_day(min(r["stck_bsop_date"] for r in fresh))
    return _rows_to_df(rows)


def daily_ohlcv(symbol: str, period: str = "D", count: int = 220) -> pd.DataFrame:
    """KIS 일/주/월봉 OHLCV DataFrame(오름차순). 실패 시 빈 DataFrame.

    period: 'D'(일) 'W'(주) 'M'(월).
    """
    if not enabled():
        return _empty_df()
    if _is_kr(symbol):
        return _kr_daily(symbol, {"D": "D", "W": "W", "M": "M"}.get(period, "D"), count)
    return _us_daily(symbol, {"D": "0", "W": "1", "M": "2"}.get(period, "0"), count)
