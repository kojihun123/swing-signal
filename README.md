# stock-signal — 미국 주식 스윙 트레이딩 AI 분석

찜한 미국 주식을 매일 자동 분석하고, 장중에는 가격을 감시해 진입가/목표가/
손절가 도달과 급등락을 **터미널 + 텔레그램**으로 알려준다.
**자동 주문은 없고 분석 정보 제공만 한다.**

## 4단계 동작

| 단계 | 시점 (KST) | 내용 |
|------|-----------|------|
| ① 풀 분석 | 매일 22:30 | 거시·섹터·종목 종합 + LLM 판단 → `daily_result.json` + 리포트 전송 |
| ② 가격 감시 | 30분마다 (미국 정규장) | 현재가·거래량 체크 → 진입/목표/손절 도달·±3% 급등락 알림 (LLM 없음) |
| ③ 긴급 분석 | 트리거 시 | ±5% 또는 (거래량 3배 + ±2%) → 뉴스 긁어 LLM 긴급 판단 (1시간 재호출 금지) |
| ④ 마감 요약 | 매일 06:10 | 진입 도달·신호 변경·긴급 호출 내역 + 내일 주의 |

## 빠른 시작

```bash
# 1) 종목 편집 (symbol + sector_etf + sector_name)
vi watchlist.json

# 2) API 키 설정 (.env)
cp .env.example .env   # GEMINI / TELEGRAM / FRED (없으면 해당 기능 자동 skip)

# 3) 빌드
docker compose build

# 4) 수동 실행
docker compose run --rm stock-signal python src/full_analysis.py            # 풀 분석
docker compose run --rm stock-signal python src/full_analysis.py --ticker NVDA
docker compose run --rm stock-signal python src/intraday.py --force         # 인트라데이 강제 1회
docker compose run --rm stock-signal python src/final_summary.py            # 마감 요약
docker compose run --rm stock-signal python src/emergency_analyzer.py --ticker NVDA --trigger "주가 +5.8%"

# 5) 스케줄러 가동 (22:30 풀분석 / 1시간 인트라데이 / 06:10 요약)
docker compose up -d && docker compose logs -f
```

## 스코어링 (0~100)

가중합: Macro 20% + Sector 15% + Fundamental 25% + Growth 10% +
Technical 15% + Sentiment 10% · 거시 보정계수(×0.75~1.0) · 섹터 ±5 ·
리스크 감점 · 재무위험 −15.

| 등급 | 점수 | 신호 |
|------|------|------|
| ★★★★★ | 90~100 | Strong Buy (적극매수) |
| ★★★★☆ | 80~89 | Buy (매수) |
| ★★★☆☆ | 70~79 | Watch (관망) |
| ★★☆☆☆ | 60~69 | Neutral (중립) |
| ★☆☆☆☆ | <60 | Avoid (회피) |

진입가 / 목표가(+2·ATR14) / 손절가(−1·ATR14) / R:R을 산출한다.
GEMINI_API_KEY가 있으면 LLM이 진입가·목표가·손절가·요약을 보정한다.

## 산출물
- `data/cache/daily_result.json` — 오늘 풀 분석 결과 (장중 감시·긴급 분석의 기준)
- `data/cache/emergency_log.json` — 긴급 LLM 호출 기록 (1시간 재호출 금지용)
- `data/cache/{macro_today,sector_latest}.json`, `fundamentals/TICKER.json`
- `data/reports/YYYYMMDD.csv` · `data/history/scores_history.csv`

## 시간대
- 스케줄/알림 표시: Asia/Seoul (KST, 컨테이너 TZ)
- 미국장 개장 판단(정규장 09:30~16:00 ET): 코드에서 America/New_York로 명시 변환
