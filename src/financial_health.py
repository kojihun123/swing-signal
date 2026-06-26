"""재무건전성 분석 (탑다운 6단계).

collector가 수집한 펀더멘탈/원시 info(raw)를 입력으로 받아 추가 네트워크
호출 없이 재무건전성 점수(0~100)와 재무위험 경고를 산출한다.

점수 구성
  부채 안전성 40 + 현금흐름 35 + 수익성 25

재무위험 경고: 아래 4개 중 2개 이상 해당하면 risk_flag=True (최종 -15점)
  - 부채비율 200% 초과
  - FCF 음수
  - 이자보상배율 3배 미만
  - 현금소진율(런웨이) 6개월 미만
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class FinancialHealth:
    score: float = 0.0
    parts: dict = field(default_factory=dict)     # 부채/현금흐름/수익성 부분점수
    metrics: dict = field(default_factory=dict)   # 표시용 지표값
    warnings: list[str] = field(default_factory=list)
    risk_count: int = 0
    risk_flag: bool = False                       # 2개 이상 위험 → -15점


def _num(v):
    try:
        if v is None:
            return None
        f = float(v)
        return f if f == f else None
    except (TypeError, ValueError):
        return None


def assess(fund: dict, raw: dict | None = None) -> FinancialHealth:
    raw = raw or {}
    fh = FinancialHealth()

    # --- 지표 추출 ---
    # yfinance debtToEquity는 이미 %(예: 150.5) 단위
    dte = _num(raw.get("debtToEquity"))
    total_debt = _num(raw.get("totalDebt"))
    total_cash = _num(raw.get("totalCash"))
    fcf = _num(raw.get("freeCashflow"))
    if fcf is None:
        ocf = _num(raw.get("operatingCashflow"))
        capex = _num(raw.get("capitalExpenditures"))
        if ocf is not None and capex is not None:
            fcf = ocf + capex  # capex는 보통 음수로 제공됨
    cov = _num(fund.get("interest_coverage"))
    op_margin = _num(fund.get("op_margin"))
    profit_margin = _num(fund.get("profit_margin"))
    roe = _num(fund.get("roe"))
    roa = _num(fund.get("roa"))

    # 현금소진율(런웨이): FCF 음수일 때만 의미. 월 소진액 = |FCF|/12
    runway = None
    if fcf is not None and fcf < 0 and total_cash is not None:
        monthly_burn = abs(fcf) / 12.0
        if monthly_burn:
            runway = total_cash / monthly_burn  # 개월

    # --- 부채 안전성 (40점) ---
    debt_s = 0.0
    if dte is not None:
        debt_s += 22 if dte <= 100 else 13 if dte <= 200 else 4   # max 22
    else:
        debt_s += 13
    if cov is not None:
        debt_s += 18 if cov >= 5 else 11 if cov >= 3 else 3        # max 18
    else:
        debt_s += 11
    debt_s = min(debt_s, 40)

    # --- 현금흐름 (35점) ---
    cash_s = 0.0
    if fcf is not None:
        cash_s += 20 if fcf > 0 else 4                            # max 20
    else:
        cash_s += 12
    if runway is None:
        cash_s += 15  # 소진 없음(FCF 양수 등) → 안전
    else:
        cash_s += 12 if runway >= 18 else 7 if runway >= 6 else 1  # max 15
    cash_s = min(cash_s, 35)

    # --- 수익성 (25점) ---
    prof_s = 0.0
    if op_margin is not None:
        prof_s += 9 if op_margin >= 15 else 6 if op_margin >= 5 else 2  # max 9
    else:
        prof_s += 5
    if roe is not None:
        prof_s += 10 if roe >= 15 else 6 if roe >= 5 else 2            # max 10
    else:
        prof_s += 5
    if roa is not None:
        prof_s += 6 if roa >= 8 else 4 if roa >= 3 else 1             # max 6
    else:
        prof_s += 3
    prof_s = min(prof_s, 25)

    total = round(debt_s + cash_s + prof_s, 1)
    fh.score = total
    fh.parts = {"debt": round(debt_s, 1), "cashflow": round(cash_s, 1),
                "profit": round(prof_s, 1)}
    fh.metrics = {
        "debt_to_equity": dte, "interest_coverage": cov,
        "total_cash": total_cash, "total_debt": total_debt, "fcf": fcf,
        "runway_months": runway, "op_margin": op_margin,
        "profit_margin": profit_margin, "roe": roe, "roa": roa,
    }

    # --- 재무위험 경고 ---
    warnings = []
    if dte is not None and dte > 200:
        warnings.append("부채비율 200% 초과")
    if fcf is not None and fcf < 0:
        warnings.append("FCF 음수")
    if cov is not None and cov < 3:
        warnings.append("이자보상배율 3배 미만")
    if runway is not None and runway < 6:
        warnings.append("현금소진율 6개월 미만")
    fh.warnings = warnings
    fh.risk_count = len(warnings)
    fh.risk_flag = len(warnings) >= 2
    return fh
