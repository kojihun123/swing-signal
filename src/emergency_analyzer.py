"""3단계: 장중 긴급 LLM 분석 (트리거 조건 충족 시에만).

price_monitor가 급등락/거래량 폭발을 감지하면 호출한다.
최신 뉴스를 긁어 "원인이 뭔지, 아침 판단을 유지해도 되는지"를 LLM에 물어
즉시 텔레그램으로 알린다. 신호 변경 시 daily_result.json을 갱신한다.

같은 종목은 1시간 이내 재호출 금지(emergency_log.json).

단독 실행(테스트):
  python src/emergency_analyzer.py --ticker NVDA --trigger "주가 +5.8%"
"""
from __future__ import annotations

import argparse
import os
import sys

from dotenv import load_dotenv

load_dotenv("/app/.env" if os.path.exists("/app/.env") else ".env")

import llm_analyzer  # noqa: E402
import news  # noqa: E402
import reporter  # noqa: E402
from cache import load_data, save_cache  # noqa: E402
from utils import money, now_notify, safe_num  # noqa: E402

VALID_SIGNALS = ("Strong Buy", "Buy", "Watch", "Neutral", "Avoid")


def _emergency_log() -> dict:
    return load_data("emergency_log.json") or {}


def update_emergency_log(symbol: str, trigger: str) -> None:
    """긴급 호출 시각·횟수·트리거 기록 (오늘 호출 횟수 누적)."""
    log = _emergency_log()
    today = now_notify().strftime("%Y-%m-%d")
    prev = log.get(symbol, {})
    count = prev.get("call_count_today", 0)
    # 날짜가 바뀌면 카운트 리셋
    if prev.get("date") != today:
        count = 0
    log[symbol] = {
        "date": today,
        "last_called_at": now_notify().isoformat(),
        "call_count_today": count + 1,
        "last_trigger": trigger,
    }
    save_cache("emergency_log.json", log)


def _build_prompt(symbol: str, trigger: str, current_price: float,
                  prev: dict, fresh_news: str) -> str:
    macro = load_data("macro_today.json") or {}
    fund = load_data(f"fundamentals/{symbol}.json") or {}
    fm = fund.get("metrics", {})

    macro_line = (f"{macro.get('label', '측정불가')} ({safe_num(macro.get('score'), 0)}점)")
    fund_line = (
        f"PER {safe_num(fm.get('per'), 1)}, ROE {safe_num(fm.get('roe'), 0)}%, "
        f"매출성장 {safe_num(fm.get('rev_growth'), 1)}%, "
        f"펀더멘탈 {safe_num(fund.get('score'), 0)}점"
    )
    cur = prev.get("currency", "USD")
    return f"""[긴급 상황]
종목: {symbol}
트리거: {trigger}
현재가: {money(current_price, cur)} (아침 분석가 {money(prev.get('current_price'), cur)})

[아침 분석 결과]
신호: {prev.get('signal')} · 점수: {prev.get('score')}점
요약: {prev.get('summary') or '(없음)'}
핵심 촉매: {prev.get('key_catalyst') or '(없음)'}

[최근 1시간 뉴스]
{fresh_news}

[거시환경]
{macro_line}

[재무]
{fund_line}

질문:
1. 이 급등락의 원인이 뉴스에 있나요?
2. 아침의 {prev.get('signal')} 판단을 유지해야 하나요, 변경해야 하나요?
3. 지금 당장 취해야 할 행동은?

JSON으로만 답해:
{{
  "cause": "원인 1줄",
  "signal_change": true/false,
  "new_signal": "Strong Buy|Buy|Watch|Neutral|Avoid",
  "new_score": 0~100,
  "action": "지금 당장 취할 행동 1줄",
  "urgency": "high|medium|low"
}}"""


def _normalize(data: dict, prev_signal: str) -> dict:
    new_signal = str(data.get("new_signal", "")).strip()
    if new_signal not in VALID_SIGNALS:
        new_signal = prev_signal
    changed = bool(data.get("signal_change")) and new_signal != prev_signal
    score = None
    try:
        score = float(data.get("new_score"))
    except (TypeError, ValueError):
        score = None
    return {
        "cause": str(data.get("cause", "")).strip() or "원인 불명",
        "signal_change": changed,
        "new_signal": new_signal,
        "new_score": score,
        "action": str(data.get("action", "")).strip() or "현 판단 유지",
        "urgency": str(data.get("urgency", "medium")).strip().lower(),
    }


def run(symbol: str, trigger: str, current_price: float,
        daily_result: dict | None = None) -> dict | None:
    """긴급 분석 실행. daily_result(전체 dict)를 받으면 신호 변경 시 갱신·저장.

    반환: 정규화된 LLM 응답 dict (실패 시 None).
    """
    # daily_result가 안 넘어오면 캐시에서 로드
    standalone = daily_result is None
    if daily_result is None:
        daily_result = load_data("daily_result.json")
    if not daily_result or symbol not in daily_result.get("tickers", {}):
        print(f"[긴급] {symbol} daily_result에 없음 → 분석 불가")
        return None
    prev = daily_result["tickers"][symbol]

    print(f"[긴급] {symbol} 분석 시작 (트리거: {trigger})")
    fresh_news = news.fetch_latest(symbol, hours=1)
    prompt = _build_prompt(symbol, trigger, current_price, prev, fresh_news)

    if not llm_analyzer.llm_enabled():
        print(f"[긴급] {symbol} GEMINI_API_KEY 없음 → 긴급 분석 skip")
        return None

    raw = llm_analyzer.generate_json(prompt, label=f"{symbol}(긴급)")
    if raw is None:
        return None
    resp = _normalize(raw, prev.get("signal", "Neutral"))

    update_emergency_log(symbol, trigger)

    # 텔레그램 전송
    msg = reporter.format_emergency_alert(symbol, trigger, current_price, resp, prev)
    print("\n" + msg + "\n")
    if reporter.telegram_enabled():
        reporter.send_telegram_message(msg)

    # 신호 변경 시 daily_result 갱신
    if resp["signal_change"]:
        prev["signal"] = resp["new_signal"]
        if resp["new_score"] is not None:
            prev["score"] = round(resp["new_score"])
        prev["emergency_note"] = f"{trigger}: {resp['cause']}"
        if standalone:
            save_cache("daily_result.json", daily_result)
        print(f"[긴급] {symbol} 신호 변경: {resp['new_signal']}")
    return resp


def main() -> int:
    p = argparse.ArgumentParser(description="긴급 LLM 분석 (테스트)")
    p.add_argument("--ticker", required=True)
    p.add_argument("--trigger", default="수동 테스트")
    p.add_argument("--price", type=float, default=None,
                   help="현재가 (미지정 시 daily_result의 current_price 사용)")
    args = p.parse_args()

    daily = load_data("daily_result.json")
    price = args.price
    if price is None and daily and args.ticker.upper() in daily.get("tickers", {}):
        price = daily["tickers"][args.ticker.upper()].get("current_price")
    if price is None:
        price = 0.0
    run(args.ticker.upper(), args.trigger, float(price), daily)
    return 0


if __name__ == "__main__":
    sys.exit(main())
