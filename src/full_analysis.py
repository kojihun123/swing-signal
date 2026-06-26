"""1단계: 하루 1회 풀 분석 (KST 22:30, 미국장 개장 1시간 전).

흐름
  ① 거시경제 분석          → cache/macro_today.json
  ② 섹터 로테이션 분석      → cache/sector_latest.json
  ③ 종목 루프 (종목당 딜레이)
       수집 → 펀더/기술/뉴스/성장/감성/리스크 → 기계적 종합점수
       → LLM 종합 판단(정기 1회) → daily_result 누적
  ④ 텔레그램: 오늘의 분석 리포트
  ⑤ data/reports/YYYYMMDD.csv 저장

산출물: cache/daily_result.json (장중 가격 감시·긴급 분석의 기준)

실행
  python src/full_analysis.py                  # 워치리스트 전체
  python src/full_analysis.py --ticker NVDA    # 단일 종목
  python src/full_analysis.py --tickers NVDA,AAPL
"""
from __future__ import annotations

import argparse
import os
import sys
import time

from dotenv import load_dotenv

load_dotenv("/app/.env" if os.path.exists("/app/.env") else ".env")

import fundamental as fundamental_mod  # noqa: E402
import growth as growth_mod  # noqa: E402
import kr_data  # noqa: E402
import llm_analyzer  # noqa: E402
import macro as macro_mod  # noqa: E402
import market_brief  # noqa: E402
import reporter  # noqa: E402
import risk as risk_mod  # noqa: E402
import sector_llm  # noqa: E402
import scraper  # noqa: E402
import sentiment as sentiment_mod  # noqa: E402
import technical as technical_mod  # noqa: E402
from cache import save_cache  # noqa: E402
from collector import collect_all, fetch_history  # noqa: E402
from news import fetch_news  # noqa: E402
from scorer import StockResult, compute  # noqa: E402
from sector import (ROTATION_ETFS, SectorAnalyzer,  # noqa: E402
                    macro_fit, momentum_label, needed_etfs, sector_score)
from utils import load_watchlist, now_market, now_notify  # noqa: E402

STOCK_DELAY = 3.0          # 종목 간 딜레이(초) — 워치리스트 확장 대비 단축
LLM_DAILY_CAP = 30         # LLM 호출 하드 상한 (Gemini+Groq 폴백으로 여유)
LLM_TOPN = 30              # 기계점수 상위 N개만 LLM 종합판단 (비용/확장성)


def _is_kr(sym: str) -> bool:
    return sym.upper().endswith((".KS", ".KQ"))


def _watchlist_path() -> str:
    for p in ("/app/watchlist.json", "watchlist.json", "../watchlist.json"):
        if os.path.exists(p):
            return p
    return "watchlist.json"


def _num(*vals):
    """첫 번째 유효 숫자값을 반환(없으면 None)."""
    for v in vals:
        if v is None:
            continue
        try:
            f = float(v)
            if f == f:
                return f
        except (TypeError, ValueError):
            continue
    return None


def build_ticker_entry(r: StockResult) -> dict:
    """StockResult → daily_result.json 종목 엔트리."""
    llm = r.llm or {}
    lv = r.levels or {}
    cs = r.component_scores

    signal = llm.get("signal") or r.grade.get("en", "Avoid")
    score = _num(llm.get("score_adjusted"), r.final_score) or r.final_score

    entry = _num(llm.get("entry"), lv.get("entry"))
    target = _num(llm.get("target"), lv.get("target"))
    stop = _num(llm.get("stop"), lv.get("stop"))

    # 수익률·손익비 — 표시되는 최종 진입/목표/손절에서 직접 계산(정합성 보장).
    # LLM의 rr_ratio 문자열("2:1" 등 형식오류)·기계 levels의 stale pct는 무시한다.
    target_pct = (target / entry - 1) * 100 if (entry and target) else lv.get("target_pct")
    stop_pct = (stop / entry - 1) * 100 if (entry and stop) else lv.get("stop_pct")
    rr = None
    if entry and target and stop and entry > stop:
        rr = f"1:{(target - entry) / (entry - stop):.1f}"
    elif lv.get("rr"):
        rr = f"1:{lv['rr']:.1f}"

    fm = (r.fundamental or {}).get("metrics", {})
    ind = (r.technical or {}).get("indicators", {})
    pats = (r.technical or {}).get("patterns", [])

    # 52주 위치(%) = (현재가 - 52주저) / (52주고 - 52주저)
    w52h, w52l = fm.get("week52_high"), fm.get("week52_low")
    w52_pos = None
    if (w52h and w52l and w52h > w52l and r.price == r.price
            and w52l <= r.price <= w52h * 1.2):
        w52_pos = round((r.price - w52l) / (w52h - w52l) * 100)

    # 섹터 근거 (item 7): 대표 ETF 평가 + 점수 + MacroFit
    sd = (r.sector or {}).get("detail") or {}
    sector_detail = {
        "etf": (r.sector or {}).get("etf"),
        "rating": sd.get("rating"), "score": sd.get("score"),
        "rs_1m": sd.get("rs_1m"), "macro_fit": sd.get("macro_fit"),
    } if sd else None

    # 리스크 최대 3개 (item 8): 리스크 항목 + 재무 경고
    risk_items: list[str] = []
    for it in (r.risk or {}).get("items", []):
        nm = it.get("name") if isinstance(it, dict) else str(it)
        if nm:
            risk_items.append(nm)
    for w in ((r.fundamental or {}).get("risk", {}) or {}).get("warnings", []):
        if w and w not in risk_items:
            risk_items.append(w)
    risk_items = risk_items[:3]

    # 최근 뉴스 헤드라인 1건
    news0 = None
    if r.news:
        n0 = r.news[0]
        news0 = {"headline": getattr(n0, "headline", ""),
                 "age": getattr(n0, "age", ""), "url": getattr(n0, "url", "")}

    return {
        "name": r.name,
        "currency": r.currency,
        "signal": signal,
        "original_signal": signal,
        "grade": r.grade.get("stars", ""),
        "score": round(float(score)),
        "current_price": round(r.price, 4) if r.price == r.price else None,
        "change_pct": round(ind["change_pct"], 2) if ind.get("change_pct") is not None else None,
        "vol_ratio": round(ind["vol_ratio"], 1) if ind.get("vol_ratio") is not None else None,
        "w52_pos": w52_pos,
        "entry_price": round(entry, 4) if entry else None,
        "target_price": round(target, 4) if target else None,
        "stop_price": round(stop, 4) if stop else None,
        "target_pct": round(target_pct, 2) if target_pct is not None else None,
        "stop_pct": round(stop_pct, 2) if stop_pct is not None else None,
        "rr_ratio": rr,
        "entry_reached": False,
        "entry_reached_at": None,
        "target_reached": False,
        "stop_reached": False,
        "summary": llm.get("summary") or "",
        "risk_comment": llm.get("risk_comment") or "",
        "key_catalyst": llm.get("key_catalyst") or "",
        "sector_detail": sector_detail,
        "risk_items": risk_items,
        "llm_adjustment": ({"mech": round(r.mech_score),
                            "delta": round(float(score) - r.mech_score)}
                           if r.mech_score is not None
                           and abs(float(score) - r.mech_score) >= 0.5 else None),
        # 펀더멘털/밸류에이션
        "per": round(fm["per"], 1) if fm.get("per") is not None else None,
        "roe": round(fm["roe"], 1) if fm.get("roe") is not None else None,
        "target_mean": round(fm["target_mean"], 4) if fm.get("target_mean") else None,
        "upside": round(fm["upside"], 1) if fm.get("upside") is not None else None,
        "num_analysts": fm.get("num_analysts"),
        "recommendation": fm.get("recommendation"),
        # 기술적
        "rsi": round(ind["rsi_d"]) if ind.get("rsi_d") == ind.get("rsi_d") and ind.get("rsi_d") is not None else None,
        "macd_state": ind.get("macd_state"),
        "aligned": bool(ind.get("aligned")),
        "patterns": pats[:2],
        "buy_signals": ind.get("buy_signals") or [],
        "sell_signals": ind.get("sell_signals") or [],
        "ichimoku": ind.get("ichimoku_pos"),
        # 뉴스
        "news": news0,
        "days_to_earnings": fm.get("days_to_earnings"),
        "next_earnings": fm.get("next_earnings"),
        "scores": {
            "macro": cs.get("macro"),
            "sector": cs.get("sector"),
            "fundamental": cs.get("fundamental"),
            "growth": cs.get("growth"),
            "technical": cs.get("technical"),
            "sentiment": cs.get("sentiment"),
            "risk_deduction": (r.risk or {}).get("deduction", 0),
        },
        "base_score": round(r.base_score) if r.base_score else None,
    }


def run_full_analysis(tickers_override: list[str] | None = None) -> dict | None:
    try:
        entries = load_watchlist(_watchlist_path())
    except Exception as e:  # noqa: BLE001
        print(f"[오류] 워치리스트 로드 실패: {e}")
        return None
    if tickers_override:
        entries = [{"symbol": t.upper(), "profile": None,
                    "sector_etf": None, "sector_name": None}
                   for t in tickers_override]
    if not entries:
        print("[경고] 워치리스트가 비어 있습니다.")
        return None
    by_symbol = {e["symbol"]: e for e in entries}
    tickers = [e["symbol"] for e in entries]

    # ① 거시 ───────────────────────────────────────────────
    print("[1/5] 거시경제 분석 ...")
    macro = macro_mod.analyze_macro()
    regime = macro_mod.market_regime(macro)
    macro_dict = {"score": macro.score, "multiplier": macro.multiplier,
                  "label": macro.label, "available": macro.available,
                  "parts": macro.parts, "metrics": macro.metrics,
                  "regime": regime}
    print(f"      거시 환경: {macro.label} ({macro.score:.0f}점, ×{macro.multiplier})")
    print(f"      시장 레짐: {regime['summary']}")

    # ② 섹터 ───────────────────────────────────────────────
    # 수집 = GICS 로테이션 11섹터(항상, 인기 판단용) + 종목 매핑 ETF + 방어/벤치.
    # 세부 산업 ETF(IGV/QTUM/HACK 등) 중 워치리스트 미사용분만 제외.
    etf_set = needed_etfs(entries)
    extra = len(etf_set) - len(ROTATION_ETFS)
    print(f"[2/5] 섹터 로테이션: GICS 11섹터(항상) + 종목/벤치 {max(extra,0)}개 "
          f"= {len(etf_set)}개 수집 ...")
    sectors = SectorAnalyzer()
    rotation = sectors.load(etf_set)
    if rotation.top3:
        print("      유입 TOP3: " + ", ".join(
            f"{s.label} {s.chg_5d:+.1f}%" for s in rotation.top3))

    # 섹터 로테이션 LLM 분석 (하루 1회, 전 종목 공유)
    sector_rotation = None
    if llm_analyzer.llm_enabled():
        sector_rotation = sector_llm.run(sectors, macro_dict)
        if sector_rotation:
            sb = [f"{d['label']} {d['rating']}" for d in sector_rotation.values()
                  if d["rating"] in ("Strong Bullish", "Bullish")]
            print(f"      [섹터LLM] {len(sector_rotation)}개 평가 · 강세: "
                  + (", ".join(sb[:5]) or "없음"))

    # 섹터 상세 — 점수 + 근거(상대강도·추세·모멘텀·MacroFit) + LLM 평가 결합
    sector_detail = {}
    for row in sectors.rotation_table():
        etf = row["etf"]
        lr = (sector_rotation or {}).get(etf, {})
        sector_detail[etf] = {
            "label": row["label"], "score": sector_score(row),
            "rs_1m": row.get("rs_1m"), "rs_3m": row.get("rs_3m"),
            "trend": row.get("ma_alignment"),
            "momentum": momentum_label(row),
            "macro_fit": macro_fit(etf, regime),
            "rating": lr.get("rating"), "outlook": lr.get("outlook"),
            "reason": lr.get("reason"),
        }

    # 시장 컨텍스트 — 거시 레짐 + 섹터 평가를 묶은 단일 객체(종목 분석에 주입)
    market_context = {
        "regime": regime,
        "macro": {"label": macro.label, "score": macro.score,
                  "multiplier": macro.multiplier},
        "sectors": {etf: {"label": d["label"], "rating": d["rating"],
                          "outlook": d.get("outlook")}
                    for etf, d in (sector_rotation or {}).items()},
    }

    # 시장 브리핑 LLM — 오늘의 테마 + 논리일관성 + 한줄요약 (하루 1회)
    market_brief_result = None
    if llm_analyzer.llm_enabled() and sector_rotation:
        market_brief_result = market_brief.run(
            macro_dict, regime, market_context["sectors"])
        if market_brief_result:
            print("      [브리핑] 테마: "
                  + " · ".join(market_brief_result.get("themes", [])))

    # ③ 종목 ───────────────────────────────────────────────
    print(f"[3/5] 종목 {len(tickers)}개: {', '.join(tickers)}")
    try:
        spy_daily = fetch_history("SPY", period="300d", interval="1d")
    except Exception:  # noqa: BLE001
        spy_daily = None
    kospi_daily = None
    if any(_is_kr(t) for t in tickers):
        try:
            kospi_daily = fetch_history("^KS11", period="300d", interval="1d")
        except Exception:  # noqa: BLE001
            kospi_daily = None

    stock_data = collect_all(tickers)
    llm_on = llm_analyzer.llm_enabled()
    if not llm_on:
        print("      [LLM] GEMINI_API_KEY 없음 → 기계적 점수만 사용")
    llm_calls = 0

    daily_result = {
        "date": now_market().strftime("%Y-%m-%d"),
        "updated_at": now_notify().isoformat(),
        "macro": {"label": macro.label, "score": macro.score,
                  "available": macro.available, "parts": macro.parts,
                  "regime": regime, "metrics": macro.metrics},
        "sector": {
            "top3": [{"label": s.label, "etf": s.etf, "chg_5d": s.chg_5d}
                     for s in rotation.top3],
            "bottom3": [{"label": s.label, "etf": s.etf, "chg_5d": s.chg_5d}
                        for s in rotation.bottom3],
            "defensive_strong": rotation.defensive_strong,
            "rotation_llm": sector_rotation,
            "detail": sector_detail,
        },
        "market_context": market_context,
        "market_brief": market_brief_result,
        "tickers": {},
    }
    results: list[StockResult] = []

    # ── Pass 1: 전 종목 수집·분석·기계점수 (LLM 제외) ──
    for i, tk in enumerate(tickers):
        entry = by_symbol[tk]
        data = stock_data[tk]
        if not data.ok:
            print(f"      {tk}: 데이터 수집 실패 - {data.error}")
            results.append(StockResult(ticker=tk, error=data.error or "데이터 없음"))
            continue

        is_kr = _is_kr(tk)
        bench = kospi_daily if (is_kr and kospi_daily is not None) else spy_daily
        bench_label = "KOSPI" if (is_kr and kospi_daily is not None) else "SPY"

        sec = sectors.for_ticker(tk, data.info, profile=entry.get("profile"),
                                 sector_etf=entry.get("sector_etf"))
        # 대표 업종 ETF가 GICS 11섹터에 있으면 LLM 평가 + 섹터 상세를 부착
        if sec and sec.get("etf") in sector_detail:
            if sector_rotation and sec["etf"] in sector_rotation:
                sec["rotation"] = sector_rotation[sec["etf"]]
            sec["detail"] = sector_detail[sec["etf"]]
        sector_name = entry.get("sector_name") or (sec or {}).get("label")

        news = fetch_news(tk)
        try:
            save_cache(f"news/{tk}_news.json", [n.as_dict() for n in news])
        except Exception:  # noqa: BLE001
            pass

        daily_close = float(data.daily["Close"].iloc[-1])
        fresh = scraper.base_price(tk)
        cur_price = fresh if fresh else daily_close

        r = StockResult(
            ticker=tk, name=entry.get("name") or data.info.get("name") or tk,
            price=cur_price,
            currency=(data.raw or {}).get("currency") or "USD",
            bench_label=bench_label, sector_name=sector_name,
            macro=macro_dict, sector=sec, news=news,
            technical=technical_mod.analyze_technical(data.daily, data.weekly, bench),
            fundamental=fundamental_mod.analyze_fundamental(data, sector_name),
            growth=growth_mod.analyze_growth(news),
            sentiment=sentiment_mod.analyze_sentiment(news),
        )
        r.market_context = market_context
        r.risk = risk_mod.analyze_risk(data, r.fundamental, news)
        if is_kr:
            r.kr_extra = kr_data.gather(tk)
            try:
                save_cache(f"kr_extra/{tk}.json", r.kr_extra)
            except Exception:  # noqa: BLE001
                pass
        compute(r)
        results.append(r)
        print(f"      {tk}: {r.grade['stars']} {r.final_score:.0f}점 (기계점수)")
        if i < len(tickers) - 1:
            time.sleep(STOCK_DELAY)

    # ── Pass 2: 기계점수 상위 N개만 LLM 종합판단 (비용/확장성) ──
    ok = [r for r in results if not r.error]
    topn = sorted(ok, key=lambda r: r.final_score, reverse=True)[:LLM_TOPN]
    if llm_on and topn:
        print(f"      LLM 종합판단: 기계점수 상위 {len(topn)}/{len(ok)}개 "
              f"(일일 상한 {LLM_DAILY_CAP})")
        for r in topn:
            if llm_calls >= LLM_DAILY_CAP:
                print("      LLM 일일 상한 도달 → 나머지는 기계점수 유지")
                break
            r.mech_score = r.final_score      # LLM 보정 전 기계점수 보존
            r.llm = llm_analyzer.analyze(r)
            llm_calls += 1
            if r.llm:
                compute(r)  # LLM 반영해 재계산
                print(f"      {r.ticker}: {r.mech_score:.0f} → {r.final_score:.0f}점 "
                      f"({r.llm.get('signal','')})")

    # ── daily_result 엔트리(전 종목) 구성 + 저장 ──
    for r in ok:
        daily_result["tickers"][r.ticker] = build_ticker_entry(r)
    save_cache("daily_result.json", daily_result)
    print(f"      💾 daily_result.json 저장 ({len(daily_result['tickers'])}개 종목)")

    # ④ 텔레그램 리포트 ────────────────────────────────────
    print("[4/5] 오늘의 분석 리포트 ...")
    report = reporter.format_daily_report(daily_result)
    print("\n" + report + "\n")
    if reporter.telegram_enabled():
        reporter.send_telegram_message(report)
        print("      📨 텔레그램 전송 완료")
    else:
        print("      [텔레그램] 토큰/채팅ID 없음 → 전송 skip")

    # ⑤ CSV 저장 ──────────────────────────────────────────
    print("[5/5] CSV 저장 ...")
    reporter.save_reports(results)
    if llm_on:
        print(f"      LLM 호출: {llm_calls}회 (한도 {LLM_DAILY_CAP})")
    return daily_result


def main() -> int:
    p = argparse.ArgumentParser(description="하루 1회 풀 분석")
    p.add_argument("--ticker", type=str, default=None, help="단일 종목")
    p.add_argument("--tickers", type=str, default=None,
                   help="임시 종목 (쉼표구분, 예: NVDA,AAPL)")
    args = p.parse_args()

    override = None
    if args.ticker:
        override = [args.ticker.strip()]
    elif args.tickers:
        override = [t.strip() for t in args.tickers.split(",") if t.strip()]

    run_full_analysis(override)
    return 0


if __name__ == "__main__":
    sys.exit(main())
