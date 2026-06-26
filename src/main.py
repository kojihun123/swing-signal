"""진입점 + 스케줄러 (4단계 운영, 캐시 기반).

스케줄 (ET 앵커 → 미국 서머타임 자동 추종)
  · ET 08:30  풀 분석        (정규장 개장 09:30의 1시간 전, 하루 1회)
  · 1시간마다 뉴스 감시       (미국 전 세션. 신규 뉴스 시에만 LLM 분석)
  · ET 17:00  장 마감 요약    (정규장 마감 16:00의 1시간 후, 하루 1회)

ET 기준으로 잡으면 KST 환산값이 여름(개장=22:30 KST)·겨울(개장=23:30 KST)에
1시간 달라져도 항상 개장 1시간 전/마감 1시간 후에 정확히 실행된다.

실행
  python src/main.py --schedule    # 스케줄러 (기본, Docker CMD)
  python src/main.py --full        # 풀 분석 1회
  python src/main.py --intraday    # 인트라데이 1회 (--force로 장시간 무시)
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
from intraday import run_intraday  # noqa: E402
from utils import now_market, now_notify  # noqa: E402

MARKET_TZ = "America/New_York"   # ET — 서머타임 자동 처리
FULL_AT = "08:30"       # ET, 정규장 개장(09:30) 1시간 전
SUMMARY_AT = "17:00"    # ET, 정규장 마감(16:00) 1시간 후
INTRADAY_EVERY = 60     # 분 (기존 30분 가격감시를 흡수)


def _full_job() -> None:
    print(f"\n[스케줄러] 풀 분석 시작: {now_notify():%Y-%m-%d %H:%M KST}")
    try:
        run_full_analysis()
    except Exception as e:  # noqa: BLE001
        print(f"[스케줄러] 풀 분석 오류: {e}")


def _intraday_job() -> None:
    try:
        run_intraday()
    except Exception as e:  # noqa: BLE001
        print(f"[스케줄러] 인트라데이 오류: {e}")


def _summary_job() -> None:
    print(f"\n[스케줄러] 마감 요약 시작: {now_notify():%Y-%m-%d %H:%M KST}")
    try:
        run_final_summary()
    except Exception as e:  # noqa: BLE001
        print(f"[스케줄러] 마감 요약 오류: {e}")


def run_scheduler() -> None:
    print("[스케줄러] 가동 (스케줄은 ET 기준 → 서머타임 자동 추종)")
    print(f"  · 풀 분석     매일 ET {FULL_AT} (개장 1시간 전)")
    print(f"  · 인트라데이  {INTRADAY_EVERY}분마다 (미국 전 세션: 데이마켓/프리/정규/애프터)")
    print(f"  · 마감 요약   매일 ET {SUMMARY_AT} (마감 1시간 후)")
    print(f"  현재: {now_notify():%Y-%m-%d %H:%M KST} / ET {now_market():%H:%M}")

    schedule.every().day.at(FULL_AT, MARKET_TZ).do(_full_job)
    schedule.every(INTRADAY_EVERY).minutes.do(_intraday_job)
    schedule.every().day.at(SUMMARY_AT, MARKET_TZ).do(_summary_job)

    while True:
        schedule.run_pending()
        time.sleep(20)


def main() -> int:
    p = argparse.ArgumentParser(description="미국 주식 스윙 트레이딩 AI 분석")
    p.add_argument("--schedule", action="store_true", help="스케줄러 모드")
    p.add_argument("--full", action="store_true", help="풀 분석 1회")
    p.add_argument("--intraday", "--monitor", dest="intraday",
                   action="store_true", help="인트라데이 분석 1회")
    p.add_argument("--summary", action="store_true", help="마감 요약 1회")
    p.add_argument("--force", action="store_true", help="감시 시 장시간 무시")
    args = p.parse_args()

    if args.full:
        run_full_analysis()
    elif args.intraday:
        run_intraday(force=args.force)
    elif args.summary:
        run_final_summary()
    else:
        run_scheduler()
    return 0


if __name__ == "__main__":
    sys.exit(main())
