"""텔레그램 메시지 포맷·전송 + CSV 저장 (별점 등급 기반).

메시지 1: 거시경제 브리핑 (하루 1회)
메시지 2: 종목 분석 (종목마다)
메시지 3: 변화 알림 (변화 감지 시만)
메시지 4: 최종 요약 (마지막)
"""
from __future__ import annotations

import csv
import os
import time

import requests

from scorer import StockResult
from utils import fmt_kst, money, now_notify, pct, safe_num, today_stamp

BAR = "━" * 20
SUB = "-" * 40

# 영문 신호 → 한글 표기
SIGNAL_KO = {
    "Strong Buy": "적극매수", "Buy": "매수", "Watch": "관망",
    "Neutral": "중립", "Avoid": "회피",
}


def _sig_ko(signal: str) -> str:
    return SIGNAL_KO.get(signal, signal or "중립")


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
        if m.get("fedfunds") is not None:
            lines.append(f"💰 금리: {_s(m.get('fedfunds'),2)}% · "
                         f"10년물 {_s(m.get('y10'),2)}% ({m.get('y10_trend','')})")
        if m.get("cpi_yoy") is not None:
            lines.append(f"📈 물가: CPI {_s(m.get('cpi_yoy'),1)}% · "
                         f"PCE {_s(m.get('pce_yoy'),1)}%")
        emp = f"👷 고용: 실업률 {_s(m.get('unrate'),1)}%"
        if m.get("nfp_change") is not None:
            emp += f" · NFP {m['nfp_change']/10:+.0f}만"
        lines.append(emp)
    senti = []
    if m.get("vix") is not None:
        senti.append(f"VIX {_s(m.get('vix'),1)}")
    if m.get("fg_score") is not None:
        senti.append(f"F&G {_s(m.get('fg_score'),0)}({m.get('fg_label','')})")
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


def send_telegram_message(text: str, retries: int = 2) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return False
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


def format_daily_report(daily: dict) -> str:
    macro = daily.get("macro", {})
    sector = daily.get("sector", {})
    mm = macro.get("metrics", {})

    lines = [BAR, f"📊 오늘의 분석 리포트 [{_md(daily.get('date'))}]", BAR]

    # 거시
    if macro.get("available"):
        lines.append(f"🌍 거시 환경: {macro.get('label','측정불가')} "
                     f"({macro.get('score',0):.0f}점)")
    else:
        lines.append("🌍 거시 환경: 측정불가 (FRED 키 미설정)")
    macro_bits = []
    if mm.get("fedfunds") is not None:
        macro_bits.append(f"금리 {safe_num(mm.get('fedfunds'),2)}%")
    if mm.get("cpi_yoy") is not None:
        macro_bits.append(f"CPI {safe_num(mm.get('cpi_yoy'),1)}%")
    if mm.get("vix") is not None:
        macro_bits.append(f"VIX {safe_num(mm.get('vix'),1)}")
    if mm.get("fg_score") is not None:
        macro_bits.append(f"F&G {safe_num(mm.get('fg_score'),0)}")
    if macro_bits:
        lines.append("💰 " + " · ".join(macro_bits))

    # 섹터
    top3 = sector.get("top3", [])
    bot3 = sector.get("bottom3", [])
    if top3:
        lines.append("")
        lines.append("🔥 강한 섹터: " + " · ".join(
            f"{s['label']} {pct(s['chg_5d'])}" for s in top3 if s.get("chg_5d") is not None))
    if bot3:
        lines.append("❄️ 약한 섹터: " + " · ".join(
            f"{s['label']} {pct(s['chg_5d'])}" for s in bot3 if s.get("chg_5d") is not None))
    if sector.get("defensive_strong"):
        lines.append("⚠️ 방어섹터 강세 → 시장 위험 신호")

    # 종목 (점수 내림차순)
    tickers = daily.get("tickers", {})
    for sym in sorted(tickers, key=lambda s: tickers[s].get("score", 0), reverse=True):
        t = tickers[sym]
        lines.append(SUB)
        lines.append(f"{t.get('grade','')} {sym} · {t.get('score',0):.0f}점 "
                     f"→ {_sig_ko(t.get('signal',''))}")
        cur = t.get("currency", "USD")
        if t.get("current_price") is not None:
            lines.append(f"현재가  {money(t['current_price'], cur)}")
        if t.get("entry_price"):
            lines.append(f"진입가  {money(t['entry_price'], cur)}")
        if t.get("target_price"):
            tp = f" ({pct(t['target_pct'])})" if t.get("target_pct") is not None else ""
            lines.append(f"목표가  {money(t['target_price'], cur)}{tp}")
        if t.get("stop_price"):
            sp = f" ({pct(t['stop_pct'])})" if t.get("stop_pct") is not None else ""
            lines.append(f"손절가  {money(t['stop_price'], cur)}{sp}")
        if t.get("rr_ratio"):
            lines.append(f"R:R    {t['rr_ratio']}")
        catalyst = t.get("key_catalyst") or t.get("summary")
        if catalyst:
            lines.append(f"핵심: {catalyst}")
        if t.get("risk_comment"):
            lines.append(f"⚠️ {t['risk_comment']}")
        d = t.get("days_to_earnings")
        if d is not None and 0 <= d <= 3:
            lines.append(f"⚠️ 어닝 발표 D-{d} 주의")

    lines.append(BAR)
    return "\n".join(lines)


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
