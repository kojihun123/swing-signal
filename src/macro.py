"""거시경제 분석 (탑다운 1단계).

데이터 소스
  - FRED API (무료, .env의 FRED_API_KEY): 금리/물가/고용 지표
  - yfinance (^VIX): 변동성 지수
  - CNN Fear & Greed: 시장 심리 지수

FRED_API_KEY가 없으면 금리/물가/고용 수집을 skip한다. 이 경우
거시경제 점수는 0점으로 '측정불가' 처리하고, 개별 종목에 대한 거시 보정
배수는 1.0(중립)으로 둬서 인프라 미설정만으로 모든 종목이 감점되는 것을
방지한다(VIX·Fear&Greed는 키 없이도 수집해 브리핑에는 표시).

종합 점수(macro_score, 0~100)
  금리 환경 30 + 물가 25 + 고용 25 + 시장심리 20

거시 보정 배수
  70+   → ×1.00
  40~69 → ×0.90
  39-   → ×0.75
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

import requests

FRED_BASE = "https://api.stlouisfed.org/fred/series/observations"
FEAR_GREED_URL = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"

_UA = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept": "application/json",
}


def fred_key() -> str | None:
    return os.environ.get("FRED_API_KEY") or None


@dataclass
class MacroData:
    available: bool = False           # FRED 기반 종합점수 산출 가능 여부
    score: float = 0.0                # 0~100 (available=False면 0)
    multiplier: float = 1.0           # 개별 종목 보정 배수
    label: str = "측정불가"           # 긍정적 / 중립 / 부정적 / 측정불가
    parts: dict = field(default_factory=dict)   # 항목별 부분점수
    metrics: dict = field(default_factory=dict)  # 원시 지표값/해석 문자열


# ---------- FRED ----------


def _fred_series(series_id: str, api_key: str, units: str = "lin",
                 limit: int = 24) -> list[tuple[str, float]]:
    """FRED 관측치를 최신순으로 반환. (date, value) 리스트. 실패 시 빈 리스트."""
    params = {
        "series_id": series_id,
        "api_key": api_key,
        "file_type": "json",
        "sort_order": "desc",
        "limit": limit,
        "units": units,
    }
    try:
        r = requests.get(FRED_BASE, params=params, timeout=15)
        r.raise_for_status()
        obs = r.json().get("observations", [])
    except Exception as e:  # noqa: BLE001
        print(f"[거시] FRED {series_id} 조회 실패: {e}")
        return []
    out = []
    for o in obs:
        v = o.get("value")
        if v in (".", "", None):
            continue
        try:
            out.append((o["date"], float(v)))
        except (TypeError, ValueError):
            continue
    return out


def _latest(series: list[tuple[str, float]]) -> float | None:
    return series[0][1] if series else None


# ---------- 시장 심리 ----------


def fetch_vix() -> float | None:
    try:
        import yfinance as yf
        df = yf.Ticker("^VIX").history(period="5d", interval="1d", auto_adjust=False)
        if df is None or df.empty:
            return None
        return float(df["Close"].dropna().iloc[-1])
    except Exception as e:  # noqa: BLE001
        print(f"[거시] VIX 조회 실패: {e}")
        return None


def fetch_fear_greed() -> tuple[float | None, str | None]:
    """CNN Fear & Greed 지수 (점수, 등급). 실패 시 (None, None)."""
    try:
        r = requests.get(FEAR_GREED_URL, headers=_UA, timeout=15)
        r.raise_for_status()
        fg = r.json().get("fear_and_greed", {})
        score = fg.get("score")
        rating = fg.get("rating")
        return (float(score) if score is not None else None,
                str(rating) if rating else None)
    except Exception as e:  # noqa: BLE001
        print(f"[거시] Fear&Greed 조회 실패: {e}")
        return (None, None)


def _fg_label(rating: str | None, score: float | None) -> str:
    table = {
        "extreme fear": "극단적 공포", "fear": "공포", "neutral": "중립",
        "greed": "탐욕", "extreme greed": "극단적 탐욕",
    }
    if rating and rating.lower() in table:
        return table[rating.lower()]
    if score is None:
        return "N/A"
    if score < 25:
        return "극단적 공포"
    if score < 45:
        return "공포"
    if score < 55:
        return "중립"
    if score < 75:
        return "탐욕"
    return "극단적 탐욕"


# ---------- 종합 분석 ----------


def _trend(series: list[tuple[str, float]], lookback: int = 20) -> str:
    """최신값 vs 과거값 비교로 추세 판정."""
    if len(series) < 2:
        return "N/A"
    recent = series[0][1]
    older = series[min(lookback, len(series) - 1)][1]
    diff = recent - older
    if abs(diff) < 1e-9:
        return "보합"
    return "하락 중" if diff < 0 else "상승 중"


def analyze_macro(cache_dir: str = "data/macro") -> MacroData:
    """거시경제 종합 분석."""
    key = fred_key()

    # 시장 심리는 FRED 키와 무관하게 수집
    vix = fetch_vix()
    fg_score, fg_rating = fetch_fear_greed()
    fg_label = _fg_label(fg_rating, fg_score)

    # 시장심리 점수 (20점: VIX 12 + Fear&Greed 8)
    senti = 0.0
    if vix is not None:
        senti += 12 if vix <= 20 else 6 if vix <= 30 else 2
        vix_label = "안정" if vix <= 20 else "주의" if vix <= 30 else "공포"
    else:
        senti += 6
        vix_label = "N/A"
    if fg_score is not None:
        senti += 8 if fg_score >= 55 else 5 if fg_score >= 25 else 2
    else:
        senti += 4

    metrics = {
        "vix": vix, "vix_label": vix_label,
        "fg_score": fg_score, "fg_label": fg_label,
    }

    if not key:
        print("[거시] FRED_API_KEY 없음 → 금리/물가/고용 skip, 거시 점수 측정불가(보정 1.0)")
        md = MacroData(
            available=False, score=0.0, multiplier=1.0, label="측정불가",
            parts={"rates": None, "inflation": None, "employment": None,
                   "sentiment": round(senti, 1)},
            metrics=metrics,
        )
        _save_cache(md, cache_dir)
        return md

    # --- 금리 (30점) ---
    fedfunds = _fred_series("FEDFUNDS", key, limit=6)
    dgs10 = _fred_series("DGS10", key, limit=40)
    dgs2 = _fred_series("DGS2", key, limit=40)
    ff = _latest(fedfunds)
    y10 = _latest(dgs10)
    y2 = _latest(dgs2)
    y10_trend = _trend(dgs10, 20)
    spread = (y10 - y2) if (y10 is not None and y2 is not None) else None

    rates = 0.0
    if ff is not None:
        rates += 15 if ff <= 4 else 9 if ff < 5 else 4
    else:
        rates += 7
    rates += {"하락 중": 8, "보합": 5, "상승 중": 2}.get(y10_trend, 5)
    if spread is not None:
        rates += 7 if spread > 0.2 else 4 if spread > 0 else 1
    else:
        rates += 4

    # --- 물가 (25점: CPI 15 + 근원 10) ---
    cpi = _fred_series("CPIAUCSL", key, units="pc1", limit=6)
    core = _fred_series("CPILFESL", key, units="pc1", limit=6)
    cpi_yoy = _latest(cpi)
    core_yoy = _latest(core)
    cpi_trend = _trend(cpi, 1)
    infl = 0.0
    if cpi_yoy is not None:
        infl += 15 if cpi_yoy <= 2 else 9 if cpi_yoy < 4 else 3
    else:
        infl += 7
    if core_yoy is not None:
        infl += 10 if core_yoy <= 2 else 6 if core_yoy < 4 else 2
    else:
        infl += 5

    # --- 추가 거시지표 (표시용: PCE / GDP / NFP) ---
    pce = _fred_series("PCEPI", key, units="pc1", limit=6)
    gdp = _fred_series("A191RL1Q225SBEA", key, limit=4)
    payems = _fred_series("PAYEMS", key, limit=3)
    pce_yoy = _latest(pce)
    gdp_growth = _latest(gdp)
    nfp_change = None  # 전월 대비 비농업고용 증감(천명)
    if len(payems) >= 2:
        nfp_change = payems[0][1] - payems[1][1]

    # --- 고용 (25점: 실업률 15 + 신규청구 10) ---
    unrate = _fred_series("UNRATE", key, limit=6)
    icsa = _fred_series("ICSA", key, limit=6)
    ur = _latest(unrate)
    icsa_dir = _trend(icsa, 1)   # 직전 주 대비
    emp = 0.0
    if ur is not None:
        emp += 15 if ur <= 4 else 9 if ur < 5 else 3
    else:
        emp += 7
    # 신규 실업수당: 감소(하락 중)면 긍정
    emp += {"하락 중": 10, "보합": 6, "상승 중": 2}.get(icsa_dir, 6)

    total = round(rates + infl + emp + senti, 1)
    label = "긍정적" if total >= 70 else "중립" if total >= 40 else "부정적"
    mult = 1.0 if total >= 70 else 0.9 if total >= 40 else 0.75

    metrics.update({
        "fedfunds": ff, "y10": y10, "y2": y2, "y10_trend": y10_trend,
        "spread": spread, "inverted": (spread is not None and spread < 0),
        "cpi_yoy": cpi_yoy, "cpi_trend": cpi_trend, "core_yoy": core_yoy,
        "pce_yoy": pce_yoy, "gdp_growth": gdp_growth, "nfp_change": nfp_change,
        "unrate": ur, "icsa": _latest(icsa), "icsa_dir": icsa_dir,
    })

    md = MacroData(
        available=True, score=total, multiplier=mult, label=label,
        parts={"rates": round(rates, 1), "inflation": round(infl, 1),
               "employment": round(emp, 1), "sentiment": round(senti, 1)},
        metrics=metrics,
    )
    _save_cache(md, cache_dir)
    return md


def _save_cache(md: MacroData, cache_dir: str) -> None:
    payload = {
        "available": md.available, "score": md.score,
        "multiplier": md.multiplier, "label": md.label,
        "parts": md.parts, "metrics": md.metrics,
    }
    # 1) 공통 캐시 (cache/macro_today.json) — 하루 1회 덮어쓰기
    try:
        from cache import save_cache
        save_cache("macro_today.json", payload)
    except Exception as e:  # noqa: BLE001
        print(f"[거시] 공통 캐시 저장 실패: {e}")
    # 2) 디버그용 사본 (data/macro/macro_latest.json)
    try:
        os.makedirs(cache_dir, exist_ok=True)
        with open(os.path.join(cache_dir, "macro_latest.json"), "w",
                  encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
    except Exception:  # noqa: BLE001
        pass
