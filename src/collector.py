"""yfinance 기반 데이터 수집 (탑다운 3단계 입력).

- 일봉 200일, 주봉 52주 OHLCV
- 펀더멘탈: 52주 고저, 시총, PER, PBR, ROE, EPS
- 밸류에이션: PSR, EV/EBITDA, 포워드 PER, PEG
- 실적: 최근 4분기 EPS 서프라이즈, 매출 성장, 영업이익률 추세, 어닝 일정
- 애널리스트: 목표주가, 업사이드, 추천 분포
- 재무 raw: 부채/현금/현금흐름/마진 (financial_health 모듈에서 사용)
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import pandas as pd
import yfinance as yf

import naver
import toss


def _kr_indicators(ticker: str) -> dict:
    """KR 종목: 네이버 핵심지표 → 펀더 머지용 dict(per/pbr/eps/52주). US는 {}."""
    if not ticker.upper().endswith((".KS", ".KQ")):
        return {}
    try:
        ind = naver.key_indicators(ticker) or {}
    except Exception:  # noqa: BLE001
        return {}
    return {
        "per": ind.get("per"), "pbr": ind.get("pbr"), "eps": ind.get("eps"),
        "week52_high": ind.get("week52_high"), "week52_low": ind.get("week52_low"),
        "currency": "KRW",
    }

# yfinance 1.4.x는 curl_cffi가 설치돼 있으면 내부적으로 브라우저를
# 임퍼소네이트해 Yahoo rate limit(Too Many Requests)을 완화한다.


def _ticker(symbol: str) -> "yf.Ticker":
    return yf.Ticker(symbol)


@dataclass
class StockData:
    ticker: str
    daily: pd.DataFrame = field(default_factory=pd.DataFrame)   # 일봉 ~200일
    weekly: pd.DataFrame = field(default_factory=pd.DataFrame)  # 주봉 ~52주
    info: dict = field(default_factory=dict)                    # 가공 펀더멘탈
    raw: dict = field(default_factory=dict)                     # yfinance 원시 info
    earnings: dict = field(default_factory=dict)               # 실적/어닝 일정
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and not self.daily.empty


def _clean_ohlcv(df: pd.DataFrame) -> pd.DataFrame:
    """yfinance 결과를 표준 컬럼(Open/High/Low/Close/Volume)으로 정리."""
    if df is None or df.empty:
        return pd.DataFrame()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    keep = [c for c in ["Open", "High", "Low", "Close", "Volume"] if c in df.columns]
    df = df[keep].copy()
    df = df.dropna(subset=["Close"])
    return df


def fetch_history(ticker: str, period: str, interval: str,
                  tk: "yf.Ticker | None" = None) -> pd.DataFrame:
    """단일 티커 OHLCV 다운로드."""
    tk = tk or _ticker(ticker)
    df = tk.history(period=period, interval=interval, auto_adjust=True)
    return _clean_ohlcv(df)


def _g(info: dict, *keys):
    for k in keys:
        v = info.get(k)
        if v is not None:
            return v
    return None


def parse_fundamentals(info: dict, ticker: str = "") -> dict:
    """원시 info dict에서 펀더멘탈/밸류에이션/재무 raw 추출 (네트워크 없음)."""
    roe = _g(info, "returnOnEquity")
    if roe is not None:
        roe = roe * 100  # 0.32 -> 32(%)
    roa = _g(info, "returnOnAssets")
    if roa is not None:
        roa = roa * 100
    eps_growth = _g(info, "earningsGrowth", "earningsQuarterlyGrowth")
    if eps_growth is not None:
        eps_growth = eps_growth * 100
    rev_growth = _g(info, "revenueGrowth")
    if rev_growth is not None:
        rev_growth = rev_growth * 100
    op_margin = _g(info, "operatingMargins")
    if op_margin is not None:
        op_margin = op_margin * 100
    profit_margin = _g(info, "profitMargins")
    if profit_margin is not None:
        profit_margin = profit_margin * 100

    target_mean = _g(info, "targetMeanPrice")
    price = _g(info, "currentPrice", "regularMarketPrice")
    upside = None
    if target_mean and price:
        upside = (target_mean / price - 1) * 100

    return {
        "name": _g(info, "shortName", "longName") or ticker,
        "sector": _g(info, "sector"),
        "industry": _g(info, "industry"),
        "market_cap": _g(info, "marketCap"),
        # 밸류에이션
        "per": _g(info, "trailingPE", "forwardPE"),
        "forward_pe": _g(info, "forwardPE"),
        "pbr": _g(info, "priceToBook"),
        "psr": _g(info, "priceToSalesTrailing12Months"),
        "ev_ebitda": _g(info, "enterpriseToEbitda"),
        "peg": _g(info, "trailingPegRatio", "pegRatio"),
        # 수익성/성장
        "roe": roe,                     # %
        "roa": roa,                     # %
        "eps": _g(info, "trailingEps"),
        "eps_growth": eps_growth,       # %
        "rev_growth": rev_growth,       # %
        "op_margin": op_margin,         # %
        "profit_margin": profit_margin,  # %
        # 52주
        "week52_high": _g(info, "fiftyTwoWeekHigh"),
        "week52_low": _g(info, "fiftyTwoWeekLow"),
        # 애널리스트
        "target_mean": target_mean,
        "target_high": _g(info, "targetHighPrice"),
        "target_low": _g(info, "targetLowPrice"),
        "upside": upside,               # %
        "num_analysts": _g(info, "numberOfAnalystOpinions"),
        "recommendation": _g(info, "recommendationKey"),
        "recommendation_mean": _g(info, "recommendationMean"),
    }


def fetch_fundamentals(ticker: str, tk: "yf.Ticker | None" = None) -> tuple[dict, dict]:
    """(가공 펀더멘탈, 원시 info) 반환."""
    try:
        info = (tk or _ticker(ticker)).info or {}
    except Exception:
        info = {}
    return parse_fundamentals(info, ticker), info


def _op_margin_trend(tk: "yf.Ticker") -> str | None:
    """분기 영업이익률 추세 (개선/악화). 데이터 없으면 None."""
    try:
        q = tk.quarterly_financials
        if q is None or q.empty:
            return None
        rows = {str(i).lower(): i for i in q.index}
        oi_key = next((rows[k] for k in rows if "operating income" in k), None)
        rev_key = next((rows[k] for k in rows
                        if "total revenue" in k or k == "total revenue"), None)
        if oi_key is None or rev_key is None:
            return None
        oi = q.loc[oi_key].dropna()
        rev = q.loc[rev_key].dropna()
        # 컬럼은 최신 분기가 앞쪽
        margins = []
        for col in q.columns[:4]:
            try:
                r = float(rev.get(col))
                o = float(oi.get(col))
                if r:
                    margins.append(o / r)
            except (TypeError, ValueError):
                continue
        if len(margins) < 2:
            return None
        # margins[0]=최신, margins[-1]=과거
        return "개선" if margins[0] > margins[-1] else "악화"
    except Exception:
        return None


def fetch_income_items(tk: "yf.Ticker") -> dict:
    """연간 손익계산서에서 영업이익/이자비용/이자보상배율 추출 (없으면 None)."""
    out = {"operating_income": None, "interest_expense": None,
           "interest_coverage": None}
    try:
        fin = tk.income_stmt
        if fin is None or fin.empty:
            return out
        idx = {str(i).lower(): i for i in fin.index}

        def row(*names):
            for n in names:
                for k in idx:
                    if n in k:
                        return fin.loc[idx[k]]
            return None

        col = fin.columns[0]  # 최신 회계연도
        oi = row("operating income")
        ie = row("interest expense")
        oi_v = float(oi.get(col)) if oi is not None and oi.get(col) == oi.get(col) else None
        ie_v = (abs(float(ie.get(col)))
                if ie is not None and ie.get(col) == ie.get(col) else None)
        out["operating_income"] = oi_v
        out["interest_expense"] = ie_v
        if oi_v is not None and ie_v:
            out["interest_coverage"] = oi_v / ie_v
    except Exception:
        pass
    return out


def fetch_earnings(ticker: str, tk: "yf.Ticker", fund: dict) -> dict:
    """최근 4분기 EPS 서프라이즈 + 다음 어닝 일정."""
    out: dict = {
        "surprises": [], "last_surprise": None, "avg_surprise": None,
        "next_date": None, "days_to_earnings": None, "earnings_risk": False,
        "rev_growth": fund.get("rev_growth"),
        "op_margin_trend": None,
    }
    now = datetime.now(timezone.utc)

    # --- EPS 서프라이즈 (earnings_dates) ---
    try:
        ed = tk.get_earnings_dates(limit=12)
    except Exception:
        ed = None
    next_dt = None
    if ed is not None and not ed.empty:
        cols = {str(c).lower(): c for c in ed.columns}
        est_k = next((cols[c] for c in cols if "estimate" in c), None)
        rep_k = next((cols[c] for c in cols if "reported" in c), None)
        sur_k = next((cols[c] for c in cols if "surprise" in c), None)
        surprises = []
        for idx, row in ed.iterrows():
            ts = idx.to_pydatetime() if hasattr(idx, "to_pydatetime") else idx
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            reported = row.get(rep_k) if rep_k else None
            estimate = row.get(est_k) if est_k else None
            has_report = reported is not None and reported == reported  # not NaN
            if has_report:
                sp = row.get(sur_k) if sur_k else None
                if sp is not None and sp == sp:
                    surprises.append(float(sp))
                elif estimate not in (None, 0) and estimate == estimate:
                    surprises.append((float(reported) - float(estimate))
                                     / abs(float(estimate)) * 100)
            elif ts > now and next_dt is None:
                next_dt = ts
        # 최신 보고분기가 앞쪽 → 최근 4개
        surprises = surprises[:4]
        out["surprises"] = [round(s, 2) for s in surprises]
        if surprises:
            out["last_surprise"] = round(surprises[0], 2)
            out["avg_surprise"] = round(sum(surprises) / len(surprises), 2)

    # --- 다음 어닝일 (calendar 폴백) ---
    if next_dt is None:
        try:
            cal = tk.calendar
            ed_val = None
            if isinstance(cal, dict):
                ed_val = cal.get("Earnings Date")
            elif cal is not None and hasattr(cal, "loc") and "Earnings Date" in getattr(cal, "index", []):
                ed_val = cal.loc["Earnings Date"]
            if isinstance(ed_val, (list, tuple)) and ed_val:
                ed_val = ed_val[0]
            if ed_val is not None:
                dt = pd.to_datetime(ed_val).to_pydatetime()
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                next_dt = dt
        except Exception:
            pass

    if next_dt is not None:
        out["next_date"] = next_dt.date().isoformat()
        days = (next_dt.date() - now.date()).days
        out["days_to_earnings"] = days
        out["earnings_risk"] = abs(days) <= 3

    out["op_margin_trend"] = _op_margin_trend(tk)
    return out


def _merge_fundamentals(kfund: dict, yf_fund: dict, ticker: str = "") -> dict:
    """yfinance 펀더멘털에 KIS 값(있으면)을 덮어쓴다(KIS 우선).

    KIS 제공 → 덮어씀: per, pbr, eps, market_cap, week52_high/low
    KIS 미제공 → yfinance 유지: forward_pe, psr, peg, roe, roa, 성장률,
                마진, 애널리스트 목표가/투자의견, 어닝 등.
    """
    fund = dict(yf_fund or {})
    for k in ("per", "pbr", "eps", "market_cap", "week52_high", "week52_low"):
        v = (kfund or {}).get(k)
        if v is not None:
            fund[k] = v
    if not fund.get("name"):
        fund["name"] = ticker
    if not fund.get("sector") and (kfund or {}).get("sector"):
        fund["sector"] = kfund["sector"]
    return fund


def _yf_history_retry(ticker: str, period: str, interval: str,
                      retries: int, base_pause: float) -> pd.DataFrame:
    """yfinance OHLCV 폴백(KIS 실패 시). rate limit 지수 백오프."""
    for attempt in range(retries + 1):
        try:
            df = fetch_history(ticker, period=period, interval=interval)
            if not df.empty:
                return df
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            if attempt < retries:
                wait = base_pause * (2 ** attempt)
                if "Too Many" in msg or "Rate" in msg:
                    wait = max(wait, 10.0)
                time.sleep(wait)
    return pd.DataFrame()


def collect(ticker: str, retries: int = 3, base_pause: float = 2.0) -> StockData:
    """한 종목 수집 (KIS 우선).

    · 일/주봉 OHLCV: KIS → 실패 시 yfinance
    · 펀더멘털: KIS(PER/EPS/PBR/시총/52주) + yfinance(ROE/성장률/애널리스트/어닝)
    KIS가 OHLCV를 주면 yfinance가 rate limit이어도 종목이 분석에서 누락되지 않는다.
    """
    # ① OHLCV — 토스 우선 → 네이버 → yfinance 폴백
    daily = toss.ohlcv(ticker, "D", 220)
    if daily.empty:
        daily = naver.ohlcv(ticker, "D", 220)
    if daily.empty:
        daily = _yf_history_retry(ticker, "300d", "1d", retries, base_pause)
    weekly = toss.ohlcv(ticker, "W", 60)
    if weekly.empty:
        weekly = naver.ohlcv(ticker, "W", 60)
    if weekly.empty:
        weekly = _yf_history_retry(ticker, "1y", "1wk", retries, base_pause)
    daily, weekly = daily.tail(200), weekly.tail(52)
    if daily.empty:
        return StockData(ticker=ticker,
                         error="일봉 데이터 없음 (토스·네이버·yfinance 모두 실패)")

    # ② 펀더멘털 — yfinance 기본 + (KR) 네이버 핵심지표(PER/PBR/EPS) 보완
    kfund = _kr_indicators(ticker)        # KR: 네이버 지표, US: {}
    tk = _ticker(ticker)
    try:
        yf_fund, raw = fetch_fundamentals(ticker, tk)
    except Exception:  # noqa: BLE001
        yf_fund, raw = {}, {}
    raw = dict(raw or {})
    # 통화는 시장 기준으로 확정 (모든 소스 실패해도 KR→KRW 보장)
    if not raw.get("currency"):
        raw["currency"] = ("KRW" if ticker.upper().endswith((".KS", ".KQ"))
                           else "USD")
    fund = _merge_fundamentals(kfund, yf_fund, ticker)
    # 목표주가 컨센서스: 네이버(한국어 소스) 우선, 실패 시 yfinance 값 유지
    try:
        nv = naver.consensus(ticker)
    except Exception:  # noqa: BLE001
        nv = None
    if nv and nv.get("target_mean"):
        fund["target_mean"] = nv["target_mean"]
        if nv.get("target_high"):
            fund["target_high"] = nv["target_high"]
        if nv.get("target_low"):
            fund["target_low"] = nv["target_low"]
        if nv.get("recommendation_mean") is not None:
            fund["recommendation_mean"] = nv["recommendation_mean"]
            fund["recommendation"] = nv["recommendation"]
        fund["target_source"] = "naver"
        try:
            px = float(daily["Close"].iloc[-1])
            if px > 0:
                fund["upside"] = (nv["target_mean"] / px - 1) * 100
        except Exception:  # noqa: BLE001
            pass
    try:
        fund.update(fetch_income_items(tk))
    except Exception:  # noqa: BLE001
        pass
    try:
        earnings = fetch_earnings(ticker, tk, fund)
    except Exception as ee:  # noqa: BLE001
        print(f"[수집] {ticker} 실적 데이터 일부 실패: {ee}")
        earnings = {}
    return StockData(ticker=ticker, daily=daily, weekly=weekly,
                     info=fund, raw=raw, earnings=earnings)


def collect_all(tickers: list[str], pause: float = 1.5) -> dict[str, StockData]:
    """워치리스트 전체 수집. 종목 간 간격을 둬 rate limit을 완화."""
    out: dict[str, StockData] = {}
    for i, tk in enumerate(tickers):
        out[tk] = collect(tk)
        if i < len(tickers) - 1:
            time.sleep(pause)
    return out
