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
import llm_analyzer  # noqa: E402
import macro as macro_mod  # noqa: E402
import reporter  # noqa: E402
import risk as risk_mod  # noqa: E402
import scraper  # noqa: E402
import sentiment as sentiment_mod  # noqa: E402
import technical as technical_mod  # noqa: E402
from cache import save_cache  # noqa: E402
from collector import collect_all, fetch_history  # noqa: E402
from news import fetch_news  # noqa: E402
from scorer import StockResult, compute  # noqa: E402
from sector import SECTOR_ETFS, SectorAnalyzer  # noqa: E402
from utils import load_watchlist, now_market, now_notify  # noqa: E402

STOCK_DELAY = 5.0          # 종목 간 딜레이(초)
LLM_DAILY_CAP = 18         # 무료 한도(20/일) 보호


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

    rr = llm.get("rr_ratio") or None
    if not rr and lv.get("rr"):
        rr = f"1:{lv['rr']:.1f}"

    target_pct = lv.get("target_pct")
    stop_pct = lv.get("stop_pct")
    if target_pct is None and entry and target:
        target_pct = (target / entry - 1) * 100
    if stop_pct is None and entry and stop:
        stop_pct = (stop / entry - 1) * 100

    fm = (r.fundamental or {}).get("metrics", {})
    return {
        "name": r.name,
        "currency": r.currency,
        "signal": signal,
        "original_signal": signal,
        "grade": r.grade.get("stars", ""),
        "score": round(float(score)),
        "current_price": round(r.price, 4) if r.price == r.price else None,
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
    }


def run_full_analysis(tickers_override: list[str] | None = None) -> dict | None:
    try:
        entries = load_watchlist(_watchlist_path())
    except Exception as e:  # noqa: BLE001
        print(f"[오류] 워치리스트 로드 실패: {e}")
        return None
    if tickers_override:
        entries = [{"symbol": t.upper(), "sector_etf": None, "sector_name": None}
                   for t in tickers_override]
    if not entries:
        print("[경고] 워치리스트가 비어 있습니다.")
        return None
    by_symbol = {e["symbol"]: e for e in entries}
    tickers = [e["symbol"] for e in entries]

    # ① 거시 ───────────────────────────────────────────────
    print("[1/5] 거시경제 분석 ...")
    macro = macro_mod.analyze_macro()
    macro_dict = {"score": macro.score, "multiplier": macro.multiplier,
                  "label": macro.label, "available": macro.available,
                  "metrics": macro.metrics}
    print(f"      거시 환경: {macro.label} ({macro.score:.0f}점, ×{macro.multiplier})")

    # ② 섹터 ───────────────────────────────────────────────
    print(f"[2/5] 섹터 로테이션 ({len(SECTOR_ETFS)}개 ETF) ...")
    sectors = SectorAnalyzer()
    rotation = sectors.load()
    if rotation.top3:
        print("      유입 TOP3: " + ", ".join(
            f"{s.label} {s.chg_5d:+.1f}%" for s in rotation.top3))

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
                  "available": macro.available, "metrics": macro.metrics},
        "sector": {
            "top3": [{"label": s.label, "etf": s.etf, "chg_5d": s.chg_5d}
                     for s in rotation.top3],
            "bottom3": [{"label": s.label, "etf": s.etf, "chg_5d": s.chg_5d}
                        for s in rotation.bottom3],
            "defensive_strong": rotation.defensive_strong,
        },
        "tickers": {},
    }
    results: list[StockResult] = []

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

        sec = sectors.for_ticker(tk, data.info, entry.get("sector_etf"))
        sector_name = entry.get("sector_name") or (sec or {}).get("label")

        news = fetch_news(tk)
        try:
            save_cache(f"news/{tk}_news.json", [n.as_dict() for n in news])
        except Exception:  # noqa: BLE001
            pass

        # 풀 분석 기준가(장 시작가)는 파이낸셜 API(Finnhub→yfinance) 가격을 사용.
        # 장중 실시간 감시(price_monitor)만 크롤(CNBC) 가격을 쓴다.
        daily_close = float(data.daily["Close"].iloc[-1])
        fresh = scraper.base_price(tk)
        cur_price = fresh if fresh else daily_close

        r = StockResult(
            ticker=tk, name=data.info.get("name") or tk,
            price=cur_price,
            currency=(data.raw or {}).get("currency") or "USD",
            bench_label=bench_label, sector_name=sector_name,
            macro=macro_dict, sector=sec, news=news,
            technical=technical_mod.analyze_technical(data.daily, data.weekly, bench),
            fundamental=fundamental_mod.analyze_fundamental(data, sector_name),
            growth=growth_mod.analyze_growth(news),
            sentiment=sentiment_mod.analyze_sentiment(news),
        )
        r.risk = risk_mod.analyze_risk(data, r.fundamental, news)
        compute(r)

        # 정기 LLM 종합 판단 (종목당 1회)
        if llm_on and llm_calls < LLM_DAILY_CAP:
            r.llm = llm_analyzer.analyze(r)
            llm_calls += 1
            if r.llm:
                compute(r)  # LLM growth_score 등 반영해 재계산

        results.append(r)
        daily_result["tickers"][tk] = build_ticker_entry(r)
        print(f"      {tk}: {r.grade['stars']} {r.final_score:.0f}점 "
              f"→ {daily_result['tickers'][tk]['signal']}")

        if i < len(tickers) - 1:
            time.sleep(STOCK_DELAY)

    # daily_result 저장 (핵심 산출물)
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
