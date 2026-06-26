#!/usr/bin/env bash
# 서버 배포 스크립트 — git pull → 빌드 → 재시작 (한 방).
#
#   ./deploy.sh             기본: pull → 빌드 → 재시작(up -d)
#   ./deploy.sh --test      pull → 빌드 → 풀분석 1회 테스트(전송 skip), 상주 안 함
#   ./deploy.sh --no-build  pull → 재시작만(빌드 생략). 코드만 바뀐 경우 빠름.
set -euo pipefail
cd "$(dirname "$0")"

BUILD=1; TEST=0
for a in "$@"; do
  case "$a" in
    --no-build) BUILD=0 ;;
    --test) TEST=1 ;;
    *) echo "알 수 없는 옵션: $a"; exit 1 ;;
  esac
done

c() { printf "\n\033[1;36m▶ %s\033[0m\n" "$*"; }
ok() { printf "\033[1;32m✓ %s\033[0m\n" "$*"; }
err() { printf "\033[1;31m✗ %s\033[0m\n" "$*"; }

# docker compose vs docker-compose 자동 감지
if docker compose version >/dev/null 2>&1; then DC="docker compose"; else DC="docker-compose"; fi

# 0) 사전 점검 — .env / 토스 키
[ -f .env ] || { err ".env 없음 — API 키 파일을 먼저 만드세요"; exit 1; }
if ! grep -q '^TOSS_CLIENT_ID=.\+' .env; then
  printf "\033[1;33m⚠ .env에 TOSS_CLIENT_ID 미설정 — 시세가 네이버/yfinance 폴백으로 동작합니다\033[0m\n"
fi

# 1) 최신 코드
c "git pull origin main"
git pull origin main
ok "코드 갱신: $(git log --oneline -1)"

# 2) 빌드
if [ "$BUILD" = 1 ]; then
  c "이미지 빌드"
  $DC build
  ok "빌드 완료"
fi

# 3) 테스트 모드 — 풀분석 1회만 돌려보고 종료(전송 skip)
if [ "$TEST" = 1 ]; then
  c "테스트 풀분석 (텔레그램 전송 skip)"
  $DC run --rm -e TELEGRAM_BOT_TOKEN= -e TELEGRAM_CHAT_ID= \
    stock-signal python src/main.py --full
  ok "테스트 완료 — 문제 없으면 './deploy.sh' 로 상주 기동"
  exit 0
fi

# 4) 재시작 (상주)
c "스케줄러 재시작 (up -d)"
$DC up -d
ok "기동 완료"
$DC ps
c "최근 로그 (Ctrl+C로 빠져나오기)"
$DC logs -f --tail 20
