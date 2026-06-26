"""2단계: 장중 가격 감시 (30분마다, 평상시 LLM 없음).

흐름 (미국 전 세션 동작: 데이마켓/프리/정규/애프터)
  · KIS(데이마켓 포함)·Finviz/yfinance로 현재가·거래량만 빠르게 체크
  · 진입가/목표가/손절가 도달 여부 → 즉시 알림
  · ±3% 급등락 감지 → 즉시 알림 (LLM 없음)
  · 긴급 LLM 트리거(±5% or 거래량 3배+±2%) 충족 시 emergency_analyzer 호출

변경 사항은 daily_result.json에 다시 저장한다(current_price·도달 플래그).

단독 실행(테스트):
  python src/price_monitor.py            # 장중이면 1회 감시
  python src/price_monitor.py --force    # 장 시간 무시하고 강제 1회
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime

from dotenv import load_dotenv

load_dotenv("/app/.env" if os.path.exists("/app/.env") else ".env")

import emergency_analyzer  # noqa: E402
import reporter  # noqa: E402
import scraper  # noqa: E402
from cache import load_data, save_cache  # noqa: E402
from utils import (SESSION_KO, current_session, is_session_open,  # noqa: E402
                   now_notify)

# 긴급 LLM 재호출 금지 간격(초)
EMERGENCY_COOLDOWN = 3600
# 급등락 즉시 알림 임계치(%)
SURGE_PCT = 3.0


def _notify(msg: str) -> None:
    print("\n" + msg)
    if reporter.telegram_enabled():
        reporter.send_telegram_message(msg)


def check_emergency_trigger(symbol: str, change_pct: float,
                            volume_ratio: float | None) -> str | None:
    """긴급 LLM 호출 조건 체크. 트리거 문자열 또는 None."""
    # 1시간 이내 재호출 금지
    log = load_data("emergency_log.json") or {}
    if symbol in log:
        try:
            last = datetime.fromisoformat(log[symbol]["last_called_at"])
            now = now_notify()
            if last.tzinfo is None:
                last = last.replace(tzinfo=now.tzinfo)
            if (now - last).total_seconds() < EMERGENCY_COOLDOWN:
                return None
        except (KeyError, ValueError, TypeError):
            pass

    # 조건 A: 주가 ±5% 이상
    if abs(change_pct) >= 5.0:
        return f"주가 {change_pct:+.1f}%"
    # 조건 B: 거래량 3배 이상 + 주가 ±2% 이상 동시
    if volume_ratio is not None and volume_ratio >= 3.0 and abs(change_pct) >= 2.0:
        return f"거래량 {volume_ratio:.1f}배 + 주가 {change_pct:+.1f}%"
    return None


def run_price_monitor(force: bool = False) -> None:
    session = current_session()
    if not force and not is_session_open():
        print(f"[감시] 미국 휴장 → skip ({now_notify():%H:%M KST})")
        return

    daily = load_data("daily_result.json")
    if not daily or not daily.get("tickers"):
        print("[감시] daily_result.json 없음 → 풀 분석 먼저 필요")
        return

    sess_ko = SESSION_KO.get(session, session)
    print(f"[감시] {len(daily['tickers'])}개 종목 점검 "
          f"[{sess_ko}] ({now_notify():%H:%M KST})")
    changed = False

    for symbol, result in daily["tickers"].items():
        price, volume, avg_volume = scraper.get_realtime(symbol)
        if price is None:
            print(f"      {symbol}: 시세 수집 실패 → skip")
            continue

        prev_price = result.get("current_price") or price
        change_pct = (price - prev_price) / prev_price * 100 if prev_price else 0.0
        volume_ratio = (volume / avg_volume) if (volume and avg_volume) else None
        signal = result.get("signal", "")
        print(f"      {symbol}: {price:.2f} ({change_pct:+.2f}%) "
              f"거래량 {volume_ratio:.1f}배" if volume_ratio
              else f"      {symbol}: {price:.2f} ({change_pct:+.2f}%)")

        # ① 진입가 도달 (매수 신호 종목)
        if signal in ("Strong Buy", "Buy") and not result.get("entry_reached"):
            entry = result.get("entry_price")
            if entry and price <= entry * 1.005:
                result["entry_reached"] = True
                result["entry_reached_at"] = now_notify().isoformat()
                _notify(reporter.format_entry_alert(symbol, price, result))
                changed = True

        # ② 목표가/손절가 도달 (진입 완료 종목)
        if result.get("entry_reached"):
            target = result.get("target_price")
            stop = result.get("stop_price")
            if target and not result.get("target_reached") and price >= target:
                result["target_reached"] = True
                _notify(reporter.format_target_alert(symbol, price, result))
                changed = True
            if stop and not result.get("stop_reached") and price <= stop:
                result["stop_reached"] = True
                _notify(reporter.format_stop_alert(symbol, price, result))
                changed = True

        # ③ 급등락 알림 (±3%, LLM 없이)
        if abs(change_pct) >= SURGE_PCT:
            _notify(reporter.format_surge_alert(symbol, price, change_pct, result))

        # ④ 긴급 LLM 트리거
        trigger = check_emergency_trigger(symbol, change_pct, volume_ratio)
        if trigger:
            emergency_analyzer.run(symbol, trigger, price, daily)
            changed = True

        # 현재가 업데이트
        result["current_price"] = round(price, 4)
        changed = True

    if changed:
        daily["updated_at"] = now_notify().isoformat()
        save_cache("daily_result.json", daily)


def main() -> int:
    p = argparse.ArgumentParser(description="장중 가격 감시 1회")
    p.add_argument("--force", action="store_true",
                   help="장 시간 무시하고 강제 실행")
    args = p.parse_args()
    run_price_monitor(force=args.force)
    return 0


if __name__ == "__main__":
    sys.exit(main())
