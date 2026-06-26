"""4단계: 장 마감 최종 요약 (KST 06:10, 하루 1회).

daily_result.json + emergency_log.json을 읽어 오늘 하루를 요약한다.
  · 진입가 도달 / 미도달
  · 신호 변경 내역 (긴급분석 시각 포함)
  · 최종 신호 (점수순)
  · 긴급 LLM 호출 횟수
  · 내일 주의 (어닝 임박 등)

단독 실행:
  python src/final_summary.py
"""
from __future__ import annotations

import os
import sys

from dotenv import load_dotenv

load_dotenv("/app/.env" if os.path.exists("/app/.env") else ".env")

import reporter  # noqa: E402
from cache import load_data  # noqa: E402


def run_final_summary() -> None:
    daily = load_data("daily_result.json")
    if not daily or not daily.get("tickers"):
        print("[마감] daily_result.json 없음 → 요약 불가")
        return
    emergency_log = load_data("emergency_log.json") or {}

    msg = reporter.format_final_summary(daily, emergency_log)
    print("\n" + msg + "\n")
    if reporter.telegram_enabled():
        reporter.send_telegram_message(msg)
        print("[마감] 📨 텔레그램 전송 완료")
    else:
        print("[마감] [텔레그램] 토큰/채팅ID 없음 → 전송 skip")


if __name__ == "__main__":
    run_final_summary()
    sys.exit(0)
