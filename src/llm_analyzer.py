"""LLM 종합 판단 (Google Gemini).

scorer.StockResult에 모인 거시·섹터·펀더멘탈·기술·뉴스·성장·리스크 데이터를
하나의 프롬프트로 조립해 Gemini에 종합 판단을 요청한다.
호출은 alert가 '변화 감지된 종목'에 대해서만 트리거한다(무료 한도 절약).

GEMINI_API_KEY(또는 GOOGLE_API_KEY) 없으면 None 반환 → 기계적 점수 사용.
"""
from __future__ import annotations

import json
import os
import time

from utils import money, safe_num

MODEL = "gemini-2.5-flash"


def _api_key() -> str | None:
    return os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")


def llm_enabled() -> bool:
    return bool(_api_key())


def _s(v, d=1, suf=""):
    return safe_num(v, d, suf)


# ---------- 프롬프트 ----------


def build_prompt(r) -> str:
    m = r.macro or {}
    mm = m.get("metrics", {})
    macro_line = (
        f"환경 {m.get('label','측정불가')} ({_s(m.get('score'),0)}점), "
        f"기준금리 {_s(mm.get('fedfunds'),2)}%, 10년물 {_s(mm.get('y10'),2)}%, "
        f"CPI {_s(mm.get('cpi_yoy'),1)}%, 실업률 {_s(mm.get('unrate'),1)}%, "
        f"VIX {_s(mm.get('vix'),1)}, F&G {_s(mm.get('fg_score'),0)}, "
        f"장단기차 {_s(mm.get('spread'),2)}%"
    )

    sec = r.sector or {}
    if sec.get("chg_5d") is not None:
        sector_line = (f"{sec.get('label')}({sec.get('etf')}) 5일 "
                       f"{_s(sec.get('chg_5d'),1)}% · 순위 {sec.get('rank_5d')}/"
                       f"{sec.get('total')} · 보정 {sec.get('adj',0):+g}")
    else:
        sector_line = "정보 없음"

    fm = (r.fundamental or {}).get("metrics", {})
    fund_line = (
        f"PER {_s(fm.get('per'),1)}(섹터평균 {_s(fm.get('per_base'),0)}), "
        f"포워드PER {_s(fm.get('forward_pe'),1)}, PSR {_s(fm.get('psr'),1)}, "
        f"PEG {_s(fm.get('peg'),2)}, ROE {_s(fm.get('roe'),0)}%, "
        f"FCF {'양수' if (fm.get('fcf') or 0) > 0 else '음수/미상'}, "
        f"부채비율 {_s(fm.get('debt_to_equity'),0)}%, "
        f"EPS서프라이즈 {_s(fm.get('last_surprise'),1)}%, "
        f"매출성장 {_s(fm.get('rev_growth'),1)}%, "
        f"펀더멘탈 {_s((r.fundamental or {}).get('score'),0)}점"
    )
    fr = (r.fundamental or {}).get("risk", {})
    if fr.get("flag"):
        fund_line += f" / ⚠️재무위험: {', '.join(fr.get('warnings', []))}"

    ind = (r.technical or {}).get("indicators", {})
    pats = (r.technical or {}).get("patterns", [])
    tech_line = (
        f"현재가 {money(r.price, r.currency)} ({_s(ind.get('change_pct'),1)}%), "
        f"RSI일 {_s(ind.get('rsi_d'),0)}/주 {_s(ind.get('rsi_w'),0)}, "
        f"MACD {ind.get('macd_state','N/A')}, "
        f"{'MA정배열' if ind.get('aligned') else 'MA혼조'}, "
        f"거래량 {_s(ind.get('vol_ratio'),1)}배, "
        f"{r.bench_label}대비 RS {_s(ind.get('rs'),1)}, "
        f"기술 {_s((r.technical or {}).get('score'),0)}점"
        + (f", 패턴[{', '.join(pats)}]" if pats else "")
    )

    g = r.growth or {}
    gsig = ", ".join(s["category"] for s in g.get("signals", [])) or "특이 시그널 없음"
    growth_line = f"{gsig} · 가이던스 {g.get('guidance','판단불가')} · 성장 {_s(g.get('score'),0)}점"

    risk = r.risk or {}
    risk_items = ", ".join(f"{it['name']}({it['points']})"
                           for it in risk.get("items", [])) or "특이 리스크 없음"

    if r.news:
        news_lines = "\n".join(f'{i}. "{n.headline}" ({n.age})'
                               for i, n in enumerate(r.news[:6], 1))
    else:
        news_lines = "(최근 뉴스 없음)"

    return f"""[거시경제]
{macro_line}

[섹터]
{sector_line}

[{r.ticker} 펀더멘탈]
{fund_line}

[{r.ticker} 기술적]
{tech_line}

[성장성]
{growth_line}

[리스크]
{risk_items} (감점 합계 {risk.get('deduction',0)})

[최근 뉴스]
{news_lines}

[기계적 종합 점수]
{_s(r.final_score,0)}점 → {r.grade.get('stars','')} {r.recommendation}

위 데이터를 종합 분석해서 JSON으로만 답해줘:
{{
  "signal": "Strong Buy" | "Buy" | "Watch" | "Neutral" | "Avoid",
  "score_adjusted": 0~100,
  "entry": 숫자,
  "target": 숫자,
  "stop": 숫자,
  "rr_ratio": "1:N",
  "summary": "3줄 이내 핵심 근거",
  "growth_score": 0~100,
  "sentiment": "긍정" | "중립" | "부정",
  "risk_comment": "1줄 주요 리스크",
  "key_catalyst": "핵심 촉매 1줄"
}}"""


# ---------- 응답 파싱 ----------


def _extract_json(text: str) -> dict | None:
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None


def _normalize(data: dict) -> dict | None:
    signal = str(data.get("signal", "")).strip()
    if signal not in ("Strong Buy", "Buy", "Watch", "Neutral", "Avoid"):
        return None

    def fnum(k):
        try:
            return float(data[k])
        except (KeyError, TypeError, ValueError):
            return None

    return {
        "signal": signal,
        "score_adjusted": fnum("score_adjusted"),
        "entry": fnum("entry"), "target": fnum("target"), "stop": fnum("stop"),
        "rr_ratio": str(data.get("rr_ratio", "")).strip(),
        "summary": str(data.get("summary", "")).strip(),
        "growth_score": fnum("growth_score"),
        "sentiment": str(data.get("sentiment", "중립")).strip(),
        "risk_comment": str(data.get("risk_comment", "")).strip(),
        "key_catalyst": str(data.get("key_catalyst", "")).strip(),
    }


# ---------- 본체 ----------


def generate_json(prompt: str, label: str = "") -> dict | None:
    """프롬프트로 Gemini를 호출하고 JSON 응답(dict)을 반환. 공용 호출 함수.

    정기 분석(analyze)과 긴급 분석(emergency_analyzer)이 함께 사용한다.
    키 없음/미설치/실패/파싱 실패 시 None.
    """
    key = _api_key()
    if not key:
        return None
    try:
        from google import genai
        from google.genai import types
    except ImportError:
        print("[LLM] google-genai 미설치 → skip")
        return None

    client = genai.Client(api_key=key)
    resp = last_err = None
    for attempt in range(3):
        try:
            resp = client.models.generate_content(
                model=MODEL, contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json"),
            )
            break
        except Exception as e:  # noqa: BLE001
            last_err = e
            msg = str(e)
            daily = "PerDay" in msg or "RequestsPerDay" in msg
            transient = (not daily) and any(
                s in msg for s in ("503", "UNAVAILABLE", "429", "overloaded",
                                   "high demand", "RESOURCE_EXHAUSTED", "500"))
            if transient and attempt < 2:
                time.sleep(3 * (attempt + 1))
                continue
            if daily:
                print(f"[LLM] {label} 일일 무료 한도(20회/일) 초과 → AI 판단 생략")
            break
    if resp is None:
        print(f"[LLM] {label} 호출 실패: {last_err}")
        return None

    data = _extract_json((resp.text or "").strip())
    if data is None:
        print(f"[LLM] {label} JSON 파싱 실패")
    return data


def analyze(r) -> dict | None:
    data = generate_json(build_prompt(r), label=r.ticker)
    if data is None:
        return None
    out = _normalize(data)
    if out is None:
        print(f"[LLM] {r.ticker} 응답 형식 오류 → skip")
    return out
