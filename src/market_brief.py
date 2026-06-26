"""시장 브리핑 LLM — 오늘의 테마 + 거시-섹터 논리일관성 + 한줄요약 (하루 1회).

거시 4축·레짐·섹터 평가(이미 계산된 값)만 입력으로 1회 LLM 호출.
종목 분석과 별개로 리포트 상단/하단의 시장 내러티브를 생성한다.

  run(macro_dict, regime, sectors) → {themes:[..], consistency:"", summary:""}

LLM 키 없거나 실패 시 None. 모든 문자열은 2문장·120자 이내로 제한된다.
"""
from __future__ import annotations

import llm_analyzer


def _s(v, d: int = 1, suf: str = "") -> str:
    try:
        return f"{float(v):.{d}f}{suf}"
    except (TypeError, ValueError):
        return "N/A"


def _clip(text: str, limit: int = 120) -> str:
    t = " ".join(str(text or "").split())
    return t if len(t) <= limit else t[:limit - 1].rstrip() + "…"


def build_prompt(macro: dict, regime: dict, sectors: dict) -> str:
    m = macro.get("metrics", {})
    bull = [v["label"] for v in sectors.values()
            if v.get("rating") in ("Strong Bullish", "Bullish")]
    bear = [v["label"] for v in sectors.values()
            if v.get("rating") in ("Bearish", "Strong Bearish")]
    defensive = [v["label"] for k, v in sectors.items()
                 if k in ("XLU", "XLP", "XLV", "XLRE")
                 and v.get("rating") in ("Strong Bullish", "Bullish")]
    return f"""당신은 매크로 전략가입니다. 아래 시장 데이터로 '오늘의 시장'을 요약하세요.

[거시] 환경 {macro.get('label','')} {_s(macro.get('score'),0)}점 · 레짐 {regime.get('summary','')}
  기준금리 {_s(m.get('fedfunds'),2,'%')} · 10년물 {_s(m.get('y10'),2,'%')}({m.get('y10_trend','')})
  달러 {_s(m.get('dxy'),1)}({m.get('dxy_trend','')}) · 하이일드 {_s(m.get('hy_spread'),2,'%')}
  VIX {_s(m.get('vix'),1)} · F&G {_s(m.get('fg_score'),0)}({m.get('fg_label','')})
[섹터] 강세: {', '.join(bull) or '-'} / 약세: {', '.join(bear) or '-'}
  (방어섹터 동반강세: {', '.join(defensive) or '없음'})

논리 점검: 레짐과 섹터가 상충하면(예: Expansion인데 유틸리티 강세) 그 이유를 설명하세요.

아래 JSON으로만 답하세요. 초보 투자자도 이해할 쉬운 한국어. 각 문장 2문장·120자 이내. 중복 금지:
- 어려운 경제용어·영어 약어(RS, Fit, Momentum 등) 금지. 쉬운 우리말로.
- themes: 추상어("성장","수익성") 금지. 실제 시장을 묘사하는 구체 문구로.
  좋은 예: "AI 반도체 강세", "금리 안정으로 성장주 선호", "방어주 동반 강세", "투자심리 위축"
- summary: 한 문장이되 주도 업종 + 주의할 점을 함께 담아 구체적으로.
{{
  "themes": ["구체적 시장 테마 3~5개, 각 25자 이내"],
  "consistency": "거시-섹터 논리 충돌 시 한 문장 설명, 충돌 없으면 빈 문자열",
  "summary": "오늘 시장 한 문장 요약 (주도+경계 포함)",
  "strategy": ["오늘의 실행 전략 2~3개. 행동 지침으로(예: '반도체 중심 관심 유지', '추격매수 자제', '조정 시 분할매수')"]
}}"""


def run(macro: dict, regime: dict, sectors: dict) -> dict | None:
    if not sectors or not llm_analyzer.llm_enabled():
        return None
    data = llm_analyzer.generate_json(
        build_prompt(macro, regime, sectors), label="market_brief")
    if not isinstance(data, dict):
        return None
    themes = [_clip(t, 30) for t in (data.get("themes") or []) if str(t).strip()]
    strategy = [_clip(s, 40) for s in (data.get("strategy") or []) if str(s).strip()]
    result = {
        "themes": themes[:5],
        "consistency": _clip(data.get("consistency", "")),
        "summary": _clip(data.get("summary", "")),
        "strategy": strategy[:3],
    }
    return result if (result["themes"] or result["summary"]) else None
