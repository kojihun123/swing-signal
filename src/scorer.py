"""종합 점수 계산 (가중합 + 외부보정 + 별점 등급).

기본 가중치
  Macro 0.20 + Sector 0.15 + Fundamental 0.25 + Growth 0.10
  + Technical 0.15 + Sentiment 0.10
외부 보정
  거시환경 보정계수 ×0.75~1.0 · 섹터 ±5 · 리스크 감점 · 재무위험 -15
"""
from __future__ import annotations

from dataclasses import dataclass, field

from utils import grade_for

WEIGHTS = {
    "macro": 0.20, "sector": 0.15, "fundamental": 0.25,
    "growth": 0.10, "technical": 0.15, "sentiment": 0.10,
}


@dataclass
class StockResult:
    ticker: str
    name: str = ""
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
    # 점수
    component_scores: dict = field(default_factory=dict)
    base_score: float = 0.0
    final_score: float = 0.0
    grade: dict = field(default_factory=dict)   # {stars, en, ko}
    levels: dict = field(default_factory=dict)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def recommendation(self) -> str:
        return self.grade.get("en", "Avoid")


def _sector_score(sector: dict | None) -> float:
    if not sector:
        return 50.0
    rank, total = sector.get("rank_5d"), sector.get("total")
    if rank and total:
        return round(100.0 * (total - rank + 1) / total, 1)
    return 50.0


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

    base = sum(comp[k] * w for k, w in WEIGHTS.items())
    result.base_score = round(base, 1)

    # 외부 보정
    sector_adj = float((result.sector or {}).get("adj", 0) or 0)
    risk_ded = float((result.risk or {}).get("deduction", 0) or 0)
    fin_ded = float((result.fundamental.get("risk", {}) or {}).get("deduction", 0) or 0)

    final = base * macro_mult + sector_adj + risk_ded + fin_ded
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
