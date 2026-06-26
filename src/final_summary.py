"""4단계: 장 마감 최종 요약 (KST 06:10, 하루 1회).

daily_result.json + emergency_log.json + 인트라데이 시계열을 읽어 하루를 요약.
  · 진입가 도달 / 미도달
  · 신호 변경 내역 (긴급분석 시각 포함)
  · 최종 신호 (점수순)
  · 긴급 LLM 호출 횟수
  · 오늘의 인트라데이 흐름(시간별 변화 누적 종합)
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
from utils import trading_day_key  # noqa: E402

INTRADAY_DIR = os.environ.get("INTRADAY_DIR", "data/intraday")


def _intraday_digest(daily: dict) -> str:
    """오늘 인트라데이 시계열을 종목별 한 줄(+AI 요약)로 종합."""
    day = trading_day_key()
    lines: list[str] = []
    for symbol in daily.get("tickers", {}):
        store = load_data(f"{day}/{symbol}.json", cache_dir=INTRADAY_DIR)
        snaps = (store or {}).get("snapshots") or []
        triggered = [s for s in snaps if s.get("triggers")]
        if not triggered:
            continue
        first, last = snaps[0], snaps[-1]
        path = f"{first.get('price')}→{last.get('price')}"
        ai = next((s["llm"] for s in reversed(snaps) if s.get("llm")), None)
        line = f"• {symbol}: {path} · 변화 {len(triggered)}회"
        if ai and ai.get("action"):
            line += f" · AI {ai['action']}"
        lines.append(line)
        if ai and ai.get("summary"):
            lines.append(f"    {ai['summary']}")
    if not lines:
        return ""
    return "📊 오늘의 인트라데이\n" + "\n".join(lines)


def run_final_summary() -> None:
    daily = load_data("daily_result.json")
    if not daily or not daily.get("tickers"):
        print("[마감] daily_result.json 없음 → 요약 불가")
        return
    emergency_log = load_data("emergency_log.json") or {}

    msg = reporter.format_final_summary(daily, emergency_log)
    digest = _intraday_digest(daily)
    if digest:
        msg += "\n\n" + digest
    print("\n" + msg + "\n")
    if reporter.telegram_enabled():
        reporter.send_telegram_message(msg)
        print("[마감] 📨 텔레그램 전송 완료")
    else:
        print("[마감] [텔레그램] 토큰/채팅ID 없음 → 전송 skip")


if __name__ == "__main__":
    run_final_summary()
    sys.exit(0)
