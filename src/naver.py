"""네이버증권 데이터 레이어 — 한국어 뉴스 + 목표주가 컨센서스.

무료·무인증 JSON API. 화면 URL과 뒤단 API 대응:
  국내  https://m.stock.naver.com/domestic/stock/005930/total
  해외  https://m.stock.naver.com/worldstock/stock/NVDA.O/total
  · US 티커→reutersCode  front-api/search/autoComplete   (디스크 캐시)
  · 뉴스(한국어)          api.stock.naver.com/news/stock/{code}
  · 컨센서스(목표주가)     integration  (US: api.stock / KR: m.stock/api, 호스트 다름)

KR 종목(.KS/.KQ)은 6자리 코드, US 종목은 reutersCode(NVDA.O·ORCL.K 등, 접미사
가 거래소마다 달라 검색으로 변환). 모든 호출 실패는 None/[]로 폴백한다.
"""
from __future__ import annotations

import html
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import requests

from cache import load_data, save_cache

KST = ZoneInfo("Asia/Seoul")
_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
                   "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile Safari/604.1"),
    "Referer": "https://m.stock.naver.com/",
}
# stock.naver.com 새 PC 프론트(domestic/detail) 호출용 — 데스크톱 UA + XHR 헤더
_PC_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"),
    "Referer": "https://stock.naver.com/",
    "X-Requested-With": "XMLHttpRequest",
}
_CODE_CACHE = "naver_codes.json"   # 티커 → reutersCode (영속)


def _is_kr(symbol: str) -> bool:
    return symbol.upper().endswith((".KS", ".KQ"))


def _kr_code(symbol: str) -> str:
    return symbol.split(".")[0]


def _get(url: str, timeout: float = 8.0, headers: dict | None = None):
    try:
        r = requests.get(url, headers=headers or _HEADERS, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception as e:  # noqa: BLE001
        print(f"[네이버] 요청 실패: {e}")
        return None


def reuters_code(symbol: str) -> str | None:
    """US 티커 → 네이버 reutersCode(NVDA.O 등). autoComplete 1회 변환 후 캐시.

    KR 종목은 6자리 코드를 그대로 반환.
    """
    if _is_kr(symbol):
        return _kr_code(symbol)
    sym = symbol.upper()
    cache = load_data(_CODE_CACHE) or {}
    if sym in cache:
        return cache[sym] or None
    j = _get("https://m.stock.naver.com/front-api/search/autoComplete"
             f"?query={sym}&target=stock")
    items = (((j or {}).get("result")) or {}).get("items") or []
    code = None
    # 1순위: 심볼 정확 매칭 + 미국. 2순위: 첫 미국 주식.
    for it in items:
        if str(it.get("code", "")).upper() == sym and it.get("nationCode") == "USA":
            code = it.get("reutersCode")
            break
    if code is None:
        for it in items:
            if it.get("nationCode") == "USA" and it.get("category") == "stock":
                code = it.get("reutersCode")
                break
    cache[sym] = code or ""
    try:
        save_cache(_CODE_CACHE, cache)
    except Exception:  # noqa: BLE001
        pass
    return code


def _num(v):
    if v is None:
        return None
    try:
        return float(str(v).replace(",", "").strip())
    except (ValueError, TypeError):
        return None


def _integration(symbol: str) -> dict | None:
    code = reuters_code(symbol)
    if not code:
        return None
    if _is_kr(symbol):
        return _get(f"https://m.stock.naver.com/api/stock/{code}/integration")
    return _get(f"https://api.stock.naver.com/stock/{code}/integration")


# 네이버 recommMean(5=최고 ~ 1=최저) → yfinance식 recommendationKey
_RECOMM_KEYS = [(4.5, "strong_buy"), (3.5, "buy"), (2.5, "hold"),
                (1.5, "underperform")]


def _recomm_key(mean: float | None) -> str | None:
    if mean is None:
        return None
    for th, key in _RECOMM_KEYS:
        if mean >= th:
            return key
    return "sell"


def consensus(symbol: str) -> dict | None:
    """목표주가 컨센서스 dict 또는 None.

    {target_mean, target_high, target_low, recommendation_mean(5=최고),
     recommendation(yfinance식 key), currency}
    """
    data = _integration(symbol)
    c = (data or {}).get("consensusInfo") if isinstance(data, dict) else None
    if not c:
        return None
    tmean = _num(c.get("priceTargetMean"))
    if tmean is None:
        return None
    rmean = _num(c.get("recommMean"))
    return {
        "target_mean": tmean,
        "target_high": _num(c.get("priceTargetHigh")),
        "target_low": _num(c.get("priceTargetLow")),
        "recommendation_mean": rmean,
        "recommendation": _recomm_key(rmean),
        "currency": (c.get("currencyType") or {}).get("code"),
    }


def _world_news_rows(symbol: str, limit: int) -> list[tuple[datetime, str, str]]:
    """US 종목 한국어 번역 뉴스(worldStock/list) → news.py 호환 형식."""
    rc = reuters_code(symbol)
    if not rc:
        return []
    j = _get(f"https://stock.naver.com/api/foreign/worldStock/list"
             f"?reutersCode={rc}&page=1&pageSize={limit}", headers=_PC_HEADERS)
    if not isinstance(j, list):
        return []
    out: list[tuple[datetime, str, str]] = []
    seen: set[str] = set()
    for it in j:
        if not isinstance(it, dict):
            continue
        aid = it.get("aid")
        oid = it.get("oid")
        title = html.unescape((it.get("tit") or "").strip())
        if not aid or aid in seen or not title:
            continue
        seen.add(aid)
        try:
            dt = datetime.strptime(str(it.get("dt") or "")[:14],
                                   "%Y%m%d%H%M%S").replace(tzinfo=KST)
        except ValueError:
            continue
        url = (f"https://n.news.naver.com/mnews/article/{oid}/{aid}"
               if str(oid or "").isdigit() else
               f"https://stock.naver.com/worldstock/stock/{rc}/worldnews")
        out.append((dt.astimezone(timezone.utc), title, url))
    return out


def news_rows(symbol: str, limit: int = 12) -> list[tuple[datetime, str, str]]:
    """네이버 종목 뉴스 → [(published_utc, title, url)] (news.py 호환 형식).

    KR: 국내 뉴스 API · US: worldStock 한국어 번역 뉴스.
    """
    if not _is_kr(symbol):
        return _world_news_rows(symbol, limit)
    code = _kr_code(symbol)
    j = _get(f"https://api.stock.naver.com/news/stock/{code}?pageSize={limit}&page=1")
    if not isinstance(j, list):
        return []
    out: list[tuple[datetime, str, str]] = []
    seen: set[str] = set()
    for grp in j:
        for it in (grp.get("items") or []):
            aid = it.get("articleId") or it.get("id")
            office = it.get("officeId")
            if not aid or aid in seen:
                continue
            seen.add(aid)
            title = html.unescape((it.get("title") or "").strip())
            try:
                dt = datetime.strptime(str(it.get("datetime") or ""),
                                       "%Y%m%d%H%M").replace(tzinfo=KST)
            except ValueError:
                continue
            url = (f"https://n.news.naver.com/mnews/article/{office}/{aid}"
                   if office and aid else (it.get("mobileNewsUrl") or ""))
            if title:
                out.append((dt.astimezone(timezone.utc), title, url))
    return out


# ── OHLCV (네이버 차트) ────────────────────────────────────────────────
# US: chart/foreign/item/{reutersCode}/day · KR: chart/domestic/item/{code}/day
# 주봉 엔드포인트는 빈값을 반환하므로 일봉을 받아 주봉으로 리샘플한다.
_OHLCV_COLS = ["Open", "High", "Low", "Close", "Volume"]


def _empty_ohlcv() -> pd.DataFrame:
    return pd.DataFrame(columns=_OHLCV_COLS)


def _chart_url(symbol: str) -> str | None:
    if _is_kr(symbol):
        return (f"https://api.stock.naver.com/chart/domestic/item/"
                f"{_kr_code(symbol)}/day")
    rc = reuters_code(symbol)
    return f"https://api.stock.naver.com/chart/foreign/item/{rc}/day" if rc else None


def _rows_to_df(rows: list[tuple]) -> pd.DataFrame:
    """[(yyyymmdd, o,h,l,c,v)] → 오름차순 OHLCV DataFrame (collector 호환)."""
    if not rows:
        return _empty_ohlcv()
    rows = sorted(set(rows), key=lambda r: r[0])
    idx = pd.to_datetime([r[0] for r in rows], format="%Y%m%d")
    return pd.DataFrame(
        {"Open": [r[1] for r in rows], "High": [r[2] for r in rows],
         "Low": [r[3] for r in rows], "Close": [r[4] for r in rows],
         "Volume": [r[5] for r in rows]}, index=idx)


def ohlcv(symbol: str, period: str = "D", count: int = 220) -> pd.DataFrame:
    """네이버 차트 일봉 OHLCV(오름차순). period='W'는 일봉을 주봉으로 리샘플.

    collector.kis.daily_ohlcv와 동일 포맷(오름차순 DatetimeIndex + OHLCV).
    실패 시 빈 DataFrame.
    """
    url = _chart_url(symbol)
    if not url:
        return _empty_ohlcv()
    days_back = max(count, 1) * (8 if period == "W" else 2) + 30
    end = datetime.now(KST).date()
    start = end - timedelta(days=days_back)
    j = _get(f"{url}?startDateTime={start:%Y%m%d}&endDateTime={end:%Y%m%d}",
             headers=_PC_HEADERS)
    if not isinstance(j, list) or not j:
        return _empty_ohlcv()
    rows: list[tuple] = []
    for it in j:
        if not isinstance(it, dict):
            continue
        try:
            rows.append((
                str(it["localDate"]), float(it["openPrice"]),
                float(it["highPrice"]), float(it["lowPrice"]),
                float(it["closePrice"]), float(it.get("accumulatedTradingVolume") or 0),
            ))
        except (KeyError, TypeError, ValueError):
            continue
    df = _rows_to_df(rows)
    if df.empty:
        return df
    if period == "W":
        df = (df.resample("W-FRI").agg(
            {"Open": "first", "High": "max", "Low": "min",
             "Close": "last", "Volume": "sum"}).dropna(subset=["Close"]))
    return df.tail(count)


# ── KR 종목 부가 데이터 (수급·재무·리포트·지표) ───────────────────────────
# 모두 국내 6자리 종목 전용. US 종목·실패는 None/[] 폴백.

def _date(yyyymmdd: str | None):
    """'20260625' → date 객체. 실패 시 None."""
    try:
        return datetime.strptime(str(yyyymmdd), "%Y%m%d").date()
    except (ValueError, TypeError):
        return None


def _num_unit(v):
    """단위 붙은 지표값('27.44배','47.37%','12,372원') → 선행 숫자 float."""
    if v is None:
        return None
    m = re.match(r"\s*-?[\d,]+(?:\.\d+)?", str(v))
    return _num(m.group(0)) if m else None


def key_indicators(symbol: str) -> dict | None:
    """integration.totalInfos → 핵심 투자지표 dict (네이버 '투자정보' 탭).

    {per, eps, pbr, bps, foreign_rate(외국인비율 %), dividend_yield(%),
     market_value(억원 문자열 정규화 X), week52_high, week52_low}
    US 종목·실패는 None.
    """
    if not _is_kr(symbol):
        return None
    data = _integration(symbol)
    infos = (data or {}).get("totalInfos") if isinstance(data, dict) else None
    if not isinstance(infos, list):
        return None
    raw = {i.get("code"): i.get("value") for i in infos if isinstance(i, dict)}
    out = {
        "per": _num_unit(raw.get("per")),
        "eps": _num_unit(raw.get("eps")),
        "pbr": _num_unit(raw.get("pbr")),
        "bps": _num_unit(raw.get("bps")),
        "foreign_rate": _num_unit(raw.get("foreignRate")),
        "dividend_yield": _num_unit(raw.get("dividendYieldRatio")),
        "market_value": raw.get("marketValue"),
        "week52_high": _num_unit(raw.get("highPriceOf52Weeks")),
        "week52_low": _num_unit(raw.get("lowPriceOf52Weeks")),
    }
    return out if any(v is not None for v in out.values()) else None


def investor_trend(symbol: str, days: int = 20) -> list[dict]:
    """일별 투자자별 매매동향 (외국인/기관/개인 순매수 + 외국인보유율).

    [{date, foreigner, organ, individual, foreign_hold_ratio, close}]  최신순.
    US 종목·실패는 [].
    """
    if not _is_kr(symbol):
        return []
    code = _kr_code(symbol)
    j = _get(f"https://stock.naver.com/api/domestic/detail/{code}/trend"
             f"?tradeType=KRX&startIdx=0&pageSize={days}", headers=_PC_HEADERS)
    if not isinstance(j, list):
        return []
    out: list[dict] = []
    for it in j:
        if not isinstance(it, dict):
            continue
        d = _date(it.get("bizdate"))
        if d is None:
            continue
        out.append({
            "date": d.isoformat(),
            "foreigner": _num(it.get("foreignerPureBuyQuant")),
            "organ": _num(it.get("organPureBuyQuant")),
            "individual": _num(it.get("individualPureBuyQuant")),
            "foreign_hold_ratio": _num(it.get("frgnHoldRatio")),
            "close": _num(it.get("closePrice")),
        })
    out.sort(key=lambda r: r["date"], reverse=True)
    return out


# financeInfo.rowList 중 노출할 핵심 항목(제목 그대로 매칭)
_FIN_ROWS = ("매출액", "영업이익", "당기순이익", "영업이익률", "순이익률",
             "ROE", "부채비율", "EPS", "PER", "PBR")


def financials(symbol: str, period: str = "annual") -> dict | None:
    """연간/분기 요약 재무제표 (네이버 '기업정보' 탭).

    period: 'annual' | 'quarter'. 반환:
    {period, columns:[{key,label,consensus}], rows:[{title, values:{key:float}}]}
    US 종목·실패는 None.
    """
    if not _is_kr(symbol):
        return None
    if period not in ("annual", "quarter"):
        period = "annual"
    code = _kr_code(symbol)
    j = _get(f"https://m.stock.naver.com/api/stock/{code}/finance/{period}")
    fi = (j or {}).get("financeInfo") if isinstance(j, dict) else None
    if not isinstance(fi, dict):
        return None
    columns = [{"key": t.get("key"), "label": t.get("title"),
                "consensus": t.get("isConsensus") == "Y"}
               for t in (fi.get("trTitleList") or []) if isinstance(t, dict)]
    rows = []
    for r in (fi.get("rowList") or []):
        if not isinstance(r, dict):
            continue
        title = r.get("title")
        if title not in _FIN_ROWS:
            continue
        vals = {k: _num((v or {}).get("value"))
                for k, v in (r.get("columns") or {}).items()}
        rows.append({"title": title, "values": vals})
    if not columns or not rows:
        return None
    return {"period": period, "columns": columns, "rows": rows}


def research_reports(symbol: str, limit: int = 5) -> list[dict]:
    """증권사 리서치 리포트 (네이버 '리서치' 탭).

    [{title, broker, date, summary, category, url}]  최신순.
    US 종목·실패는 [].
    """
    if not _is_kr(symbol):
        return []
    code = _kr_code(symbol)
    j = _get(f"https://m.stock.naver.com/api/research/stock/{code}"
             f"?pageSize={limit}&page=1")
    if not isinstance(j, list):
        return []
    out: list[dict] = []
    for it in j:
        if not isinstance(it, dict):
            continue
        title = html.unescape((it.get("title") or "").strip())
        if not title:
            continue
        rid = it.get("researchId")
        out.append({
            "title": title,
            "broker": it.get("brokerName"),
            "date": it.get("writeDate"),
            "summary": html.unescape((it.get("previewContent") or "").strip()),
            "category": it.get("category") or it.get("researchCategory"),
            "url": (f"https://stock.naver.com/domestic/stock/{code}/research/{rid}"
                    if rid else ""),
        })
    return out[:limit]
