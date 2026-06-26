"""인트라데이 뉴스 감시 (1시간마다, 미국 세션 중).

스윙 트레이딩 관점에서 장중 1시간 단위 가격·섹터 갱신은 노이즈라 제거하고,
'판을 뒤집을 수 있는 신규 뉴스'만 감시한다. 종목별로 새 뉴스가 뜨면 그때만
LLM(인트라데이 분석)을 호출해 아침 베이스라인 대비 변화를 판단·알림한다.

  · 신규 뉴스 감지(네이버/Finviz, 중복 제거)
  · 신규 뉴스 시에만 LLM 분석 + 텔레그램 알림

제거됨(일일 풀분석이 담당): 실시간 가격 크롤링, 섹터 ETF 갱신, AI 섹터 재평가,
진입/목표/손절·급등락 가격 알림.

실행
  python src/intraday.py            # 1회 (장중에만)
  python src/intraday.py --force    # 장 시간 무시 강제 실행
"""
from __future__ import annotations

import argparse
import os
import sys

from dotenv import load_dotenv

load_dotenv("/app/.env" if os.path.exists("/app/.env") else ".env")

import llm_analyzer  # noqa: E402
import reporter  # noqa: E402
from cache import load_data, save_cache  # noqa: E402
from news import fetch_news  # noqa: E402
from utils import (SESSION_KO, current_session, is_session_open,  # noqa: E402
                   now_notify, trading_day_key)

INTRADAY_DIR = os.environ.get("INTRADAY_DIR", "data/intraday")


def _notify(msg: str) -> None:
    print("\n" + msg)
    if reporter.telegram_enabled():
        reporter.send_telegram_message(msg)


def _news_key(item) -> str:
    return (getattr(item, "url", "") or getattr(item, "headline", "")).strip()


def run_intraday(force: bool = False) -> None:
    session = current_session()
    if not force and not is_session_open():
        print(f"[뉴스감시] 미국 휴장 → skip ({now_notify():%H:%M KST})")
        return

    daily = load_data("daily_result.json")
    if not daily or not daily.get("tickers"):
        print("[뉴스감시] daily_result.json 없음 → 풀 분석 먼저 필요")
        return

    day = trading_day_key()
    sess_ko = SESSION_KO.get(session, session)
    print(f"[뉴스감시] {len(daily['tickers'])}개 · 거래일 {day} "
          f"[{sess_ko}] ({now_notify():%H:%M KST})")
    llm_on = llm_analyzer.llm_enabled()
    alerts: list[str] = []

    for symbol, base in daily["tickers"].items():
        store = load_data(f"{day}/{symbol}.json", cache_dir=INTRADAY_DIR) or {
            "ticker": symbol, "trading_day": day, "snapshots": [], "seen_news": []}

        # 신규 뉴스 감지(중복 제거)
        try:
            news = fetch_news(symbol)
        except Exception:  # noqa: BLE001
            news = []
        seen = set(store.get("seen_news", []))
        new_news = [n.headline for n in news if _news_key(n) not in seen]
        store["seen_news"] = list(seen | {_news_key(n) for n in news})[-300:]

        if not new_news:
            save_cache(f"{day}/{symbol}.json", store, cache_dir=INTRADAY_DIR)
            print(f"      {symbol}: 신규 뉴스 없음")
            continue

        # 신규 뉴스 → LLM 분석(아침 기준가를 정적 컨텍스트로, 크롤 없음)
        print(f"      {symbol}: 신규뉴스 {len(new_news)}건 → 분석")
        current = {
            "session": session, "price": base.get("current_price"),
            "change_pct": None, "vol_ratio": None, "etf": None,
            "new_news": new_news, "triggers": [f"신규뉴스 {len(new_news)}건"],
        }
        llm = (llm_analyzer.analyze_intraday(symbol, base, store["snapshots"], current)
               if llm_on else None)

        store["snapshots"].append({
            "t": now_notify().isoformat(), "session": session,
            "new_news": new_news, "triggers": current["triggers"], "llm": llm,
        })
        save_cache(f"{day}/{symbol}.json", store, cache_dir=INTRADAY_DIR)
        alerts.append(reporter.format_intraday_alert(symbol, base, current, llm))

    if alerts:
        _notify("\n\n━━━━━━━━━━\n\n".join(alerts))
    else:
        print("[뉴스감시] 신규 뉴스 없음 → 알림 생략")


def main() -> int:
    p = argparse.ArgumentParser(description="인트라데이 뉴스 감시 1회")
    p.add_argument("--force", action="store_true", help="장 시간 무시하고 강제 실행")
    args = p.parse_args()
    run_intraday(force=args.force)
    return 0


if __name__ == "__main__":
    sys.exit(main())
