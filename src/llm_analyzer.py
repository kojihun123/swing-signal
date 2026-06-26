"""LLM 종합 판단 (멀티 프로바이더: Gemini + Grok).

scorer.StockResult에 모인 거시·섹터·펀더멘탈·기술·뉴스·성장·리스크 데이터를
하나의 프롬프트로 조립해 LLM에 종합 판단을 요청한다. 실제 호출은
llm_providers가 담당(라운드로빈 분산 + 폴백)하며, 여기서는 프롬프트 조립과
JSON 파싱만 한다. 호출은 '변화 감지된 종목'에 대해서만 트리거한다(한도 절약).

활성 프로바이더 키(GEMINI_API_KEY/GOOGLE_API_KEY 또는 XAI_API_KEY)가
하나도 없으면 None 반환 → 기계적 점수 사용.
"""
from __future__ import annotations

import json

import kr_data
import llm_providers
from utils import money, safe_num


def llm_enabled() -> bool:
    return llm_providers.enabled()


def _s(v, d=1, suf=""):
    return safe_num(v, d, suf)


# ---------- 프롬프트 ----------


def build_prompt(r) -> str:
    m = r.macro or {}
    mm = m.get("metrics", {})
    mp = m.get("parts", {})
    macro_line = (
        f"환경 {m.get('label','측정불가')} ({_s(m.get('score'),0)}점 = "
        f"통화 {_s(mp.get('monetary'),0)}/경기 {_s(mp.get('economy'),0)}/"
        f"금융 {_s(mp.get('financial'),0)}/심리 {_s(mp.get('sentiment'),0)})\n"
        f"· 통화정책: 기준금리 {_s(mm.get('fedfunds'),2)}%, 10년물 "
        f"{_s(mm.get('y10'),2)}%({mm.get('y10_trend','')}), 장단기차 "
        f"{_s(mm.get('spread'),2)}%{' 역전⚠️' if mm.get('inverted') else ''}\n"
        f"· 경기: CPI {_s(mm.get('cpi_yoy'),1)}%/근원 {_s(mm.get('core_yoy'),1)}%, "
        f"실업률 {_s(mm.get('unrate'),1)}%, PMI프록시 {_s(mm.get('pmi_proxy'),1)}, "
        f"소매판매 {_s(mm.get('retail_yoy'),1)}%\n"
        f"· 금융환경: 달러 {_s(mm.get('dxy'),1)}({mm.get('dxy_trend','')}), "
        f"하이일드스프레드 {_s(mm.get('hy_spread'),2)}%, 연준대차대조표 "
        f"{mm.get('walcl_trend','')}, M2 {mm.get('m2_trend','')}, WTI "
        f"{_s(mm.get('wti'),1)}\n"
        f"· 심리: VIX {_s(mm.get('vix'),1)}, F&G {_s(mm.get('fg_score'),0)}"
        f"({mm.get('fg_label','')}), 지수추세 {mm.get('index_trend','')}"
    )
    rg = (m.get("regime") or {})
    if rg.get("summary"):
        macro_line += f"\n· 레짐: {rg['summary']}"

    sec = r.sector or {}
    links = sec.get("market_links") or {}

    def _axis(axis):
        bs = links.get(axis) or []
        return ", ".join(f"{b.get('etf')} {_s(b.get('chg_5d'), 1)}%" for b in bs) or "-"

    if links or sec.get("chg_5d") is not None:
        sector_line = (
            f"업종({sec.get('label')}): {_axis('industry')} · 순위 "
            f"{sec.get('rank_5d')}/{sec.get('total')} · 보정 {sec.get('adj', 0):+g}\n"
            f"기술축: {_axis('technology')} · 시장축: {_axis('market')} · "
            f"레짐 ×{sec.get('regime_mult', 1.0)}")
        if sec.get("country_code") == "KR":
            sector_line += (f"\n국가축: {_axis('country')} · "
                            f"보정 {sec.get('country_adj', 0):+g}")
        rot = sec.get("rotation")
        if rot:
            sector_line += (f"\n섹터 로테이션(LLM): {rot.get('rating')} · "
                            f"전망 {rot.get('outlook')} — {rot.get('reason', '')}")
        # 전체 섹터 강약 맵(시장 컨텍스트) — 종목이 시장 어디에 있는지
        smap = (r.market_context or {}).get("sectors") or {}
        if smap:
            bull = [v["label"] for v in smap.values()
                    if v.get("rating") in ("Strong Bullish", "Bullish")]
            bear = [v["label"] for v in smap.values()
                    if v.get("rating") in ("Bearish", "Strong Bearish")]
            sector_line += (f"\n시장 섹터맵 → 강세: {', '.join(bull) or '-'} / "
                            f"약세: {', '.join(bear) or '-'}")
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
        f"일목 {ind.get('ichimoku_pos','N/A')}, "
        f"거래량 {_s(ind.get('vol_ratio'),1)}배, "
        f"{r.bench_label}대비 RS {_s(ind.get('rs'),1)}, "
        f"기술 {_s((r.technical or {}).get('score'),0)}점"
        + (f", 패턴[{', '.join(pats)}]" if pats else "")
    )
    buy_sig = ind.get("buy_signals") or []
    sell_sig = ind.get("sell_signals") or []
    if buy_sig:
        tech_line += f"\n  ✅ 매수 시그널: {', '.join(buy_sig)}"
    if sell_sig:
        tech_line += f"\n  ⚠️ 매도/과열 시그널: {', '.join(sell_sig)}"

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

    # KR 종목: 수급·공매도·증권사 리포트 보조정보 (US 종목은 빈 문자열)
    kr_ctx = kr_data.as_prompt_context(r.ticker, data=r.kr_extra or None)
    kr_block = f"\n\n[국내 수급·공매도·리포트]\n{kr_ctx}" if kr_ctx else ""

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
{news_lines}{kr_block}

[기계적 종합 점수]
{_s(r.final_score,0)}점 → {r.grade.get('stars','')} {r.recommendation}

위 데이터를 종합 분석해서 JSON으로만 답해줘.
(문자열 값은 초보 투자자도 이해할 쉬운 한국어로. RS/Fit/Momentum 같은 영어 약어 대신
'상대강도/적합도/상승탄력' 등 우리말로. 어려운 경제용어 자제, 한자·영어 혼용 금지):
지침: ① entry(매수가)가 현재가보다 낮으면 그 이유를 summary에 한 마디 넣어줘
(예: "기술적 과열로 5% 조정 시 진입 권장"). ② 과열/매도 시그널이 있는데 관망이면
'왜 추격매수가 아닌 대기인지'를 risk_comment에 연결해. ③ key_catalyst는 이 종목만의
구체적 촉매(예: "HBM 수요 증가로 메모리 업황 직접 수혜")로, 일반론('AI 성장') 금지.
{{
  "signal": "Strong Buy" | "Buy" | "Watch" | "Neutral" | "Avoid",
  "score_adjusted": 0~100,
  "entry": 숫자,
  "target": 숫자,
  "stop": 숫자,
  "rr_ratio": "1:N",
  "summary": "3줄 이내 핵심 근거 (관망이면 진입 조건 포함)",
  "growth_score": 0~100,
  "sentiment": "긍정" | "중립" | "부정",
  "risk_comment": "1줄 주요 리스크 (과열 시 추천과 연결)",
  "key_catalyst": "이 종목만의 구체적 촉매 1줄"
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
    """프롬프트로 LLM을 호출하고 JSON 응답(dict)을 반환. 공용 호출 함수.

    실제 호출/폴백은 llm_providers.generate_text가 담당한다. 정기 분석(analyze),
    긴급 분석(emergency_analyzer), 인트라데이 분석이 모두 이 함수를 쓴다.
    키 없음/미설치/실패/파싱 실패 시 None.
    """
    text = llm_providers.generate_text(prompt, label)
    if not text:
        return None
    data = _extract_json(text)
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


# ---------- 인트라데이(시계열) 분석 ----------


def _etf_line(axis_briefs: list[dict]) -> str:
    """[{etf,label,chg_1d,chg_5d}] → '반도체 SOXX 1d-1.8%/5d-3.1%, ...' 한 줄."""
    parts = []
    for b in axis_briefs or []:
        parts.append(f"{b.get('label', b.get('etf'))} {b.get('etf')} "
                     f"1d{_s(b.get('chg_1d'))}%/5d{_s(b.get('chg_5d'))}%")
    return ", ".join(parts) or "-"


def _etf_block(links: dict | None) -> str:
    links = links or {}
    rows = []
    for axis, ko in (("industry", "업종"), ("technology", "기술"),
                     ("market", "시장"), ("country", "국가")):
        if links.get(axis):
            rows.append(f"  {ko}: {_etf_line(links[axis])}")
    return "\n".join(rows) or "  (ETF 정보 없음)"


def build_intraday_prompt(ticker: str, baseline: dict, series: list[dict],
                          current: dict) -> str:
    """아침 베이스라인 + 그날 누적 스냅샷 + 현재 상태로 인트라데이 프롬프트 조립."""
    b = baseline or {}
    base_line = (f"신호 {b.get('signal', 'N/A')} · 점수 {b.get('score', 'N/A')} · "
                 f"진입 {b.get('entry_price', 'N/A')} / 목표 {b.get('target_price', 'N/A')} "
                 f"/ 손절 {b.get('stop_price', 'N/A')}\n  근거: {b.get('summary', '') or '-'}")

    # 그날 이전 스냅샷들 — 각 1줄 요약(추세 파악용)
    hist = []
    for s in (series or [])[-6:]:
        t = str(s.get("t", ""))[11:16]      # HH:MM
        line = f"  {t} {_s(s.get('price'))}({_s(s.get('change_pct'))}%)"
        if s.get("llm", {}).get("action"):
            line += f" · {s['llm']['action']}"
        if s.get("new_news"):
            line += f" · 뉴스{len(s['new_news'])}건"
        hist.append(line)
    hist_block = "\n".join(hist) or "  (이번이 첫 스냅샷)"

    cur = current or {}
    news_lines = "\n".join(f'  - "{h}"' for h in cur.get("new_news", [])) or "  (없음)"
    triggers = ", ".join(cur.get("triggers", [])) or "-"

    return f"""[{ticker} 인트라데이 분석] — 현재 세션: {cur.get('session', '?')}

[오늘 아침 베이스라인]
  {base_line}

[오늘 누적 흐름]
{hist_block}

[현재 상태]
  가격 {_s(cur.get('price'))} (직전 대비 {_s(cur.get('change_pct'))}%), 거래량 {_s(cur.get('vol_ratio'))}배
  발동 트리거: {triggers}
[현재 ETF 영향력(축별)]
{_etf_block(cur.get('etf'))}
[새로 뜬 뉴스]
{news_lines}

위 '아침 베이스라인 → 오늘 흐름 → 현재'를 비교해 JSON으로만 답해줘.
직전 대비 무엇이 바뀌었는지, ETF/뉴스가 호재인지 악재인지, 액션을 바꿔야 하는지 판단.
(문자열 값은 초보 투자자도 이해할 쉬운 한국어로. 영어 약어·어려운 경제용어 자제):
{{
  "signal": "Strong Buy" | "Buy" | "Watch" | "Neutral" | "Avoid",
  "score_adjusted": 0~100,
  "action": "유지" | "관심↑" | "관심↓" | "진입고려" | "이탈/축소",
  "summary": "직전 대비 변화 + 하루 흐름 핵심 (2~3줄)",
  "news_impact": "신규 뉴스의 영향 1줄 (없으면 빈 문자열)",
  "sentiment": "긍정" | "중립" | "부정"
}}"""


def _normalize_intraday(data: dict) -> dict | None:
    signal = str(data.get("signal", "")).strip()
    if signal not in ("Strong Buy", "Buy", "Watch", "Neutral", "Avoid"):
        signal = ""                          # 신호 누락은 허용(서술만 받음)
    try:
        score = float(data["score_adjusted"])
    except (KeyError, TypeError, ValueError):
        score = None
    return {
        "signal": signal,
        "score_adjusted": score,
        "action": str(data.get("action", "")).strip(),
        "summary": str(data.get("summary", "")).strip(),
        "news_impact": str(data.get("news_impact", "")).strip(),
        "sentiment": str(data.get("sentiment", "중립")).strip(),
    }


def analyze_intraday(ticker: str, baseline: dict, series: list[dict],
                     current: dict) -> dict | None:
    """인트라데이 변화 분석. 결과 dict 또는 None(키 없음/실패)."""
    prompt = build_intraday_prompt(ticker, baseline, series, current)
    data = generate_json(prompt, label=f"{ticker}/intraday")
    if data is None:
        return None
    out = _normalize_intraday(data)
    if out is None:
        print(f"[LLM] {ticker} 인트라데이 응답 형식 오류 → skip")
    return out
