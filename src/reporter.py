"""텔레그램 메시지 포맷·전송 + CSV 저장 (별점 등급 기반).

메시지 1: 거시경제 브리핑 (하루 1회)
메시지 2: 종목 분석 (종목마다)
메시지 3: 변화 알림 (변화 감지 시만)
메시지 4: 최종 요약 (마지막)
"""
from __future__ import annotations

import csv
import os
import re
import time

import requests

from scorer import StockResult
from utils import fmt_kst, money, now_notify, pct, safe_num, today_stamp

BAR = "━" * 20
SUB = "-" * 40
CARD_SEP = "─" * 14   # 모바일 카드 구분선(짧게)

# 영문 신호 → 한글 표기
SIGNAL_KO = {
    "Strong Buy": "적극매수", "Buy": "매수", "Watch": "관망",
    "Neutral": "중립", "Avoid": "회피",
}


def _sig_ko(signal: str) -> str:
    return SIGNAL_KO.get(signal, signal or "중립")


# 애널리스트 투자의견(yfinance recommendationKey) → 한글
_RECO_KO = {
    "strong_buy": "강력매수", "buy": "매수", "hold": "중립",
    "underperform": "비중축소", "sell": "매도",
}


def _reco_ko(key) -> str:
    return _RECO_KO.get(str(key or "").lower().replace(" ", "_"), "")


def _z(v) -> str:
    """점수 표시(None→'-')."""
    return f"{v:.0f}" if isinstance(v, (int, float)) else "-"


def _lvl(v) -> str:
    """진입/목표/손절 숫자 (통화기호 없이 천단위 콤마, 1000↑은 정수)."""
    if not isinstance(v, (int, float)):
        return "-"
    return f"{v:,.0f}" if abs(v) >= 1000 else f"{round(v, 2):g}"


def _hm() -> str:
    """현재 KST 'HH:MM' (알림 말미 타임스탬프용)."""
    return now_notify().strftime("%H:%M")


def _hm_from(iso: str | None) -> str:
    """ISO 문자열에서 'HH:MM' 추출. 실패 시 '--:--'."""
    if not iso:
        return "--:--"
    try:
        from datetime import datetime
        return datetime.fromisoformat(iso).strftime("%H:%M")
    except (ValueError, TypeError):
        return "--:--"


def _s(v, d=1, suf=""):
    return safe_num(v, d, suf)


def _clip(text, limit: int = 120) -> str:
    """AI 설명 길이 제한(기본 120자, 2문장). 공백 정규화 + 말줄임."""
    t = " ".join(str(text or "").split())
    parts = re.split(r"(?<=[.!?。…])\s+", t)
    if len(parts) > 2:
        t = " ".join(parts[:2])
    return t if len(t) <= limit else t[:limit - 1].rstrip() + "…"


# GICS 경기민감/방어 (로테이션 흐름 판정)
_CYC_ETF = {"XLK", "SOXX", "XLF", "XLI", "XLY", "XLB", "XLE"}
_DEF_ETF = {"XLU", "XLP", "XLV", "XLRE"}

# 종합점수 가중치 (scorer.WEIGHTS와 동일 — 점수 산출 근거 표시용)
_SCORE_W = {"macro": 0.20, "sector": 0.15, "fundamental": 0.25,
            "growth": 0.10, "technical": 0.15, "sentiment": 0.10}


# ---------- 컴포넌트 근거 ----------


def _reason_macro(r: StockResult) -> str:
    mm = (r.macro or {}).get("metrics", {})
    bits = []
    if mm.get("y10_trend"):
        bits.append(f"10년물 {mm['y10_trend']}")
    if mm.get("cpi_yoy") is not None:
        bits.append(f"CPI {_s(mm['cpi_yoy'],1)}%")
    return (r.macro or {}).get("label", "측정불가") + (" · " + "·".join(bits) if bits else "")


def _reason_sector(r: StockResult) -> str:
    s = r.sector or {}
    if s.get("chg_5d") is None:
        return "정보 없음"
    flow = "자금 유입" if s["chg_5d"] > 0 else "자금 유출"
    return f"{s.get('label')} {flow}({pct(s['chg_5d'])})"


def _reason_fundamental(r: StockResult) -> str:
    fm = (r.fundamental or {}).get("metrics", {})
    bits = []
    if fm.get("roe") is not None:
        bits.append(f"ROE {_s(fm['roe'],0)}%")
    if fm.get("fcf") is not None:
        bits.append("FCF " + ("양호" if fm["fcf"] > 0 else "음수"))
    if fm.get("per") is not None:
        bits.append(f"PER {_s(fm['per'],0)}")
    return "·".join(bits) or "데이터 부족"


def _reason_growth(r: StockResult) -> str:
    g = r.growth or {}
    sigs = [s["category"] for s in g.get("signals", [])]
    txt = "·".join(sigs[:2]) if sigs else ""
    if g.get("guidance") in ("상향", "하향"):
        txt = (txt + " · " if txt else "") + f"가이던스 {g['guidance']}"
    return txt or "특이 시그널 없음"


def _reason_technical(r: StockResult) -> str:
    ind = (r.technical or {}).get("indicators", {})
    pats = (r.technical or {}).get("patterns", [])
    bits = [f"MACD {ind.get('macd_state','N/A')}"]
    if pats:
        bits.append(pats[0])
    return "·".join(bits)


def _reason_sentiment(r: StockResult) -> str:
    if r.llm and r.llm.get("sentiment"):
        return f"뉴스 {r.llm['sentiment']}"
    return f"뉴스 {(r.sentiment or {}).get('label','중립')}"


_COMP = [
    ("Macro", "macro", _reason_macro),
    ("Sector", "sector", _reason_sector),
    ("Fundamental", "fundamental", _reason_fundamental),
    ("Growth", "growth", _reason_growth),
    ("Technical", "technical", _reason_technical),
    ("Sentiment", "sentiment", _reason_sentiment),
]


# ---------- 1) 거시 브리핑 ----------


def format_macro_briefing(macro, rotation=None) -> str:
    lines = [BAR, f"🌍 거시경제 브리핑 [{fmt_kst()}]", BAR]
    if macro.available:
        lines.append(f"📊 거시 환경: {macro.label} ({macro.score:.0f}점)")
    else:
        lines.append("📊 거시 환경: 측정불가 (FRED 키 미설정 → 보정 미적용)")
    m = macro.metrics
    if macro.available:
        p = getattr(macro, "parts", {}) or {}
        lines.append(f"   (통화 {_s(p.get('monetary'),0)} · 경기 {_s(p.get('economy'),0)}"
                     f" · 금융 {_s(p.get('financial'),0)} · 심리 {_s(p.get('sentiment'),0)})")
        if m.get("fedfunds") is not None:
            lines.append(f"💰 금리: {_s(m.get('fedfunds'),2)}% · "
                         f"10년물 {_s(m.get('y10'),2)}% ({m.get('y10_trend','')})")
        if m.get("cpi_yoy") is not None:
            econ = (f"📈 경기: CPI {_s(m.get('cpi_yoy'),1)}%/근원 "
                    f"{_s(m.get('core_yoy'),1)}%")
            if m.get("pmi_proxy") is not None:
                econ += f" · PMI {_s(m.get('pmi_proxy'),1)}"
            if m.get("retail_yoy") is not None:
                econ += f" · 소매 {_s(m.get('retail_yoy'),1)}%"
            lines.append(econ)
        emp = f"👷 고용: 실업률 {_s(m.get('unrate'),1)}%"
        if m.get("nfp_change") is not None:
            emp += f" · NFP {m['nfp_change']/10:+.0f}만"
        lines.append(emp)
        fin = []
        if m.get("dxy") is not None:
            fin.append(f"달러 {_s(m.get('dxy'),1)}({m.get('dxy_trend','')})")
        if m.get("hy_spread") is not None:
            fin.append(f"HY스프레드 {_s(m.get('hy_spread'),2)}%")
        if m.get("walcl_trend"):
            fin.append(f"연준B/S {m.get('walcl_trend')}")
        if fin:
            lines.append("🏦 금융환경: " + " · ".join(fin))
    senti = []
    if m.get("vix") is not None:
        senti.append(f"VIX {_s(m.get('vix'),1)}")
    if m.get("fg_score") is not None:
        senti.append(f"F&G {_s(m.get('fg_score'),0)}({m.get('fg_label','')})")
    if m.get("index_trend"):
        senti.append(f"지수 {m.get('index_trend')}")
    if senti:
        lines.append("😰 심리: " + " · ".join(senti))
    if macro.available and m.get("spread") is not None:
        warn = " 역전 중 ⚠️" if m.get("inverted") else ""
        lines.append(f"⚠️ 장단기 금리차: {_s(m.get('spread'),2)}%{warn}")

    if rotation is not None:
        if rotation.top3:
            lines += ["", "🔥 자금 유입 TOP3"]
            for i, s in enumerate(rotation.top3, 1):
                lines.append(f"  {i}. {s.label}({s.etf}) {pct(s.chg_5d)}")
        if rotation.bottom3:
            lines += ["", "❄️ 자금 유출 TOP3"]
            for i, s in enumerate(rotation.bottom3, 1):
                lines.append(f"  {i}. {s.label}({s.etf}) {pct(s.chg_5d)}")
        if rotation.defensive_strong:
            lines += ["", "⚠️ 방어섹터 강세 → 시장 위험 신호"]
    lines.append(BAR)
    return "\n".join(lines)


# ---------- 2) 종목 분석 ----------


def format_stock(r: StockResult, triggers: list[str] | None = None) -> str:
    if not r.ok:
        return f"{r.ticker}  분석 실패: {r.error}"
    cur = r.currency
    g = r.grade
    ind = (r.technical or {}).get("indicators", {})
    lines = [BAR, f"📊 {r.ticker} · {r.name}", BAR,
             f"{g['stars']} {g['en']} · {r.final_score:.0f}점", ""]

    lines.append(f"현재가: {money(r.price, cur)} ({pct(ind.get('change_pct'))})")
    vr = ind.get("vol_ratio")
    if vr is not None:
        lines.append(f"거래량: 평균 대비 {vr:.1f}배")
    lines.append("")

    lv = r.levels or {}
    if lv.get("entry") is not None and lv.get("target") is not None:
        lines.append(f"💰 진입  {money(lv['entry'], cur)}")
        lines.append(f"🎯 목표  {money(lv['target'], cur)} ({pct(lv['target_pct'])})")
        lines.append(f"🛑 손절  {money(lv['stop'], cur)} ({pct(lv['stop_pct'])})")
        if lv.get("rr"):
            lines.append(f"📐 R:R   1 : {lv['rr']:.1f}")
        lines.append("")

    lines.append("점수 상세:")
    for label, key, fn in _COMP:
        sc = r.component_scores.get(key, 0)
        lines.append(f"  {label:<12} {sc:>3.0f} · {fn(r)}")
    lines.append("")

    # 핵심 / 리스크
    if r.llm:
        if r.llm.get("key_catalyst"):
            lines.append(f"핵심: {r.llm['key_catalyst']}")
        elif r.llm.get("summary"):
            lines.append(f"핵심: {r.llm['summary']}")
        if r.llm.get("risk_comment"):
            lines.append(f"⚠️ 리스크: {r.llm['risk_comment']}")
    # 재무위험 경고
    fr = (r.fundamental or {}).get("risk", {})
    if fr.get("flag"):
        lines.append(f"⚠️ 재무위험: {', '.join(fr.get('warnings', []))} (-15)")
    # 이벤트 리스크
    ritems = (r.risk or {}).get("items", [])
    if ritems:
        lines.append("⚠️ 이벤트: " + ", ".join(it["name"] for it in ritems))
    if triggers:
        lines.append("⚡ 변화: " + ", ".join(triggers))

    lines.append(BAR)
    return "\n".join(lines)


# ---------- 3) 변화 알림 ----------


def format_change_alert(r: StockResult, triggers: list[str]) -> str:
    ind = (r.technical or {}).get("indicators", {})
    lines = [f"⚡ {r.ticker} 변화 감지!", "",
             f"{r.grade['stars']} {r.recommendation} · {r.final_score:.0f}점",
             f"현재가: {money(r.price, r.currency)} ({pct(ind.get('change_pct'))})"]
    vr = ind.get("vol_ratio")
    if vr is not None:
        lines.append(f"거래량: 평균 대비 {vr:.1f}배")
    lines.append("")
    lines.append("트리거: " + ", ".join(triggers))
    lines.append(f"[{fmt_kst()}]")
    return "\n".join(lines)


# ---------- 4) 최종 요약 ----------

_ORDER = ["Strong Buy", "Buy", "Watch", "Neutral", "Avoid"]
_STARS = {"Strong Buy": "★★★★★", "Buy": "★★★★☆", "Watch": "★★★☆☆",
          "Neutral": "★★☆☆☆", "Avoid": "★☆☆☆☆"}


def format_summary(results: list[StockResult]) -> str:
    ok = [r for r in results if r.ok]
    by_grade = {k: [] for k in _ORDER}
    for r in sorted(ok, key=lambda x: x.final_score, reverse=True):
        by_grade.get(r.recommendation, by_grade["Avoid"]).append(r)

    lines = [BAR, f"📋 오늘의 최종 요약 [{today_stamp()}]", BAR]
    for grade in _ORDER:
        items = by_grade[grade]
        if not items:
            continue
        lines.append(f"{_STARS[grade]} {grade} ({len(items)}개)")
        lines.append("  " + " · ".join(f"{r.ticker} {r.final_score:.0f}점"
                                        for r in items))
    # 1순위
    top = next((r for g in _ORDER for r in by_grade[g]), None)
    if top:
        bits = []
        if (top.sector or {}).get("adj", 0) > 0:
            bits.append("섹터강세")
        if (top.fundamental or {}).get("score", 0) >= 75:
            bits.append("펀더 우수")
        if top.technical.get("patterns"):
            bits.append(top.technical["patterns"][0])
        lines += ["", f"🏆 오늘의 1순위: {top.ticker} {top.final_score:.0f}점"]
        if bits:
            lines.append("  " + " + ".join(bits))

    # 내일 주의 (어닝 임박 + 이벤트)
    warns = []
    for r in ok:
        d = (r.fundamental or {}).get("metrics", {}).get("days_to_earnings")
        if d is not None and 0 <= d <= 3:
            warns.append(f"{r.ticker} 어닝 D-{d}")
    if warns:
        lines += ["", "⚠️ 내일 주의", "  " + " · ".join(warns[:4])]

    lines += ["", f"[{fmt_kst()}]", BAR]
    return "\n".join(lines)


# ---------- 텔레그램 ----------


def telegram_enabled() -> bool:
    return bool(os.environ.get("TELEGRAM_BOT_TOKEN")
                and os.environ.get("TELEGRAM_CHAT_ID"))


def _split_message(text: str, limit: int = 4000) -> list[str]:
    """텔레그램 4096자 제한 대비 줄 단위 분할."""
    if len(text) <= limit:
        return [text]
    chunks, buf = [], ""
    for line in text.split("\n"):
        if len(buf) + len(line) + 1 > limit and buf:
            chunks.append(buf)
            buf = ""
        buf += (line + "\n")
    if buf.strip():
        chunks.append(buf)
    return chunks


def send_telegram_message(text: str, retries: int = 2) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return False
    # 길면 여러 메시지로 분할 전송
    parts = _split_message(text)
    if len(parts) > 1:
        return all(send_telegram_message(p, retries) for p in parts)
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    for attempt in range(retries + 1):
        try:
            rr = requests.post(url, data={
                "chat_id": chat_id, "text": text,
                "disable_web_page_preview": True}, timeout=30)
            if rr.ok:
                return True
            if rr.status_code == 429 and attempt < retries:
                try:
                    wait = int(rr.json().get("parameters", {})
                               .get("retry_after", 3))
                except Exception:  # noqa: BLE001
                    wait = 3
                time.sleep(min(wait, 30))
                continue
            print(f"[텔레그램] 전송 실패 {rr.status_code}: {rr.text[:200]}")
            if attempt >= retries:
                return False
        except Exception as e:  # noqa: BLE001
            if attempt < retries:
                time.sleep(3)
                continue
            print(f"[텔레그램 전송 실패] {e}")
            return False
    return False


# ---------- 터미널 + CSV ----------


def format_report(results: list[StockResult]) -> str:
    out = [BAR, f"[{fmt_kst()}] 일일 분석", BAR]
    for r in sorted(results, key=lambda x: (x.ok, x.final_score), reverse=True):
        if r.ok:
            cs = r.component_scores
            out.append(f"{r.ticker:10} {r.final_score:5.1f} {r.grade['stars']} "
                       f"{r.recommendation:10} "
                       f"[M{cs.get('macro',0):.0f} S{cs.get('sector',0):.0f} "
                       f"F{cs.get('fundamental',0):.0f} G{cs.get('growth',0):.0f} "
                       f"T{cs.get('technical',0):.0f} Se{cs.get('sentiment',0):.0f}]")
            fr = (r.fundamental or {}).get("risk", {})
            if fr.get("flag"):
                out.append(f"   ⚠️ 재무위험: {', '.join(fr.get('warnings', []))}")
        else:
            out.append(f"{r.ticker:10} 분석 실패: {r.error}")
    out.append(BAR)
    return "\n".join(out)


def save_reports(results: list[StockResult], data_dir: str = "data") -> None:
    os.makedirs(os.path.join(data_dir, "reports"), exist_ok=True)
    os.makedirs(os.path.join(data_dir, "history"), exist_ok=True)
    stamp = today_stamp()
    ts = fmt_kst()

    path = os.path.join(data_dir, "reports", f"{stamp}.csv")
    fields = ["ticker", "name", "final_score", "grade", "recommendation", "price",
              "macro", "sector", "fundamental", "growth", "technical", "sentiment",
              "per", "roe", "fcf", "rsi_d", "macd", "eps_surprise",
              "days_to_earnings", "risk_deduction", "entry", "target", "stop", "rr"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in results:
            cs = r.component_scores
            fm = (r.fundamental or {}).get("metrics", {})
            ind = (r.technical or {}).get("indicators", {})
            lv = r.levels or {}
            w.writerow({
                "ticker": r.ticker, "name": r.name,
                "final_score": r.final_score, "grade": r.grade.get("stars", ""),
                "recommendation": r.recommendation,
                "price": round(r.price, 2) if r.price == r.price else "",
                "macro": cs.get("macro", ""), "sector": cs.get("sector", ""),
                "fundamental": cs.get("fundamental", ""), "growth": cs.get("growth", ""),
                "technical": cs.get("technical", ""), "sentiment": cs.get("sentiment", ""),
                "per": _s(fm.get("per"), 1), "roe": _s(fm.get("roe"), 1),
                "fcf": fm.get("fcf", ""), "rsi_d": _s(ind.get("rsi_d"), 1),
                "macd": ind.get("macd_state", ""),
                "eps_surprise": _s(fm.get("last_surprise"), 1),
                "days_to_earnings": fm.get("days_to_earnings", ""),
                "risk_deduction": (r.risk or {}).get("deduction", 0),
                "entry": round(lv["entry"], 2) if lv.get("entry") else "",
                "target": round(lv["target"], 2) if lv.get("target") else "",
                "stop": round(lv["stop"], 2) if lv.get("stop") else "",
                "rr": round(lv["rr"], 2) if lv.get("rr") else "",
            })
    print(f"[저장] {path}")

    hist = os.path.join(data_dir, "history", "scores_history.csv")
    new = not os.path.exists(hist)
    with open(hist, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["datetime_kst", "date", "ticker", "final_score",
                        "recommendation"])
        for r in results:
            w.writerow([ts, stamp, r.ticker, r.final_score, r.recommendation])
    print(f"[저장] {hist}")


# ════════════════════════════════════════════════════════════════════
#  daily_result.json 기반 메시지 (v5: 풀분석/장중감시/긴급/마감)
# ════════════════════════════════════════════════════════════════════

_SIGNAL_ORDER = ["Strong Buy", "Buy", "Watch", "Neutral", "Avoid"]


def _md(date_str: str | None) -> str:
    """'2026-06-27' → '06-27'."""
    if date_str and len(date_str) >= 10:
        return date_str[5:10]
    return today_stamp()


# ---------- [1] 오늘의 분석 리포트 ----------


_RATING_EMO = {"Strong Bullish": "🟢🟢", "Bullish": "🟢", "Neutral": "⚪",
               "Bearish": "🔴", "Strong Bearish": "🔴🔴"}
_RATING_RANK = {"Strong Bullish": 2, "Bullish": 1, "Neutral": 0,
                "Bearish": -1, "Strong Bearish": -2}

# ── 출력 전용 한국어 용어 변환(내부 계산값은 그대로, 표시만 쉬운 말로) ──
_RATING_KO = {"Strong Bullish": "매우 강세", "Bullish": "강세", "Neutral": "중립",
              "Bearish": "약세", "Strong Bearish": "매우 약세"}
_PHASE_KO = {"Expansion": "경기 확장", "Neutral": "중립", "Contraction": "경기 위축",
             "판단불가": "판단 불가"}
_RISK_KO = {"Risk-On": "위험 선호", "Neutral": "중립", "Risk-Off": "위험 회피"}
_LIQ_KO = {"Improving": "개선", "Neutral": "중립", "Tightening": "위축"}
_TREND_KO = {"정배열": "상승 추세", "혼조": "방향성 없음", "역배열": "하락 추세"}
_MOM_KO = {"Strong": "탄력 강함", "Moderate": "탄력 보통", "Weak": "탄력 약함"}
_FIT_KO = {"High": "적합도 높음", "Medium": "적합도 보통", "Low": "적합도 낮음"}


def _ko(table: dict, v, default: str = "") -> str:
    return table.get(v, v if v is not None else default)


def _rotation_lines(detail: dict) -> list[str]:
    """섹터 상세 → 로테이션 흐름(유입/유출 + 스타일 우위)."""
    scored = [d for d in detail.values() if d.get("score") is not None]
    if not scored:
        return []
    ranked = sorted(scored, key=lambda d: (_RATING_RANK.get(d.get("rating"), 0),
                                           d.get("score") or 0), reverse=True)
    inflow = [d["label"] for d in ranked[:3]
              if _RATING_RANK.get(d.get("rating"), 0) >= 1 or (d.get("score") or 0) >= 65]
    outflow = [d["label"] for d in ranked[-3:][::-1]
               if _RATING_RANK.get(d.get("rating"), 0) <= -1 or (d.get("score") or 0) <= 45]
    cyc = [d["score"] for k, d in detail.items()
           if k in _CYC_ETF and d.get("score") is not None]
    deff = [d["score"] for k, d in detail.items()
            if k in _DEF_ETF and d.get("score") is not None]
    ca = sum(cyc) / len(cyc) if cyc else 0
    da = sum(deff) / len(deff) if deff else 0
    style = ("성장주 우위" if ca > da + 3 else "방어주 우위" if da > ca + 3
             else "성장·방어 혼조")
    out = []
    if inflow:
        out.append(f"  ▲ 유입: {' · '.join(inflow)}")
    if outflow:
        out.append(f"  ▼ 유출: {' · '.join(outflow)}")
    out.append(f"  ↔ {style}")
    return out


def _gauge(value, maximum: int = 100, blocks: int = 10) -> str:
    """0~max 값을 ■/□ 게이지 막대로."""
    try:
        filled = round(float(value) / maximum * blocks)
    except (TypeError, ValueError):
        return ""
    filled = max(0, min(blocks, filled))
    return "■" * filled + "□" * (blocks - filled)


def _decision_reasons(t: dict) -> tuple[str, str, list[str]]:
    """신호별 의사결정 근거 (이모지, 라벨, 항목 최대 3개)."""
    sc = t.get("scores", {})
    pairs = [("업종", sc.get("sector")), ("시장환경", sc.get("macro")),
             ("기업가치", sc.get("fundamental")), ("차트", sc.get("technical"))]
    strong = [n for n, v in pairs if v is not None and v >= 75]
    weak = [n for n, v in pairs if v is not None and v <= 60]
    sig = t.get("signal", "")
    if sig in ("Strong Buy", "Buy"):
        return "🟢", "매수근거", [f"{n} 강세" for n in strong[:3]] or ["종합 양호"]
    if sig == "Watch":
        items = [f"{n} 양호" for n in strong[:2]] + [f"{n} 애매" for n in weak[:1]]
        return "🟡", "관망근거", items or ["혼조"]
    return "🔴", "회피근거", [f"{n} 약세" for n in weak[:3]] or ["종합 부진"]


def format_daily_report(daily: dict) -> str:
    macro = daily.get("macro", {})
    sector = daily.get("sector", {})
    mm = macro.get("metrics", {})
    mp = macro.get("parts", {})
    regime = macro.get("regime", {})
    brief = daily.get("market_brief") or {}
    detail = sector.get("detail", {})

    lines = [BAR, f"📊 오늘의 분석 리포트 [{_md(daily.get('date'))}]", BAR]

    # ── 오늘의 핵심 테마 (item 2) ──
    if brief.get("themes"):
        lines.append("🗓 오늘의 핵심 테마")
        for th in brief["themes"][:5]:
            lines.append(f"  • {th}")
        lines.append("")

    # ── 거시 + 세부 구성 (item 4) ──
    if macro.get("available"):
        lines.append(f"🌍 거시 환경 {macro.get('score',0):.0f}점 "
                     f"{_gauge(macro.get('score'))} ({macro.get('label','')})")
        if mp:
            lines.append(
                f"  통화정책 {_z(mp.get('monetary'))}/25 · 경기 {_z(mp.get('economy'))}/30 · "
                f"금융여건 {_z(mp.get('financial'))}/25 · 투자심리 {_z(mp.get('sentiment'))}/20")
    else:
        lines.append("🌍 거시 환경: 측정 불가 (FRED 키 미설정)")
    macro_bits = []
    if mm.get("fedfunds") is not None:
        macro_bits.append(f"금리 {safe_num(mm.get('fedfunds'),2)}%")
    if mm.get("cpi_yoy") is not None:
        macro_bits.append(f"물가(CPI) {safe_num(mm.get('cpi_yoy'),1)}%")
    if mm.get("dxy") is not None:
        macro_bits.append(f"달러 {safe_num(mm.get('dxy'),1)}")
    if mm.get("vix") is not None:
        macro_bits.append(f"변동성(VIX) {safe_num(mm.get('vix'),1)}")
    if mm.get("fg_score") is not None:
        fgl = f"({mm.get('fg_label')})" if mm.get("fg_label") else ""
        macro_bits.append(f"투자심리 {safe_num(mm.get('fg_score'),0)}{fgl}")
    if macro_bits:
        lines.append("  " + " · ".join(macro_bits))

    # ── 현재 시장 상황 (item 3) ──
    if regime.get("summary"):
        lines.append("")
        lines.append("🧭 현재 시장 상황")
        lines.append(f"  국면 {_ko(_PHASE_KO, regime.get('phase'))} · "
                     f"위험도 {_ko(_RISK_KO, regime.get('risk'))} · "
                     f"유동성 {_ko(_LIQ_KO, regime.get('liquidity'))}")
        rot_lines = _rotation_lines(detail)
        if rot_lines:
            lines.append("  💸 자금 이동")
            lines += rot_lines
    # 논리 일관성 (item 6)
    if brief.get("consistency"):
        lines.append(f"  ⚖️ {brief['consistency']}")

    # ── 섹터 강도 + 근거 (item 5) ──
    if detail:
        ranked = sorted(detail.values(),
                        key=lambda d: (_RATING_RANK.get(d.get("rating"), 0),
                                       d.get("score") or 0), reverse=True)
        lines.append("")
        lines.append("🔥 강한 업종 순위 (AI 분석) — 상위 5")
        for d in ranked[:5]:
            emo = _RATING_EMO.get(d.get("rating"), "")
            bits = []
            if d.get("rs_1m") is not None:
                bits.append(f"상대강도 {pct(d['rs_1m'])}")
            if d.get("trend"):
                bits.append(_ko(_TREND_KO, d["trend"]))
            if d.get("momentum"):
                bits.append(_ko(_MOM_KO, d["momentum"]))
            if d.get("macro_fit"):
                bits.append(_ko(_FIT_KO, d["macro_fit"]))
            lines.append(f"  {emo} {d['label']} {_z(d.get('score'))}점 — "
                         + " · ".join(bits))
        # 하위 (상위 5와 겹치지 않게, 약한 순) — 종목이 약한 업종일 수도 있어 표시
        weak = ranked[max(5, len(ranked) - 5):][::-1]
        if weak:
            lines.append("❄️ 약한 업종: " + " · ".join(
                f"{_RATING_EMO.get(d.get('rating'),'')}{d['label']} {_z(d.get('score'))}점"
                for d in weak))
    if sector.get("defensive_strong"):
        lines.append("⚠️ 방어 업종 강세 → 시장 경계 신호")

    # ── 종목 (점수 내림차순) ──
    tickers = daily.get("tickers", {})
    ordered = sorted(tickers, key=lambda s: tickers[s].get("score", 0), reverse=True)
    for sym in ordered:
        lines.append("")
        lines.append(CARD_SEP)
        lines += _format_ticker_block(sym, tickers[sym])

    # ── 오늘의 실행 전략 + 한 줄 요약 ──
    if brief.get("strategy"):
        lines.append("")
        lines.append("🎯 오늘의 전략")
        for s in brief["strategy"][:3]:
            lines.append(f"  • {s}")
    if brief.get("summary"):
        lines.append("")
        lines.append(f"📝 {brief['summary']}")

    lines.append(BAR)
    return "\n".join(lines)


def _format_ticker_block(sym: str, t: dict) -> list[str]:
    """모바일 텔레그램용 종목 카드 (중간 상세 — 짧은 줄, 핵심만)."""
    cur = t.get("currency", "USD")

    # 헤더: ⭐ 회사명(심볼) · 점수 신호
    name = t.get("name") or sym
    head_name = f"{name} ({sym})" if name != sym else sym
    lines = [f"⭐ {head_name} · {t.get('score', 0):.0f} {_sig_ko(t.get('signal', ''))}"]

    # 현재가 (변화%)
    if t.get("current_price") is not None:
        chg = f"  ({pct(t['change_pct'])})" if t.get("change_pct") is not None else ""
        lines.append(f"{money(t['current_price'], cur)}{chg}")

    # 진입 / 목표 / 손절 (3줄, 통화기호 생략해 줄 짧게)
    # 매매 대상(적극매수/매수/관망)만 표시 — 회피/중립엔 노이즈라 생략
    tradeable = t.get("signal") in ("Strong Buy", "Buy", "Watch")
    if tradeable and t.get("entry_price") and t.get("target_price"):
        lines.append(f"🎯 매수가 {_lvl(t['entry_price'])}")
        tp = f" ({pct(t['target_pct'])})" if t.get("target_pct") is not None else ""
        lines.append(f"   목표가 {_lvl(t['target_price'])}{tp}")
        stop_seg = f"   손절가 {_lvl(t.get('stop_price'))}"
        if t.get("rr_ratio"):
            stop_seg += f"  손익비 {t['rr_ratio']}"
        lines.append(stop_seg)

    # 점수 구성 (item 1: 항목별 점수 투명 표시)
    sc = t.get("scores", {})
    if sc:
        lines.append(f"📊 기업가치 {_z(sc.get('fundamental'))} · 차트 {_z(sc.get('technical'))} · "
                     f"업종 {_z(sc.get('sector'))} · 시장환경 {_z(sc.get('macro'))}")
        # 점수 산출 근거 — 기여도(점수×비중) 합 → 보정 → 최종
        contrib = []
        for key, label in (("fundamental", "기업가치"), ("macro", "시장환경"),
                           ("sector", "업종"), ("technical", "차트"),
                           ("growth", "성장"), ("sentiment", "심리")):
            v = sc.get(key)
            if v is not None:
                contrib.append(f"{label} {v * _SCORE_W[key]:.0f}")
        base = t.get("base_score")
        if contrib:
            tail = (f" = 기본 {base} → 보정 → 최종 {t.get('score',0):.0f}"
                    if base else "")
            lines.append("🧮 " + " + ".join(contrib) + tail)

    # AI 점수 보정 근거 — 기본 점수 → AI 점수 (AI가 점수를 바꿨을 때만)
    adj = t.get("llm_adjustment")
    if adj:
        lines.append(f"🤖 기본 점수 {adj['mech']} → AI 보정 {t.get('score',0):.0f} "
                     f"({adj['delta']:+d})")

    # 의사결정 한 줄 (매수/관망/회피 근거)
    emo, label, reasons = _decision_reasons(t)
    if reasons:
        lines.append(f"{emo} {label}: {' · '.join(reasons)}")

    # 섹터 근거 (item 7) — 쉬운 한국어
    sd = t.get("sector_detail")
    if sd and sd.get("rating"):
        seg = f"   ↳ 업종 {_ko(_RATING_KO, sd['rating'])}"
        if sd.get("rs_1m") is not None:
            seg += f" · 상대강도 {pct(sd['rs_1m'])}"
        if sd.get("macro_fit"):
            seg += f" · {_ko(_FIT_KO, sd['macro_fit'])}"
        lines.append(seg)

    # AI 핵심 (item 9: 2문장·120자 제한)
    catalyst = t.get("key_catalyst") or t.get("summary")
    if catalyst:
        lines.append(f"💡 {_clip(catalyst)}")

    # 매수/매도 타이밍 시그널 (RSI·볼린저·MACD·일목·주봉 과열)
    buy_sig = t.get("buy_signals") or []
    sell_sig = t.get("sell_signals") or []
    if buy_sig:
        lines.append("✅ 매수 신호: " + " · ".join(buy_sig[:3]))
    if sell_sig:
        lines.append("🔺 과열/매도 신호: " + " · ".join(sell_sig[:3]))

    # AI 판단 코멘트(과열↔관망 등 연결) + 기계 리스크 항목
    rc = t.get("risk_comment")
    if rc:
        lines.append(f"⚠️ {_clip(rc)}")
    risks = t.get("risk_items") or []
    if risks:
        lines.append("   리스크: " + " · ".join(risks[:3]))

    d = t.get("days_to_earnings")
    if d is not None and 0 <= d <= 7:
        lines.append(f"📅 어닝 D-{d}")
    return lines


# ---------- [2] 진입가 도달 ----------


def format_entry_alert(symbol: str, price: float, t: dict) -> str:
    cur = t.get("currency", "USD")
    lines = [f"🎯 {symbol} 진입가 도달!", "",
             f"현재가: {money(price, cur)}",
             f"목표 진입가: {money(t.get('entry_price'), cur)}", ""]
    if t.get("target_price"):
        tp = f" ({pct(t['target_pct'])})" if t.get("target_pct") is not None else ""
        lines.append(f"목표가: {money(t['target_price'], cur)}{tp}")
    if t.get("stop_price"):
        sp = f" ({pct(t['stop_pct'])})" if t.get("stop_pct") is not None else ""
        lines.append(f"손절가: {money(t['stop_price'], cur)}{sp}")
    if t.get("rr_ratio"):
        lines.append(f"R:R: {t['rr_ratio']}")
    lines += ["", "→ 매수 고려하세요", f"[{_hm()} KST]"]
    return "\n".join(lines)


# ---------- [3] 목표가 / 손절가 도달 ----------


def _entry_diff(price: float, t: dict) -> str:
    entry = t.get("entry_price")
    if not entry:
        return ""
    return f" (진입 대비 {pct((price/entry - 1) * 100)})"


def format_target_alert(symbol: str, price: float, t: dict) -> str:
    cur = t.get("currency", "USD")
    return (f"✅ {symbol} 목표가 도달!\n"
            f"현재가: {money(price, cur)}{_entry_diff(price, t)}\n"
            f"→ 익절 고려하세요 [{_hm()} KST]")


def format_stop_alert(symbol: str, price: float, t: dict) -> str:
    cur = t.get("currency", "USD")
    return (f"🛑 {symbol} 손절가 도달!\n"
            f"현재가: {money(price, cur)}{_entry_diff(price, t)}\n"
            f"→ 손절 고려하세요 [{_hm()} KST]")


# ---------- [4] 급등락 감지 (±3%, LLM 없이) ----------


def format_surge_alert(symbol: str, price: float, change_pct: float, t: dict) -> str:
    cur = t.get("currency", "USD")
    kind = "급등" if change_pct >= 0 else "급락"
    emoji = "⚡" if change_pct >= 0 else "🔻"
    entry_state = ""
    if t.get("entry_price"):
        reached = "이미 도달" if t.get("entry_reached") else "미도달"
        entry_state = f"\n진입가: {money(t['entry_price'], cur)} ({reached})"
    return (f"{emoji} {symbol} {kind} 감지 {pct(change_pct)}\n\n"
            f"현재가: {money(price, cur)}\n"
            f"오늘 신호: {_sig_ko(t.get('signal',''))} ({t.get('score',0):.0f}점)"
            f"{entry_state}\n\n"
            f"[±5% 이상이면 LLM 긴급 분석 실행]\n[{_hm()} KST]")


# ---------- [4b] 인트라데이 변화 분석 (1시간 누적) ----------


def format_intraday_alert(symbol: str, base: dict, current: dict,
                          llm: dict | None) -> str:
    """1시간 인트라데이 스냅샷에서 '유의미한 변화' 감지 시 알림 메시지."""
    cur_ccy = base.get("currency", "USD")
    price = current.get("price")
    chg = current.get("change_pct")
    # 실시간가가 있으면 '현재가(변화율)', 없으면(뉴스 감시) '기준가'만
    if chg is not None:
        head = f"📈 {symbol} 변화 감지"
        price_line = f"현재가: {money(price, cur_ccy)} ({pct(chg)})"
    else:
        head = f"🗞 {symbol} 신규 뉴스"
        price_line = (f"기준가: {money(price, cur_ccy)}" if price is not None
                      else "")
    lines = [head, ""]
    if price_line:
        lines.append(price_line)
    lines.append(f"트리거: {', '.join(current.get('triggers', [])) or '-'}")

    # ETF 축별 한 줄(업종 우선)
    links = current.get("etf") or {}
    for axis, ko in (("industry", "업종ETF"), ("technology", "기술ETF"),
                     ("market", "시장ETF")):
        brs = links.get(axis) or []
        if brs:
            seg = ", ".join(f"{b.get('etf')} {pct(b.get('chg_1d')) if b.get('chg_1d') is not None else 'N/A'}"
                            for b in brs[:2])
            lines.append(f"{ko}: {seg}")

    if current.get("new_news"):
        lines.append("")
        lines.append("🗞 새 뉴스:")
        lines += [f'- "{h}"' for h in current["new_news"][:3]]

    if llm:
        lines.append("")
        base_sig = base.get("signal", "")
        new_sig = llm.get("signal") or base_sig
        sig_part = _sig_ko(new_sig)
        if new_sig and new_sig != base_sig:
            sig_part = f"{_sig_ko(base_sig)} → {_sig_ko(new_sig)}"
        lines.append(f"🤖 AI: {sig_part}"
                     + (f" · {llm['action']}" if llm.get("action") else ""))
        if llm.get("summary"):
            lines.append(llm["summary"])
        if llm.get("news_impact"):
            lines.append(f"뉴스영향: {llm['news_impact']}")

    lines += ["", f"[{_hm()} KST]"]
    return "\n".join(lines)


# ---------- [5] 긴급 LLM 분석 ----------

_URGENCY_KO = {"high": "높음", "medium": "중간", "low": "낮음"}


def format_emergency_alert(symbol: str, trigger: str, price: float,
                           resp: dict, prev: dict) -> str:
    lines = [f"🚨 {symbol} 긴급 분석 완료", "",
             f"트리거: {trigger}", "",
             f"원인: {resp.get('cause','원인 불명')}"]

    if resp.get("signal_change"):
        old = _sig_ko(prev.get("signal", ""))
        new = _sig_ko(resp.get("new_signal", ""))
        score_txt = ""
        if resp.get("new_score") is not None:
            score_txt = f" ({prev.get('score','?')}점 → {resp['new_score']:.0f}점)"
        lines.append("판단 변경: 있음")
        lines.append(f"  {old} → {new}{score_txt}")
    else:
        lines.append(f"판단 변경: 없음 ({_sig_ko(resp.get('new_signal',''))} 유지)")

    lines.append(f"행동: {resp.get('action','현 판단 유지')}")
    conf = _URGENCY_KO.get(resp.get("urgency", "medium"), "중간")
    lines += ["", f"[신뢰도: {conf}]", f"[{_hm()} KST]"]
    return "\n".join(lines)


# ---------- [6] 장 마감 최종 요약 ----------


def format_final_summary(daily: dict, emergency_log: dict | None = None) -> str:
    tickers = daily.get("tickers", {})
    emergency_log = emergency_log or {}

    lines = [BAR, f"📋 오늘 마감 요약 [{_md(daily.get('date'))}]", BAR]

    # 진입가 도달 / 미도달 (매수 신호 종목만)
    buy_syms = [s for s, t in tickers.items()
                if t.get("signal") in ("Strong Buy", "Buy")]
    reached = [(s, tickers[s]) for s in buy_syms if tickers[s].get("entry_reached")]
    waiting = [s for s in buy_syms if not tickers[s].get("entry_reached")]
    if reached:
        lines.append("진입가 도달: " + " · ".join(
            f"{s} ✅ {_hm_from(t.get('entry_reached_at'))}" for s, t in reached))
    if waiting:
        lines.append("미도달:      " + " · ".join(f"{s} ⏳" for s in waiting))

    # 신호 변경 (original_signal 대비)
    changes = []
    for s, t in tickers.items():
        orig = t.get("original_signal")
        if orig and orig != t.get("signal"):
            note = ""
            if s in emergency_log:
                note = f" (긴급분석 {_hm_from(emergency_log[s].get('last_called_at'))})"
            changes.append(f"  {s} {_sig_ko(orig)}→{_sig_ko(t['signal'])}{note}")
    if changes:
        lines += ["", "신호 변경:"] + changes

    # 최종 신호 (점수 내림차순)
    lines += ["", "최종 신호:"]
    for s in sorted(tickers, key=lambda x: tickers[x].get("score", 0), reverse=True):
        t = tickers[s]
        lines.append(f"  {s:6} {t.get('score',0):>3.0f}점 {t.get('grade','')} "
                     f"{_sig_ko(t.get('signal',''))}")

    # 긴급 LLM 호출
    if emergency_log:
        total = sum(e.get("call_count_today", 0) for e in emergency_log.values())
        if total:
            lines += ["", f"긴급 LLM 호출: {total}회 "
                          f"({', '.join(emergency_log.keys())})"]

    # 내일 주의 (어닝 임박)
    warns = []
    for s, t in tickers.items():
        d = t.get("days_to_earnings")
        if d is not None and 0 <= d <= 2:
            warns.append(f"{s} 어닝 발표 D-{d}")
    if warns:
        lines += ["", "⚠️ 내일 주의", "  " + " · ".join(warns[:5])]

    lines += [f"[{_hm()} KST]", BAR]
    return "\n".join(lines)
