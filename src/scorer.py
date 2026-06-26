"""종합 점수 계산 (가중합 + 외부보정 + 별점 등급).

기본 가중치
  Macro 0.20 + Sector 0.15 + Fundamental 0.25 + Growth 0.10
  + Technical 0.15 + Sentiment 0.10

ETF 영향력 4축(sector.market_links) 반영
  · industry  → Sector 컴포넌트(0.15) + ±5 가산  (대표 모멘텀)
  · technology+market → 레짐 곱하기 보정 ×0.94~1.05 (시장 우호/비우호)
  · country   → KR 종목 한정 ±2 (EWY 강약 = 코리아 디스카운트)
외부 보정
  거시 ×0.75~1.0 · 레짐 ×0.94~1.05 · 섹터 ±5 · 국가 ±2 · 리스크/재무 감점
"""
from __future__ import annotations

from dataclasses import dataclass, field

from utils import grade_for

WEIGHTS = {
    "macro": 0.20, "sector": 0.15, "fundamental": 0.25,
    "growth": 0.10, "technical": 0.15, "sentiment": 0.10,
}
# ETF(tradable_etf) 전용 가중치 — 펀더멘털/성장 데이터가 없으므로 0으로 두고
# 그 비중을 섹터(상대강도)·기술적·거시로 재분배(탑다운 + 테이프 중심).
WEIGHTS_ETF = {
    "macro": 0.25, "sector": 0.35, "fundamental": 0.0,
    "growth": 0.0, "technical": 0.30, "sentiment": 0.10,
}


def weights_for(asset_class: str | None) -> dict:
    return WEIGHTS_ETF if asset_class == "tradable_etf" else WEIGHTS


@dataclass
class StockResult:
    ticker: str
    name: str = ""
    asset_class: str = "equity"   # equity | tradable_etf
    price: float = float("nan")
    currency: str = "USD"
    bench_label: str = "SPY"
    sector_name: str | None = None
    # 컴포넌트 결과(dict)
    macro: dict = field(default_factory=dict)
    sector: dict | None = None
    technical: dict = field(default_factory=dict)
    fundamental: dict = field(default_factory=dict)
    growth: dict = field(default_factory=dict)
    sentiment: dict = field(default_factory=dict)
    risk: dict = field(default_factory=dict)
    news: list = field(default_factory=list)
    llm: dict | None = None
    kr_extra: dict = field(default_factory=dict)   # KR 부가데이터(수급·공매도·리포트)
    market_context: dict = field(default_factory=dict)  # 거시 레짐 + 섹터 평가 묶음
    # 점수
    component_scores: dict = field(default_factory=dict)
    base_score: float = 0.0
    final_score: float = 0.0
    mech_score: float | None = None   # LLM 보정 전 기계점수(보정 근거 표시용)
    grade: dict = field(default_factory=dict)   # {stars, en, ko}
    levels: dict = field(default_factory=dict)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def recommendation(self) -> str:
        return self.grade.get("en", "Avoid")


# 레짐/국가 보정 임계(±%) 와 강도
_REGIME_TH = 1.5
_COUNTRY_TH = 1.5


def _axis_briefs(sector: dict | None, axis: str) -> list[dict]:
    return ((sector or {}).get("market_links") or {}).get(axis) or []


def _axis_avg_chg5d(sector: dict | None, axis: str) -> float | None:
    """축(axis) ETF들의 5일 수익률 평균. 값 없으면 None."""
    vals = [b["chg_5d"] for b in _axis_briefs(sector, axis)
            if b.get("chg_5d") is not None]
    return sum(vals) / len(vals) if vals else None


def _axis_rank_pct(sector: dict | None, axis: str) -> float | None:
    """축 ETF들의 5일 순위 백분위 평균(1=가장 강함). 값 없으면 None."""
    pcts = []
    for b in _axis_briefs(sector, axis):
        r, t = b.get("rank_5d"), b.get("total")
        if r and t:
            pcts.append((t - r + 1) / t)
    return sum(pcts) / len(pcts) if pcts else None


def _sector_score(sector: dict | None) -> float:
    """Sector 컴포넌트(0~100) = 업종(industry) 축 ETF 5일순위 백분위."""
    pct = _axis_rank_pct(sector, "industry")
    if pct is not None:
        return round(100.0 * pct, 1)
    # 폴백: 대표 업종 ETF 단일 순위(market_links 없을 때)
    rank, total = (sector or {}).get("rank_5d"), (sector or {}).get("total")
    if rank and total:
        return round(100.0 * (total - rank + 1) / total, 1)
    return 50.0


def _regime_mult(sector: dict | None) -> float:
    """technology+market 축 5일 흐름 기반 레짐 곱하기 보정(×0.94~1.05).

    시장(SPY)·기술(QQQ/XLK)이 강세면 우호적 레짐 → 소폭 가점, 약세면 감점.
    거시(macro_mult)가 펀더멘털 배경이라면, 이쪽은 단기 시세(테이프) 신호.
    """
    mult = 1.0
    mkt = _axis_avg_chg5d(sector, "market")
    if mkt is not None:
        mult *= 1.03 if mkt >= _REGIME_TH else 0.97 if mkt <= -_REGIME_TH else 1.0
    tech = _axis_avg_chg5d(sector, "technology")
    if tech is not None:
        mult *= 1.02 if tech >= _REGIME_TH else 0.98 if tech <= -_REGIME_TH else 1.0
    return round(mult, 4)


def _country_adj(sector: dict | None) -> float:
    """KR 종목 한정: 국가(EWY) 5일 강약 ±2점."""
    if (sector or {}).get("country_code") != "KR":
        return 0.0
    ewy = _axis_avg_chg5d(sector, "country")
    if ewy is None:
        return 0.0
    return 2.0 if ewy >= _COUNTRY_TH else -2.0 if ewy <= -_COUNTRY_TH else 0.0


def compute(result: StockResult) -> StockResult:
    """StockResult의 컴포넌트들로 최종 점수·등급·레벨을 계산."""
    macro = result.macro or {}
    macro_score = float(macro.get("score", 0) or 0)
    macro_mult = float(macro.get("multiplier", 1.0) or 1.0)

    sector_score = _sector_score(result.sector)
    fund_score = float(result.fundamental.get("score", 0) or 0)
    tech_score = float(result.technical.get("score", 0) or 0)

    # 성장/감성: LLM 값이 있으면 우선
    growth_score = float(result.growth.get("score", 50) or 50)
    sent_score = float(result.sentiment.get("score", 50) or 50)
    if result.llm:
        if result.llm.get("growth_score") is not None:
            growth_score = float(result.llm["growth_score"])

    comp = {
        "macro": macro_score, "sector": sector_score,
        "fundamental": fund_score, "growth": growth_score,
        "technical": tech_score, "sentiment": sent_score,
    }
    result.component_scores = {k: round(v, 1) for k, v in comp.items()}

    weights = weights_for(result.asset_class)
    base = sum(comp[k] * w for k, w in weights.items())
    result.base_score = round(base, 1)

    # 외부 보정 (ETF 4축 + 리스크)
    sector_adj = float((result.sector or {}).get("adj", 0) or 0)
    regime_mult = _regime_mult(result.sector)
    country_adj = _country_adj(result.sector)
    risk_ded = float((result.risk or {}).get("deduction", 0) or 0)
    fin_ded = float((result.fundamental.get("risk", {}) or {}).get("deduction", 0) or 0)

    # 산출 근거를 sector dict에 남겨 리포트/LLM에서 참조
    if result.sector is not None:
        result.sector["regime_mult"] = regime_mult
        result.sector["country_adj"] = country_adj

    final = (base * macro_mult * regime_mult
             + sector_adj + country_adj + risk_ded + fin_ded)
    final = max(0.0, min(100.0, final))
    result.final_score = round(final, 1)
    result.grade = grade_for(final)

    # 진입/목표/손절 (Watch 이상에서 제시)
    if result.grade["en"] in ("Strong Buy", "Buy", "Watch"):
        result.levels = _levels(result.price,
                                result.technical.get("indicators", {}).get("atr14"))
    return result


# ATR 배수: 진입가는 현재가에서 0.5·ATR 눌림(눌림목 매수), 손절 1·ATR, 목표 2·ATR
# → 진입가 기준 손익비 1:2. 눌림폭은 현재가의 4%로 상한(ATR이 과도할 때 보호).
ENTRY_PULLBACK_ATR = 0.5
STOP_ATR = 1.0
TARGET_ATR = 2.0
MAX_PULLBACK_PCT = 0.04


def _levels(price: float, atr14) -> dict:
    """현재가·ATR로 진입/목표/손절 산출.

    진입가 = 현재가 − 0.5·ATR (디스카운트 눌림목 매수).
    손절가 = 진입가 − 1·ATR, 목표가 = 진입가 + 2·ATR (손익비 1:2).
    ATR이 없으면 현재가를 진입가로 사용(보수적 폴백).
    """
    try:
        atr14 = float(atr14)
    except (TypeError, ValueError):
        atr14 = float("nan")
    if not price or price != price or atr14 != atr14 or not atr14:
        return {"entry": price, "target": None, "stop": None, "rr": None}

    pullback = min(ENTRY_PULLBACK_ATR * atr14, MAX_PULLBACK_PCT * price)
    entry = price - pullback
    stop = entry - STOP_ATR * atr14
    target = entry + TARGET_ATR * atr14
    risk = entry - stop
    return {
        "ref_price": price,                 # 산출 기준이 된 현재가
        "entry": entry, "target": target, "stop": stop,
        "target_pct": (target / entry - 1) * 100,
        "stop_pct": (stop / entry - 1) * 100,
        "rr": (target - entry) / risk if risk else None,
    }


def to_score_cache(result: StockResult) -> dict:
    """변화 감지(alert)용 직전 점수 스냅샷."""
    ind = result.technical.get("indicators", {})
    return {
        "ticker": result.ticker,
        "final_score": result.final_score,
        "recommendation": result.recommendation,
        "price": result.price,
        "rsi_d": ind.get("rsi_d"),
        "macd_state": ind.get("macd_state"),
        "patterns": result.technical.get("patterns", []),
        "vol_ratio": ind.get("vol_ratio"),
    }
