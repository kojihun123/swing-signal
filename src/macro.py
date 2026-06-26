"""거시경제 분석 (탑다운 1단계) — 4축 구성.

데이터 소스
  - FRED API (무료, .env의 FRED_API_KEY): 금리/물가/고용/유동성/신용 지표
  - yfinance: ^VIX(변동성), DX-Y.NYB(달러인덱스), SPY·QQQ(지수 추세)
  - CNN Fear & Greed: 시장 심리 지수

종합 점수(macro_score, 0~100) = 4축 합산
  통화정책 25 : 기준금리, 10년물(수준+추세), 장단기 금리차
  경기     30 : CPI·근원CPI, 실업률·신규청구, PMI 프록시(지역연준), 소매판매
  금융환경 25 : 달러(DXY), 연준 대차대조표(WALCL), M2, 하이일드 스프레드
  시장심리 20 : VIX, Fear&Greed, SPY/QQQ 추세

ISM PMI는 FRED 라이선스 폐지(NAPM=무데이터) → 필라델피아·뉴욕 연준 제조업
서베이 평균을 PMI 프록시로 사용(>0=확장).

FRED_API_KEY가 없으면 통화/경기/금융환경 수집을 skip하고 '측정불가'(점수 0,
보정 1.0)로 처리해 인프라 미설정만으로 전 종목이 감점되는 것을 방지한다.
시장심리·DXY·지수추세는 키 없이도 수집해 브리핑에 표시한다.

거시 보정 배수
  70+ → ×1.00 · 40~69 → ×0.90 · 39- → ×0.75
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


def fetch_dxy() -> tuple[float | None, str | None]:
    """달러 인덱스(DXY) 현재값 + 추세(약 1개월). 실패 시 (None, None)."""
    try:
        import yfinance as yf
        df = yf.Ticker("DX-Y.NYB").history(period="3mo", interval="1d",
                                           auto_adjust=False)
        c = df["Close"].dropna() if df is not None else None
        if c is None or c.empty:
            return None, None
        level = float(c.iloc[-1])
        ago = float(c.iloc[-21]) if len(c) >= 21 else float(c.iloc[0])
        trend = ("상승 중" if level > ago * 1.005 else
                 "하락 중" if level < ago * 0.995 else "보합")
        return level, trend
    except Exception as e:  # noqa: BLE001
        print(f"[거시] DXY 조회 실패: {e}")
        return None, None


def fetch_index_trend(symbol: str) -> dict | None:
    """지수 ETF의 50/200일선 상회 여부. 실패 시 None."""
    try:
        import yfinance as yf
        df = yf.Ticker(symbol).history(period="1y", interval="1d",
                                       auto_adjust=True)
        c = df["Close"].dropna() if df is not None else None
        if c is None or len(c) < 50:
            return None
        price = float(c.iloc[-1])
        ma50 = float(c.tail(50).mean())
        ma200 = float(c.tail(200).mean()) if len(c) >= 200 else ma50
        return {"price": price, "above_50": price > ma50,
                "above_200": price > ma200}
    except Exception as e:  # noqa: BLE001
        print(f"[거시] {symbol} 추세 조회 실패: {e}")
        return None


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


def _idx_trend_score(spy: dict | None, qqq: dict | None) -> tuple[float, str]:
    """SPY/QQQ의 50·200일선 상회 개수로 추세 점수(0~6) + 라벨."""
    flags: list[bool] = []
    for d in (spy, qqq):
        if d:
            flags += [d["above_200"], d["above_50"]]
    if not flags:
        return 3.0, "N/A"
    cnt = sum(1 for f in flags if f)
    score = round(6 * cnt / len(flags), 1)
    label = "정배열" if cnt == len(flags) else "하락추세" if cnt == 0 else "혼조"
    return score, label


def analyze_macro(cache_dir: str = "data/macro") -> MacroData:
    """거시경제 4축 종합 분석.

    통화정책 25 + 경기 30 + 금융환경 25 + 시장심리 20 = 0~100점.
    시장심리·DXY·지수추세는 FRED 키 없이도 수집(브리핑 표시용).
    """
    key = fred_key()

    # ── FRED 키 없이도 수집되는 시장 데이터 ──
    vix = fetch_vix()
    fg_score, fg_rating = fetch_fear_greed()
    fg_label = _fg_label(fg_rating, fg_score)
    dxy, dxy_trend = fetch_dxy()
    spy_t = fetch_index_trend("SPY")
    qqq_t = fetch_index_trend("QQQ")

    vix_label = ("안정" if (vix or 99) <= 20 else "주의" if (vix or 99) <= 30
                 else "공포") if vix is not None else "N/A"
    metrics = {
        "vix": vix, "vix_label": vix_label,
        "fg_score": fg_score, "fg_label": fg_label,
        "dxy": dxy, "dxy_trend": dxy_trend,
        "spy_above_200": (spy_t or {}).get("above_200"),
        "qqq_above_200": (qqq_t or {}).get("above_200"),
    }

    # ── 시장심리 (20): VIX 8 + Fear&Greed 6 + 지수추세 6 ──
    senti = 0.0
    if vix is not None:
        senti += 8 if vix <= 15 else 6 if vix <= 20 else 3 if vix <= 30 else 1
    else:
        senti += 4
    if fg_score is not None:
        senti += 6 if fg_score >= 55 else 4 if fg_score >= 25 else 2
    else:
        senti += 3
    idx_score, idx_label = _idx_trend_score(spy_t, qqq_t)
    senti += idx_score
    metrics["index_trend"] = idx_label

    if not key:
        print("[거시] FRED_API_KEY 없음 → 통화/경기/금융환경 skip, 측정불가(보정 1.0)")
        md = MacroData(
            available=False, score=0.0, multiplier=1.0, label="측정불가",
            parts={"monetary": None, "economy": None, "financial": None,
                   "sentiment": round(senti, 1)},
            metrics=metrics)
        _save_cache(md, cache_dir)
        return md

    # ===== 통화정책 (25): 기준금리 10 + 10년물(수준5+추세3) + 스프레드 7 =====
    fedfunds = _fred_series("FEDFUNDS", key, limit=6)
    dgs10 = _fred_series("DGS10", key, limit=40)
    dgs2 = _fred_series("DGS2", key, limit=40)
    ff = _latest(fedfunds)
    y10 = _latest(dgs10)
    y2 = _latest(dgs2)
    y10_trend = _trend(dgs10, 20)
    spread = (y10 - y2) if (y10 is not None and y2 is not None) else None

    monetary = 0.0
    if ff is not None:
        monetary += 10 if ff <= 3 else 7 if ff <= 4 else 4 if ff < 5 else 2
    else:
        monetary += 5
    if y10 is not None:
        monetary += 5 if y10 <= 3.5 else 3 if y10 <= 4.5 else 1
    else:
        monetary += 2.5
    monetary += {"하락 중": 3, "보합": 2, "상승 중": 1}.get(y10_trend, 2)
    if spread is not None:
        monetary += 7 if spread > 0.2 else 4 if spread > 0 else 1
    else:
        monetary += 4

    # ===== 경기 (30): CPI 7 + Core 5 + 실업 5 + 신규청구 4 + PMI 5 + 소매 4 =====
    cpi = _fred_series("CPIAUCSL", key, units="pc1", limit=6)
    core = _fred_series("CPILFESL", key, units="pc1", limit=6)
    cpi_yoy = _latest(cpi)
    core_yoy = _latest(core)
    cpi_trend = _trend(cpi, 1)
    unrate = _fred_series("UNRATE", key, limit=6)
    icsa = _fred_series("ICSA", key, limit=6)
    ur = _latest(unrate)
    icsa_dir = _trend(icsa, 1)
    retail_yoy = _latest(_fred_series("RSAFS", key, units="pc1", limit=6))
    # PMI 프록시: ISM은 FRED 폐지 → 지역 연준 제조업 서베이 평균(>0=확장)
    philly = _latest(_fred_series("GACDFSA066MSFRBPHI", key, limit=3))
    empire = _latest(_fred_series("GACDINA066MNFRBNY", key, limit=3))
    pmi_vals = [v for v in (philly, empire) if v is not None]
    pmi_proxy = round(sum(pmi_vals) / len(pmi_vals), 1) if pmi_vals else None

    economy = 0.0
    if cpi_yoy is not None:
        economy += (7 if cpi_yoy <= 2 else 5 if cpi_yoy < 3
                    else 3 if cpi_yoy < 4 else 1)
    else:
        economy += 3.5
    if core_yoy is not None:
        economy += (5 if core_yoy <= 2 else 3.5 if core_yoy < 3
                    else 2 if core_yoy < 4 else 1)
    else:
        economy += 2.5
    if ur is not None:
        economy += 5 if ur <= 4 else 3 if ur < 5 else 1
    else:
        economy += 2.5
    economy += {"하락 중": 4, "보합": 2.5, "상승 중": 1}.get(icsa_dir, 2.5)
    if pmi_proxy is not None:
        economy += (5 if pmi_proxy >= 10 else 3.5 if pmi_proxy >= 0
                    else 2 if pmi_proxy >= -10 else 1)
    else:
        economy += 2.5
    if retail_yoy is not None:
        economy += (4 if retail_yoy >= 4 else 3 if retail_yoy >= 2
                    else 2 if retail_yoy >= 0 else 1)
    else:
        economy += 2

    # ===== 금융환경 (25): DXY 8 + WALCL 7 + M2 4 + HY스프레드 6 =====
    walcl = _fred_series("WALCL", key, limit=20)     # 주간
    m2 = _fred_series("M2SL", key, limit=18)         # 월간
    hy_spread = _latest(_fred_series("BAMLH0A0HYM2", key, limit=6))
    wti = _latest(_fred_series("DCOILWTICO", key, limit=6))
    walcl_trend = _trend(walcl, 13)   # ~1분기
    m2_trend = _trend(m2, 12)         # ~1년

    financial = 0.0
    # DXY: 약달러=호재 (수준 4 + 추세 4)
    if dxy is not None:
        financial += 4 if dxy <= 100 else 2.5 if dxy <= 105 else 1
    else:
        financial += 2
    financial += {"하락 중": 4, "보합": 2.5, "상승 중": 1}.get(dxy_trend, 2)
    # WALCL: 확장(QE)=유동성 호재, 축소(QT)=악재
    financial += {"상승 중": 7, "보합": 4, "하락 중": 2}.get(walcl_trend, 4)
    financial += {"상승 중": 4, "보합": 2.5, "하락 중": 1}.get(m2_trend, 2.5)
    # 하이일드 스프레드: 낮을수록 신용 건강
    if hy_spread is not None:
        financial += (6 if hy_spread < 3 else 4 if hy_spread < 4
                      else 2.5 if hy_spread < 5 else 1.5 if hy_spread < 6 else 0.5)
    else:
        financial += 3

    total = round(monetary + economy + financial + senti, 1)
    label = "긍정적" if total >= 70 else "중립" if total >= 40 else "부정적"
    mult = 1.0 if total >= 70 else 0.9 if total >= 40 else 0.75

    # 추가 표시용 (PCE / GDP / NFP)
    pce_yoy = _latest(_fred_series("PCEPI", key, units="pc1", limit=6))
    gdp_growth = _latest(_fred_series("A191RL1Q225SBEA", key, limit=4))
    payems = _fred_series("PAYEMS", key, limit=3)
    nfp_change = (payems[0][1] - payems[1][1]) if len(payems) >= 2 else None

    metrics.update({
        "fedfunds": ff, "y10": y10, "y2": y2, "y10_trend": y10_trend,
        "spread": spread, "inverted": (spread is not None and spread < 0),
        "cpi_yoy": cpi_yoy, "cpi_trend": cpi_trend, "core_yoy": core_yoy,
        "pce_yoy": pce_yoy, "gdp_growth": gdp_growth, "nfp_change": nfp_change,
        "unrate": ur, "icsa": _latest(icsa), "icsa_dir": icsa_dir,
        "retail_yoy": retail_yoy, "pmi_proxy": pmi_proxy,
        "walcl_trend": walcl_trend, "m2_trend": m2_trend,
        "hy_spread": hy_spread, "wti": wti,
    })

    md = MacroData(
        available=True, score=total, multiplier=mult, label=label,
        parts={"monetary": round(monetary, 1), "economy": round(economy, 1),
               "financial": round(financial, 1), "sentiment": round(senti, 1)},
        metrics=metrics)
    _save_cache(md, cache_dir)
    return md


def market_regime(md: "MacroData") -> dict:
    """4축 거시 → 시장 레짐 한 줄. {phase, risk, liquidity, summary}.

    phase(경기): Expansion/Neutral/Contraction · risk: Risk-On/Neutral/Risk-Off
    liquidity: Improving/Neutral/Tightening. available=False면 전부 중립.
    축 만점: 통화25·경기30·금융25·심리20.
    """
    if not md.available:
        return {"phase": "판단불가", "risk": "중립", "liquidity": "중립",
                "summary": "거시 측정불가 → 레짐 중립"}
    p, m = md.parts or {}, md.metrics or {}

    # phase — 경기 점수 + PMI 프록시
    eco = p.get("economy") or 0
    pmi = m.get("pmi_proxy")
    if eco >= 22 or (pmi is not None and pmi >= 8):
        phase = "Expansion"
    elif eco >= 15:
        phase = "Neutral"
    else:
        phase = "Contraction"

    # risk — 심리 점수 + 하이일드 스프레드 + 지수추세
    senti = p.get("sentiment") or 0
    hy = m.get("hy_spread")
    idx = m.get("index_trend")
    if senti < 9 or (hy is not None and hy >= 5):
        risk = "Risk-Off"
    elif senti >= 14 and (hy is None or hy < 4) and idx in ("정배열", "N/A", None):
        risk = "Risk-On"
    else:
        risk = "Neutral"

    # liquidity — 연준 B/S·M2 추세 + 달러 방향
    walcl, m2, dxy_t = m.get("walcl_trend"), m.get("m2_trend"), m.get("dxy_trend")
    if (walcl == "상승 중" or m2 == "상승 중") and dxy_t != "상승 중":
        liquidity = "Improving"
    elif walcl == "하락 중" and dxy_t == "상승 중":
        liquidity = "Tightening"
    else:
        liquidity = "Neutral"

    return {"phase": phase, "risk": risk, "liquidity": liquidity,
            "summary": f"{phase} · {risk} · 유동성 {liquidity}"}


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
