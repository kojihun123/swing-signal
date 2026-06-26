"""진입점 + 스케줄러 (4단계 운영, 캐시 기반).

스케줄 (컨테이너 TZ=Asia/Seoul 기준)
  · KST 22:30  풀 분석        (미국장 개장 1시간 전, 하루 1회)
  · 30분마다   장중 가격 감시  (미국 전 세션: 데이마켓/프리/정규/애프터)
  · KST 06:10  장 마감 요약    (하루 1회)

실행
  python src/main.py --schedule    # 스케줄러 (기본, Docker CMD)
  python src/main.py --full        # 풀 분석 1회
  python src/main.py --monitor     # 가격 감시 1회 (--force로 장시간 무시)
  python src/main.py --summary     # 마감 요약 1회
"""
from __future__ import annotations

import argparse
import os
import sys
import time

from dotenv import load_dotenv

load_dotenv("/app/.env" if os.path.exists("/app/.env") else ".env")

import schedule  # noqa: E402

from final_summary import run_final_summary  # noqa: E402
from full_analysis import run_full_analysis  # noqa: E402
from price_monitor import run_price_monitor  # noqa: E402
from utils import now_market, now_notify  # noqa: E402

FULL_AT = "22:30"      # KST 풀 분석
SUMMARY_AT = "06:10"   # KST 마감 요약
MONITOR_EVERY = 30     # 분


def _full_job() -> None:
    print(f"\n[스케줄러] 풀 분석 시작: {now_notify():%Y-%m-%d %H:%M KST}")
    try:
        run_full_analysis()
    except Exception as e:  # noqa: BLE001
        print(f"[스케줄러] 풀 분석 오류: {e}")


def _monitor_job() -> None:
    try:
        run_price_monitor()
    except Exception as e:  # noqa: BLE001
        print(f"[스케줄러] 가격 감시 오류: {e}")


def _summary_job() -> None:
    print(f"\n[스케줄러] 마감 요약 시작: {now_notify():%Y-%m-%d %H:%M KST}")
    try:
        run_final_summary()
    except Exception as e:  # noqa: BLE001
        print(f"[스케줄러] 마감 요약 오류: {e}")


def run_scheduler() -> None:
    print("[스케줄러] 가동")
    print(f"  · 풀 분석   매일 KST {FULL_AT}")
    print(f"  · 가격 감시 {MONITOR_EVERY}분마다 (미국 전 세션: 데이마켓/프리/정규/애프터)")
    print(f"  · 마감 요약 매일 KST {SUMMARY_AT}")
    print(f"  현재: {now_notify():%Y-%m-%d %H:%M KST} / ET {now_market():%H:%M}")

    schedule.every().day.at(FULL_AT).do(_full_job)
    schedule.every(MONITOR_EVERY).minutes.do(_monitor_job)
    schedule.every().day.at(SUMMARY_AT).do(_summary_job)

    while True:
        schedule.run_pending()
        time.sleep(20)


def main() -> int:
    p = argparse.ArgumentParser(description="미국 주식 스윙 트레이딩 AI 분석")
    p.add_argument("--schedule", action="store_true", help="스케줄러 모드")
    p.add_argument("--full", action="store_true", help="풀 분석 1회")
    p.add_argument("--monitor", action="store_true", help="가격 감시 1회")
    p.add_argument("--summary", action="store_true", help="마감 요약 1회")
    p.add_argument("--force", action="store_true", help="감시 시 장시간 무시")
    args = p.parse_args()

    if args.full:
        run_full_analysis()
    elif args.monitor:
        run_price_monitor(force=args.force)
    elif args.summary:
        run_final_summary()
    else:
        run_scheduler()
    return 0


if __name__ == "__main__":
    sys.exit(main())
