"""한국 종목 부가데이터 집계 — 네이버(지표·수급·재무·리포트) + KRX(공매도).

스윙 트레이딩 판단에 쓰는 한국어 보조정보를 한 번에 모은다. US 종목은 빈
결과를 반환하므로 호출부에서 분기 없이 그대로 쓸 수 있다.

  gather(symbol)            → dict (구조화 데이터)
  as_prompt_context(symbol) → str  (LLM 프롬프트용 한국어 블록, 데이터 없으면 "")
"""
from __future__ import annotations

import naver
import krx


def _is_kr(symbol: str) -> bool:
    return symbol.upper().endswith((".KS", ".KQ"))


def gather(symbol: str, *, trend_days: int = 5, short_days: int = 5,
           reports: int = 3) -> dict:
    """KR 부가데이터 묶음. 각 항목은 개별 실패 시 None/[] 폴백."""
    if not _is_kr(symbol):
        return {"is_kr": False}
    out = {"is_kr": True}
    try:
        out["indicators"] = naver.key_indicators(symbol)
    except Exception:  # noqa: BLE001
        out["indicators"] = None
    try:
        out["investor_trend"] = naver.investor_trend(symbol, days=trend_days)
    except Exception:  # noqa: BLE001
        out["investor_trend"] = []
    try:
        out["short_selling"] = krx.short_selling(symbol, days=short_days)
    except Exception:  # noqa: BLE001
        out["short_selling"] = []
    try:
        out["financials"] = naver.financials(symbol, "annual")
    except Exception:  # noqa: BLE001
        out["financials"] = None
    try:
        out["research"] = naver.research_reports(symbol, limit=reports)
    except Exception:  # noqa: BLE001
        out["research"] = []
    return out


def _fmt_qty(v) -> str:
    """순매수 수량(주) → '+1.2만주' 식 부호 포함 축약."""
    if v is None:
        return "-"
    sign = "+" if v >= 0 else "-"
    a = abs(v)
    if a >= 1e8:
        return f"{sign}{a/1e8:.1f}억주"
    if a >= 1e4:
        return f"{sign}{a/1e4:.1f}만주"
    return f"{sign}{a:,.0f}주"


def _flow_summary(rows: list[dict]) -> str:
    """투자자별 순매수: 최근일 + 기간 누적(외국인/기관)."""
    if not rows:
        return ""
    latest = rows[0]
    f_sum = sum(r["foreigner"] or 0 for r in rows)
    o_sum = sum(r["organ"] or 0 for r in rows)
    fr = latest.get("foreign_hold_ratio")
    parts = [
        f"최근일({latest['date']}) 외국인 {_fmt_qty(latest['foreigner'])} / "
        f"기관 {_fmt_qty(latest['organ'])} / 개인 {_fmt_qty(latest['individual'])}",
        f"{len(rows)}일 누적 외국인 {_fmt_qty(f_sum)} / 기관 {_fmt_qty(o_sum)}",
    ]
    if fr is not None:
        parts.append(f"외국인보유율 {fr:.2f}%")
    return " · ".join(parts)


def _short_summary(rows: list[dict]) -> str:
    """공매도: 최근일 거래량 + 기간 평균."""
    if not rows:
        return ""
    latest = rows[0]
    vols = [r["short_volume"] for r in rows if r["short_volume"] is not None]
    avg = sum(vols) / len(vols) if vols else None
    out = f"최근일({latest['date']}) 공매도 {_fmt_qty(latest['short_volume'])}"
    if avg is not None:
        out += f" · {len(rows)}일 평균 {_fmt_qty(avg)}"
        if latest["short_volume"] and avg:
            out += f" (최근일 {latest['short_volume']/avg:.1f}배)"
    return out


def _indic_summary(ind: dict | None) -> str:
    if not ind:
        return ""
    bits = []
    if ind.get("per") is not None:
        bits.append(f"PER {ind['per']:.1f}")
    if ind.get("pbr") is not None:
        bits.append(f"PBR {ind['pbr']:.2f}")
    if ind.get("foreign_rate") is not None:
        bits.append(f"외국인 {ind['foreign_rate']:.1f}%")
    if ind.get("dividend_yield") is not None:
        bits.append(f"배당 {ind['dividend_yield']:.2f}%")
    return " / ".join(bits)


def as_prompt_context(symbol: str, data: dict | None = None) -> str:
    """LLM 프롬프트에 끼울 한국어 보조정보 블록. 데이터 없으면 빈 문자열."""
    d = data if data is not None else gather(symbol)
    if not d.get("is_kr"):
        return ""
    lines: list[str] = []
    indic = _indic_summary(d.get("indicators"))
    if indic:
        lines.append(f"- 투자지표: {indic}")
    flow = _flow_summary(d.get("investor_trend") or [])
    if flow:
        lines.append(f"- 수급동향: {flow}")
    short = _short_summary(d.get("short_selling") or [])
    if short:
        lines.append(f"- 공매도: {short}")
    reps = d.get("research") or []
    if reps:
        rl = "; ".join(f'{r["broker"]} "{r["title"]}"({r["date"]})' for r in reps[:3])
        lines.append(f"- 증권사 리포트: {rl}")
    if not lines:
        return ""
    return "한국 종목 보조정보(네이버·KRX):\n" + "\n".join(lines)
