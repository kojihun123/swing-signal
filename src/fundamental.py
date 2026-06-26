"""펀더멘탈 분석 (재무 + 밸류에이션 + 실적 + 0~100 점수).

배점: 수익성 30 + 성장성 25 + 재무건전성 25 + 밸류에이션 20
재무위험(2개 이상) 시 별도 -15점 deduction (scorer가 최종 적용).

financial_health 모듈을 재활용한다. 결과는 cache/fundamentals/TICKER.json 저장.
"""
from __future__ import annotations

import financial_health
from cache import save_cache

# 섹터별 평균 PER 기준값
SECTOR_PER_BASE = {
    "기술": 28, "반도체": 25, "헬스케어": 22, "에너지": 12, "금융": 14,
    "바이오": 35, "산업재": 20, "소프트웨어": 35, "양자/차세대컴퓨팅": 40,
    "로봇/AI": 30, "우주/방산": 22, "통신": 22, "임의소비재": 26,
    "필수소비재": 22, "유틸리티": 18, "부동산": 30, "소재": 16, "건설": 16,
}
DEFAULT_PER_BASE = 22.0


def _n(v):
    try:
        if v is None:
            return None
        f = float(v)
        return f if f == f else None
    except (TypeError, ValueError):
        return None


def analyze_fundamental(data, sector_name: str | None = None,
                        save: bool = True) -> dict:
    info = data.info or {}
    raw = data.raw or {}
    earn = data.earnings or {}

    fh = financial_health.assess(info, raw)

    roe = _n(info.get("roe"))
    roa = _n(info.get("roa"))
    op_margin = _n(info.get("op_margin"))
    profit_margin = _n(info.get("profit_margin"))
    rev_growth = _n(info.get("rev_growth"))
    eps_growth = _n(info.get("eps_growth"))
    last_sp = _n(earn.get("last_surprise"))

    per = _n(info.get("per"))
    forward_pe = _n(info.get("forward_pe"))
    pbr = _n(info.get("pbr"))
    psr = _n(info.get("psr"))
    peg = _n(info.get("peg"))
    ev_ebitda = _n(info.get("ev_ebitda"))
    base = SECTOR_PER_BASE.get(sector_name or "", DEFAULT_PER_BASE)

    # ---- 수익성 (30) ----
    prof = 0.0
    prof += (10 if (roe or -1) >= 20 else 8 if (roe or -1) >= 15
             else 5 if (roe or -1) >= 5 else 2) if roe is not None else 5
    prof += (8 if (op_margin or -1) >= 20 else 6 if (op_margin or -1) >= 10
             else 3 if (op_margin or -1) >= 0 else 1) if op_margin is not None else 4
    prof += (7 if (profit_margin or -1) >= 15 else 5 if (profit_margin or -1) >= 5
             else 2) if profit_margin is not None else 3.5
    prof += (5 if (roa or -1) >= 8 else 3 if (roa or -1) >= 3
             else 1) if roa is not None else 2.5
    prof = min(prof, 30)

    # ---- 성장성 (25) ----
    growth = 0.0
    growth += (10 if (rev_growth or -999) >= 20 else 8 if (rev_growth or -999) >= 10
               else 4 if (rev_growth or -999) >= 0 else 1) if rev_growth is not None else 5
    growth += (9 if (eps_growth or -999) >= 20 else 6 if (eps_growth or -999) >= 0
               else 1) if eps_growth is not None else 4
    growth += (6 if (last_sp or -999) >= 10 else 4 if (last_sp or -999) >= 0
               else 2 if (last_sp or -999) > -10 else 0) if last_sp is not None else 3
    growth = min(growth, 25)

    # ---- 재무건전성 (25) : financial_health의 부채+현금흐름 부분을 25점으로 ----
    dc = fh.parts.get("debt", 0) + fh.parts.get("cashflow", 0)  # max 75
    health = round(dc / 75 * 25, 1)

    # ---- 밸류에이션 (20) ----
    val = 0.0
    if per is not None and per > 0:
        val += (7 if per <= base * 0.8 else 5 if per <= base
                else 3 if per <= base * 1.2 else 1)
    else:
        val += 3
    val += (5 if (peg or 99) <= 1 else 3.5 if (peg or 99) <= 1.5
            else 2 if (peg or 99) <= 2.5 else 0.5) if peg is not None and peg > 0 else 2.5
    val += (3 if (psr or 99) <= 2 else 2 if (psr or 99) <= 5
            else 1 if (psr or 99) <= 10 else 0.5) if psr is not None and psr > 0 else 1.5
    if forward_pe is not None and per is not None and forward_pe > 0:
        val += 2 if forward_pe < per else 1
    else:
        val += 1
    val += (3 if (ev_ebitda or 99) <= 12 else 1.5 if (ev_ebitda or 99) <= 20
            else 0.5) if ev_ebitda is not None and ev_ebitda > 0 else 1.5
    val = min(val, 20)

    score = round(prof + growth + health + val, 1)

    result = {
        "score": min(score, 100.0),
        "parts": {"profitability": round(prof, 1), "growth": round(growth, 1),
                  "health": health, "valuation": round(val, 1)},
        "metrics": {
            "per": per, "forward_pe": forward_pe, "pbr": pbr, "psr": psr,
            "peg": peg, "ev_ebitda": ev_ebitda, "per_base": base,
            "roe": roe, "roa": roa, "op_margin": op_margin,
            "profit_margin": profit_margin, "rev_growth": rev_growth,
            "eps_growth": eps_growth, "current_ratio": _n(raw.get("currentRatio")),
            "fcf": fh.metrics.get("fcf"),
            "debt_to_equity": fh.metrics.get("debt_to_equity"),
            "interest_coverage": fh.metrics.get("interest_coverage"),
            "last_surprise": last_sp, "avg_surprise": _n(earn.get("avg_surprise")),
            "next_earnings": earn.get("next_date"),
            "days_to_earnings": earn.get("days_to_earnings"),
            "earnings_risk": earn.get("earnings_risk", False),
            "op_margin_trend": earn.get("op_margin_trend"),
            "target_mean": _n(info.get("target_mean")),
            "upside": _n(info.get("upside")),
            "num_analysts": info.get("num_analysts"),
            "recommendation": info.get("recommendation"),
        },
        "financial_health_score": fh.score,
        "risk": {
            "warnings": fh.warnings,
            "deduction": -15 if fh.risk_flag else 0,
            "flag": fh.risk_flag,
        },
    }
    if save:
        save_cache(f"fundamentals/{data.ticker}.json", result)
    return result
