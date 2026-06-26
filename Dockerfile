FROM python:3.11-slim

WORKDIR /app

# tzdata: 미국/한국 시간대 변환에 필요, gcc/g++: 일부 휠 빌드 대비
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/
COPY watchlist.json ./watchlist.json
COPY industry_etf_map.json ./industry_etf_map.json

# 기본 실행: 스케줄러 모드 (장 마감 후 자동 실행)
CMD ["python", "src/main.py", "--schedule"]
