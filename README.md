# stock-signal — 미국·한국 주식 스윙 트레이딩 AI 분석

찜한 종목을 매일 자동 분석하고, 장중에는 신규 뉴스를 감시해 변화가 생기면
**텔레그램**으로 브리핑한다. 거시 → 시장환경 → 섹터 → 종목 탑다운 구조.
**자동 주문은 없고 분석 정보 제공만 한다.**

## 동작 (ET 앵커 — 미국 서머타임·공휴일 자동 추종)

| 단계 | 시점 | 내용 |
|------|------|------|
| ① 풀 분석 | 매일 ET 08:30 (개장 1시간 전) | 거시 4축 → 섹터 로테이션 → 종목 2-pass(상위 N개만 LLM) → 리포트 전송 |
| ② 뉴스 감시 | 60분마다 (미국 세션) | 종목별 신규 뉴스만 체크 → 새 뉴스 시 LLM 재분석 + 알림 |
| ③ 마감 요약 | 매일 ET 17:00 (마감 1시간 후) | 하루 신호 변경·흐름 종합 |

## 데이터 소스

| 데이터 | 출처 |
|--------|------|
| 시세(OHLCV·현재가·장운영캘린더) | **토스증권 Open API** (한미 통일 심볼, OAuth2) → 네이버 → KIS → yfinance 폴백 |
| 뉴스·수급·공매도·재무·컨센서스 | 네이버증권(한국어) · KRX(공매도) · Finviz(US 폴백) |
| 거시(금리·물가·고용·유동성·신용) | FRED · yfinance(VIX·DXY·SPY/QQQ) · CNN(Fear&Greed) |
| 섹터 ETF 강도 | yfinance |
| LLM 종합판단·섹터평가·시장브리핑 | Gemini (+ Groq 폴백) |

## 분석 구성

- **거시 4축(100점)**: 통화정책25 + 경기30 + 금융환경25 + 시장심리20 → Market Regime(국면/위험도/유동성) 도출
- **섹터**: GICS 11섹터 LLM 로테이션 평가(상대강도·추세·모멘텀·거시적합도) + 자금 유입/유출
- **종목 점수(0~100)**: Fundamental25 + Macro20 + Sector15 + Technical15 + Growth10 + Sentiment10
  · 거시 보정계수(×0.75~1.0) · 섹터 ±5 · 리스크 감점
- **기술 시그널**: RSI 다이버전스 · 볼린저 양방향 · MACD 0선맥락 · 일목균형표 · 주봉 과열(일/주봉 콤비)

| 등급 | 점수 | 신호 |
|------|------|------|
| ★★★★★ | 90~100 | 적극매수 |
| ★★★★☆ | 80~89 | 매수 |
| ★★★☆☆ | 70~79 | 관망 |
| ★★☆☆☆ | 60~69 | 중립 |
| ★☆☆☆☆ | <60 | 회피 |

## 빠른 시작

```bash
# 1) 종목 편집 — {"symbol","name","profile":{country,sector,industry}}
vi watchlist.json

# 2) API 키 (.env) — 아래 '환경변수' 참고
cp .env.example .env && vi .env

# 3) 빌드 + 수동 1회 실행
docker compose build
docker compose run --rm stock-signal python src/main.py --full       # 풀 분석
docker compose run --rm stock-signal python src/main.py --intraday --force  # 뉴스감시 1회
docker compose run --rm stock-signal python src/main.py --summary     # 마감 요약

# 4) 스케줄러 상주 (go-live)
docker compose up -d && docker compose logs -f
```

## 환경변수 (.env)

| 키 | 용도 | 없으면 |
|----|------|--------|
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | 리포트 전송 | 콘솔만 출력 |
| `GEMINI_API_KEY` | LLM 종합판단·섹터·브리핑 | 기계점수만 사용 |
| `GROQ_API_KEY` | LLM 폴백(선택) | Gemini만 사용 |
| `TOSS_CLIENT_ID` / `TOSS_CLIENT_SECRET` | 시세 주 소스 | 네이버/yfinance 폴백 |
| `FRED_API_KEY` | 거시 금리/물가/고용/유동성 | 측정불가(VIX·F&G만) |
| `FINNHUB_API_KEY` / `KIS_APP_KEY`·`KIS_APP_SECRET` | 시세 폴백(선택) | yfinance 폴백 |

## 배포 (서버 갱신)

```bash
./deploy.sh           # git pull → 빌드 → 재시작 (한 방)
./deploy.sh --test    # git pull → 빌드 → 풀분석 1회 테스트(전송 skip) → 미기동
./deploy.sh --no-build  # 코드만 바뀐 경우 빌드 생략(빠름)
```

## 산출물
- `data/cache/daily_result.json` — 오늘 풀 분석 결과 (장중 감시 기준)
- `data/cache/{macro_today,sector_latest,sector_rotation,toss_token}.json`
- `data/reports/YYYYMMDD.csv` · `data/history/scores_history.csv`

## 시간대
- 스케줄: **ET 앵커**(`schedule.at(time, "America/New_York")`) → 서머타임 자동
- 장 운영 판단: 토스 장운영 캘린더(공휴일·DST 자동) → 실패 시 ET 하드코딩 폴백
- 알림 표시: Asia/Seoul (KST)
