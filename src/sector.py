"""ETF 영향력 매핑 & 강도/로테이션 분석.

종목을 단일 섹터 ETF에 묶지 않고, 영향을 주는 ETF들을 4개 축으로 분해한다.
  · country   국가 ETF (KR→EWY, US→없음)
  · industry  업종 대표 ETF (가장 중요) — 예: 메모리반도체 → SOXX, SMH
  · technology기술주 전체 흐름 — QQQ/XLK/VGT
  · market    시장 전체 흐름 — SPY 기본
매핑은 '구성종목 여부'가 아니라 '시장 영향력(상관)' 기준이다(삼성전자는 SOXX
구성종목이 아니어도 SOXX와 동행 → industry=SOXX/SMH).

단일 진실원은 industry_etf_map.json. 종목엔 industry 라벨만 붙이고 여기서 세트를
조립하므로 "같은 industry = 같은 세트" 일관성이 구조적으로 보장된다.

각 종목은 등장하는 모든 ETF의 1일/5일/1개월 수익률·거래량비·RSI(14)를 계산해
대표 업종 ETF의 5일 수익률 순위로 ±5점 보정을 적용한다(상위 30% +5 / 하위 30% -5).
방어섹터(XLU 유틸리티, XLP 필수소비재) 강세는 시장 위험 신호로 해석한다.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
import yfinance as yf

DEFENSIVE_ETFS = ("XLU", "XLP")
# GICS 11개 섹터 ETF — LLM 섹터 로테이션 분석 대상(sector_llm).
ROTATION_ETFS: dict[str, str] = {
    "XLK": "기술", "SOXX": "반도체", "XLF": "금융", "XLE": "에너지",
    "XLV": "헬스케어", "XLI": "산업재", "XLB": "소재",
    "XLY": "임의소비재", "XLP": "필수소비재", "XLU": "유틸리티",
    "XLRE": "부동산",
}
# 매핑에 안 쓰여도 항상 수집: 벤치마크(SPY) + 방어섹터 + 로테이션 11섹터.
_ALWAYS_FETCH = ("SPY", "XLU", "XLP", *ROTATION_ETFS.keys())

# ETF 심볼 → 한글 라벨 (리포트 표기용). 미등재 ETF는 심볼 그대로 표기.
ETF_LABELS: dict[str, str] = {
    "SPY": "S&P500", "QQQ": "나스닥100", "DIA": "다우", "IWM": "러셀2000",
    "XLK": "기술", "VGT": "기술(VGT)", "EWY": "한국",
    "SOXX": "반도체", "SMH": "반도체(SMH)",
    "IGV": "소프트웨어", "AIQ": "AI", "QTUM": "양자", "ARKQ": "차세대기술",
    "HACK": "사이버보안", "CIBR": "사이버보안(CIBR)",
    "SKYY": "클라우드", "CLOU": "클라우드(CLOU)", "IGN": "네트워킹",
    "IHI": "의료기기", "BOTZ": "로보틱스", "ITA": "방산", "FINX": "핀테크",
    "FDN": "인터넷", "XLU": "유틸리티", "XLP": "필수소비재",
    "XLF": "금융", "XLE": "에너지", "XLV": "헬스케어", "XLI": "산업재",
    "XLB": "소재", "XLY": "임의소비재", "XLRE": "부동산",
    "KBE": "은행", "IAI": "증권/IB", "IPAY": "결제", "XOP": "원유·가스E&P",
    "PAVE": "인프라", "XAR": "항공우주·방산", "MOO": "농업",
    "XPH": "제약", "IHF": "헬스케어서비스", "CARZ": "자동차",
}

# yfinance industry 문자열 → 내부 industry 라벨(테이블 키). 부분일치, 구체 우선.
# yfinance는 메모리/로직 반도체를 구분하지 않으므로(둘 다 'Semiconductors'),
# 더 세분화하려면 watchlist의 profile.industry로 수동 지정한다.
INDUSTRY_ALIASES: list[tuple[str, str]] = [
    ("semiconductor", "Semiconductor"),
    ("software - infrastructure", "Enterprise Software"),
    ("software - application", "Enterprise Software"),
    ("software", "Enterprise Software"),
    ("information technology services", "Enterprise Software"),
    ("aerospace", "Defense"), ("defense", "Defense"),
    ("communication equipment", "Networking"),
    ("medical devices", "Medical Device"),
    ("medical instruments", "Medical Device"),
    ("internet content", "Internet"), ("internet retail", "Internet"),
    ("credit services", "Fintech"),
]

_MAP_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                         "industry_etf_map.json")


def _load_map() -> dict:
    """industry_etf_map.json 로드. 실패 시 최소 폴백(시장=SPY)."""
    try:
        with open(_MAP_PATH, encoding="utf-8") as f:
            m = json.load(f)
        if isinstance(m.get("industries"), dict):
            return m
    except Exception as e:  # noqa: BLE001
        print(f"[섹터] industry_etf_map.json 로드 실패: {e}")
    return {"defaults": {"country": {"US": [], "KR": ["EWY"]},
                         "market": ["SPY"]}, "industries": {}}


ETF_MAP = _load_map()


def _universe(m: dict) -> list[str]:
    """매핑에 등장하는 모든 ETF + 상시수집 ETF의 합집합(수집 대상)."""
    etfs: set[str] = set(_ALWAYS_FETCH)
    d = m.get("defaults", {})
    etfs.update(d.get("market", []))
    for v in (d.get("country") or {}).values():
        etfs.update(v)
    for ind in m.get("industries", {}).values():
        for cat in ("industry", "technology", "market"):
            etfs.update(ind.get(cat, []))
    return sorted(etfs)


# 수집·랭킹 대상 ETF 전체 (심볼→라벨). full_analysis가 개수 표기에 사용.
SECTOR_ETFS: dict[str, str] = {e: ETF_LABELS.get(e, e) for e in _universe(ETF_MAP)}


def _is_kr(ticker: str) -> bool:
    return ticker.upper().endswith((".KS", ".KQ"))


def normalize_industry(info: dict | None) -> str | None:
    """yfinance industry 문자열 → 내부 industry 라벨. 매칭 실패 시 None."""
    text = str((info or {}).get("industry", "")).strip().lower()
    for kw, label in INDUSTRY_ALIASES:
        if kw in text:
            return label
    return None


def resolve_links(ticker: str, info: dict | None = None,
                  profile: dict | None = None,
                  sector_etf: str | None = None) -> dict:
    """종목의 4축 ETF 영향력 링크를 조립.

    반환: {"label": str|None, "country_code": "US"|"KR",
           "links": {"country": [], "industry": [], "technology": [], "market": []}}

    industry 라벨 결정 우선순위:
      1) profile.industry 수동지정 (예: 'Memory Semiconductor')
      2) yfinance industry 자동 정규화(normalize_industry)
      3) (구버전) sector_etf 직접지정 → 그 ETF 한 개를 industry 세트로
    """
    profile = profile or {}
    country = str(profile.get("country") or
                  ("KR" if _is_kr(ticker) else "US")).upper()
    defaults = ETF_MAP.get("defaults", {})

    label = profile.get("industry") or normalize_industry(info)
    spec = ETF_MAP.get("industries", {}).get(label or "", {})
    industry = list(spec.get("industry", []))
    if not industry and sector_etf:          # 구버전 watchlist 폴백
        industry = [sector_etf]
        label = label or ETF_LABELS.get(sector_etf, sector_etf)

    market = list(spec.get("market", defaults.get("market", ["SPY"])))
    links = {
        "country": list((defaults.get("country") or {}).get(country, [])),
        "industry": industry,
        "technology": list(spec.get("technology", [])),
        "market": market,
    }
    return {"label": label, "country_code": country, "links": links}


# 경기민감(Cyclical) vs 방어(Defensive) 섹터 — Macro Fit 판정용
_CYCLICAL = {"XLK", "SOXX", "XLF", "XLI", "XLY", "XLB", "XLE"}
_DEFENSIVE = {"XLU", "XLP", "XLV", "XLRE"}


def sector_score(m: dict) -> int:
    """섹터 강도 0~100. 기준 50 + 상대강도·추세·모멘텀·200일선."""
    s = 50.0
    if m.get("rs_1m") is not None:
        s += max(min(m["rs_1m"], 15), -15)          # SPY 대비 1개월 ±15
    s += {"정배열": 15, "혼조": 0, "역배열": -15}.get(m.get("ma_alignment"), 0)
    rsi = m.get("rsi")
    if rsi is not None:
        s += max(min((rsi - 50) * 0.3, 12), -12)     # 모멘텀 ±12
    if m.get("above_ma200"):
        s += 5
    return int(max(0, min(100, round(s))))


def momentum_label(m: dict) -> str:
    rsi = m.get("rsi")
    if rsi is None:
        return "N/A"
    return "Strong" if rsi >= 60 else "Weak" if rsi <= 45 else "Moderate"


def macro_fit(etf: str, regime: dict | None) -> str:
    """섹터가 현재 거시 레짐에 부합하는 정도 (High/Medium/Low)."""
    rg = regime or {}
    cyc = etf in _CYCLICAL
    pro_risk = rg.get("phase") == "Expansion" or rg.get("risk") == "Risk-On"
    anti_risk = rg.get("phase") == "Contraction" or rg.get("risk") == "Risk-Off"
    if pro_risk and not anti_risk:
        return "High" if cyc else "Low" if etf in _DEFENSIVE else "Medium"
    if anti_risk and not pro_risk:
        return "High" if etf in _DEFENSIVE else "Low" if cyc else "Medium"
    return "Medium"


def needed_etfs(entries: list[dict] | None) -> dict[str, str]:
    """워치리스트가 실제 쓰는 ETF만 추린 {심볼:라벨} (수집 prune용).

    = GICS 로테이션 11섹터 + 상시수집(SPY·방어 XLU/XLP) + 각 종목 4축 매핑 ETF.
    profile.industry 기준으로 해석하므로 네트워크 없이 계산된다.
    """
    etfs: set[str] = set(ROTATION_ETFS) | set(_ALWAYS_FETCH)
    for e in (entries or []):
        links = resolve_links(e.get("symbol") or "", profile=e.get("profile"),
                              sector_etf=e.get("sector_etf"))["links"]
        for axis in links.values():
            etfs.update(axis)
    return {x: ETF_LABELS.get(x, x) for x in sorted(etfs)}


def _rsi(close: pd.Series, period: int = 14) -> float:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    ag = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    al = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = ag / al.replace(0, np.nan)
    out = (100 - 100 / (1 + rs)).fillna(100)
    s = out.dropna()
    return float(s.iloc[-1]) if len(s) else float("nan")


def _ma_alignment(close: pd.Series) -> tuple[str, float | None]:
    """현재가·20·60·120·200일선 배열 상태와 200일선 값.

    정배열: 가격>20>60>120>200, 역배열: 그 반대, 그 외 혼조.
    """
    price = float(close.iloc[-1])
    mas = [float(close.tail(p).mean()) for p in (20, 60, 120, 200)
           if len(close) >= p]
    ma200 = float(close.tail(200).mean()) if len(close) >= 200 else None
    seq = [price] + mas
    if len(seq) < 3:
        return "혼조", ma200
    if all(seq[i] > seq[i + 1] for i in range(len(seq) - 1)):
        return "정배열", ma200
    if all(seq[i] < seq[i + 1] for i in range(len(seq) - 1)):
        return "역배열", ma200
    return "혼조", ma200


def _ma_proximity(close: pd.Series, period: int) -> tuple[str | None, float | None]:
    """현재가의 N일선 대비 위치(상향/근접/터치/하향)와 이격도(%).

    이격도 = (현재가/N일선 - 1)*100. |이격|≤1% 터치, ≤3% 근접(상/하), 그 밖은
    상향/하향. 데이터가 N일 미만이면 (None, None).
    """
    if len(close) < period:
        return None, None
    ma = float(close.tail(period).mean())
    if not ma:
        return None, None
    dist = round(float(close.iloc[-1]) / ma - 1, 4) * 100
    if abs(dist) <= 1.0:
        pos = "터치"
    elif dist > 3.0:
        pos = "상향"
    elif dist > 1.0:
        pos = "근접(상)"
    elif dist < -3.0:
        pos = "하향"
    else:
        pos = "근접(하)"
    return pos, round(dist, 1)


def _vol_trend(vol: pd.Series, base: int) -> float | None:
    """최근 5일 평균 거래량이 직전 base거래일 평균 대비 증감(%).

    base=21(≈1개월)/42(≈2개월). 양수면 최근 거래량 증가(자금유입). 데이터 부족 시 None.
    """
    if len(vol) < base:
        return None
    recent = float(vol.tail(5).mean())
    avg = float(vol.tail(base).mean())
    if not avg:
        return None
    return round((recent / avg - 1) * 100, 1)


@dataclass
class SectorMetric:
    etf: str
    label: str
    chg_1d: float | None = None
    chg_5d: float | None = None
    chg_1m: float | None = None
    chg_3m: float | None = None       # 최근 3개월(63거래일) 수익률
    chg_20d: float | None = None
    chg_60d: float | None = None
    vol_ratio: float | None = None
    vol_chg_1m: float | None = None   # 최근 거래량 vs 1개월 평균 증감 %
    vol_chg_2m: float | None = None   # 최근 거래량 vs 2개월 평균 증감 %
    rsi: float | None = None
    rs: float | None = None           # SPY 대비 5일 상대강도
    rs_1w: float | None = None        # SPY 대비 1주 상대강도
    rs_1m: float | None = None        # SPY 대비 1개월 상대강도
    rs_3m: float | None = None        # SPY 대비 3개월 상대강도
    ma_alignment: str | None = None   # 정배열 / 역배열 / 혼조
    above_ma200: bool | None = None
    ma120_pos: str | None = None      # 120일선 대비 상향/근접(상)/터치/근접(하)/하향
    ma120_dist: float | None = None   # 120일선 이격도 %
    ma200_pos: str | None = None      # 200일선 대비 위치
    ma200_dist: float | None = None   # 200일선 이격도 %
    rank_5d: int | None = None        # 1=가장 강함
    total: int = 0


@dataclass
class SectorRotation:
    metrics: dict[str, SectorMetric] = field(default_factory=dict)
    top3: list[SectorMetric] = field(default_factory=list)
    bottom3: list[SectorMetric] = field(default_factory=list)
    defensive_strong: bool = False   # 방어섹터 강세 = 위험 신호


class SectorAnalyzer:
    """16개 섹터 ETF를 한 번에 분석하고 종목별 보정을 제공."""

    def __init__(self):
        self.metrics: dict[str, SectorMetric] = {}
        self.rotation = SectorRotation()
        self._loaded = False

    def load(self, etfs: dict[str, str] | None = None) -> SectorRotation:
        """ETF 메트릭 수집. etfs 미지정 시 전체 SECTOR_ETFS, 지정 시 그 집합만."""
        for etf, label in (etfs or SECTOR_ETFS).items():
            self.metrics[etf] = self._fetch(etf, label)
        # SPY 대비 상대강도 (1주/1개월/3개월)
        spy = self._spy_returns()
        for m in self.metrics.values():
            if spy.get("5d") is not None and m.chg_5d is not None:
                m.rs = m.rs_1w = round(m.chg_5d - spy["5d"], 2)
            if spy.get("21d") is not None and m.chg_1m is not None:
                m.rs_1m = round(m.chg_1m - spy["21d"], 2)
            if spy.get("63d") is not None and m.chg_3m is not None:
                m.rs_3m = round(m.chg_3m - spy["63d"], 2)
        self._rank()
        self._loaded = True
        self._save_cache()
        return self.rotation

    def _spy_returns(self) -> dict:
        """SPY의 5d/21d/63d 수익률(%). 실패 항목은 None."""
        out = {"5d": None, "21d": None, "63d": None}
        try:
            df = yf.Ticker("SPY").history(period="6mo", interval="1d",
                                          auto_adjust=True)
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            c = df["Close"].dropna()
            for key, lb in (("5d", 5), ("21d", 21), ("63d", 63)):
                if len(c) > lb:
                    out[key] = float(c.iloc[-1] / c.iloc[-1 - lb] - 1) * 100
        except Exception:  # noqa: BLE001
            pass
        return out

    def _save_cache(self) -> None:
        try:
            from cache import save_cache
            etfs = {m.etf: {
                "label": m.label, "chg_1d": m.chg_1d, "chg_5d": m.chg_5d,
                "chg_20d": m.chg_20d, "chg_60d": m.chg_60d,
                "vol_ratio": m.vol_ratio, "vol_chg_1m": m.vol_chg_1m,
                "vol_chg_2m": m.vol_chg_2m, "ma120_pos": m.ma120_pos,
                "ma120_dist": m.ma120_dist, "ma200_pos": m.ma200_pos,
                "ma200_dist": m.ma200_dist, "rsi": m.rsi, "rs": m.rs,
                "rank_5d": m.rank_5d, "total": m.total,
            } for m in self.metrics.values()}
            payload = {
                "etfs": etfs,
                "top3": [{"etf": s.etf, "label": s.label, "chg_5d": s.chg_5d}
                         for s in self.rotation.top3],
                "bottom3": [{"etf": s.etf, "label": s.label, "chg_5d": s.chg_5d}
                            for s in self.rotation.bottom3],
                "defensive_strong": self.rotation.defensive_strong,
            }
            save_cache("sector_latest.json", payload)
        except Exception as e:  # noqa: BLE001
            print(f"[섹터] 캐시 저장 실패: {e}")

    def _fetch(self, etf: str, label: str) -> SectorMetric:
        m = SectorMetric(etf=etf, label=label)
        try:
            # 200일 이동평균을 위해 1년치(≈252거래일) 수집
            df = yf.Ticker(etf).history(period="1y", interval="1d",
                                        auto_adjust=True)
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            close = df["Close"].dropna()
            vol = df["Volume"].dropna()
            if len(close) < 6:
                return m

            def ret(lb):
                return (float(close.iloc[-1] / close.iloc[-1 - lb] - 1) * 100
                        if len(close) > lb else None)

            m.chg_1d = ret(1)
            m.chg_5d = ret(5)
            m.chg_1m = ret(21)
            m.chg_3m = ret(63)
            m.chg_20d = ret(20)
            m.chg_60d = ret(60)
            if len(vol) >= 20:
                v20 = float(vol.tail(20).mean())
                m.vol_ratio = float(vol.iloc[-1] / v20) if v20 else None
            m.vol_chg_1m = _vol_trend(vol, 21)
            m.vol_chg_2m = _vol_trend(vol, 42)
            m.rsi = _rsi(close)
            m.ma_alignment, ma200 = _ma_alignment(close)
            if ma200 is not None:
                m.above_ma200 = bool(float(close.iloc[-1]) > ma200)
            m.ma120_pos, m.ma120_dist = _ma_proximity(close, 120)
            m.ma200_pos, m.ma200_dist = _ma_proximity(close, 200)
        except Exception as e:  # noqa: BLE001
            print(f"[섹터] {etf} 수집 실패: {e}")
        return m

    def _rank(self) -> None:
        ranked = sorted(
            [m for m in self.metrics.values() if m.chg_5d is not None],
            key=lambda x: x.chg_5d, reverse=True,
        )
        n = len(ranked)
        for i, m in enumerate(ranked):
            m.rank_5d = i + 1
            m.total = n
        self.rotation.metrics = self.metrics
        self.rotation.top3 = ranked[:3]
        self.rotation.bottom3 = ranked[-3:][::-1] if n >= 3 else ranked[::-1]
        # 방어섹터가 상위 1/3 안에 들면 위험 신호
        cutoff = max(1, n // 3)
        self.rotation.defensive_strong = any(
            self.metrics.get(d) and self.metrics[d].rank_5d is not None
            and self.metrics[d].rank_5d <= cutoff
            for d in DEFENSIVE_ETFS
        )

    def rotation_table(self) -> list[dict]:
        """GICS 11개 섹터의 리치 지표 리스트 (sector_llm 프롬프트용)."""
        if not self._loaded:
            self.load()
        out: list[dict] = []
        for etf, label in ROTATION_ETFS.items():
            m = self.metrics.get(etf)
            if m is None:
                continue
            out.append({
                "etf": etf, "label": label,
                "rs_1w": m.rs_1w, "rs_1m": m.rs_1m, "rs_3m": m.rs_3m,
                "chg_5d": m.chg_5d, "chg_1m": m.chg_1m, "chg_3m": m.chg_3m,
                "ma_alignment": m.ma_alignment, "above_ma200": m.above_ma200,
                "ma120_pos": m.ma120_pos, "ma120_dist": m.ma120_dist,
                "ma200_pos": m.ma200_pos, "ma200_dist": m.ma200_dist,
                "rsi": m.rsi, "vol_ratio": m.vol_ratio,
                "vol_chg_1m": m.vol_chg_1m, "vol_chg_2m": m.vol_chg_2m,
                "rank_5d": m.rank_5d, "total": m.total,
            })
        return out

    def adjustment(self, etf: str | None) -> tuple[float, SectorMetric | None]:
        """섹터 5일 수익률 순위 기반 ±5점 보정. (점수, 메트릭)."""
        if not etf or etf not in self.metrics:
            return 0.0, None
        m = self.metrics[etf]
        if m.rank_5d is None or m.total == 0:
            return 0.0, m
        pct = m.rank_5d / m.total          # 0~1, 작을수록 강함
        if pct <= 0.30:
            return 5.0, m
        if pct >= 0.70:
            return -5.0, m
        return 0.0, m

    def _brief(self, etf: str) -> dict:
        """ETF 한 개의 간략 지표(표기/순위용)."""
        m = self.metrics.get(etf)
        if m is None:
            return {"etf": etf, "label": ETF_LABELS.get(etf, etf),
                    "chg_5d": None, "rank_5d": None, "total": 0}
        return {"etf": etf, "label": m.label, "chg_1d": m.chg_1d,
                "chg_5d": m.chg_5d, "chg_1m": m.chg_1m,
                "vol_ratio": m.vol_ratio, "rsi": m.rsi,
                "rank_5d": m.rank_5d, "total": m.total}

    def for_etf(self, etf: str) -> dict:
        """ETF 자체를 섹터 객체로 (tradable_etf 채점용).

        업종(industry) 축 = ETF 자기 자신 → 섹터 컴포넌트가 그 ETF의 자기
        상대강도/순위로 매겨진다. market 축엔 SPY를 넣어 레짐 보정이 동작한다.
        반환 구조는 for_ticker와 동일(scorer/리포트 호환).
        """
        if not self._loaded:
            self.load()
        brief = self._brief(etf)
        adj, m = self.adjustment(etf)
        out = {
            "label": ETF_LABELS.get(etf, etf), "etf": etf,
            "country_code": "US", "adj": adj,
            "market_links": {"country": [], "industry": [brief],
                             "technology": [], "market": [self._brief("SPY")]},
        }
        if m is not None:
            out.update({
                "chg_1d": m.chg_1d, "chg_5d": m.chg_5d, "chg_1m": m.chg_1m,
                "vol_ratio": m.vol_ratio, "rsi": m.rsi,
                "rank_5d": m.rank_5d, "total": m.total,
            })
        return out

    def for_ticker(self, ticker: str, info: dict | None = None,
                   profile: dict | None = None,
                   sector_etf: str | None = None) -> dict:
        """종목의 4축 ETF 영향력 + 대표 업종 ETF 기반 보정 점수 dict.

        반환(scorer/llm_analyzer 호환): label, etf(대표 업종 ETF),
        country_code, adj, chg_1d/5d/1m·vol_ratio·rsi·rank_5d·total(대표 ETF 기준),
        market_links(4축별 ETF 간략지표 리스트). 매핑이 없어도 market=SPY는 채워짐.
        """
        if not self._loaded:
            self.load()
        resolved = resolve_links(ticker, info, profile, sector_etf)
        links, label = resolved["links"], resolved["label"]
        primary = links["industry"][0] if links["industry"] else None
        adj, m = self.adjustment(primary)

        enriched = {cat: [self._brief(e) for e in etfs]
                    for cat, etfs in links.items()}
        out = {
            "label": label or (ETF_LABELS.get(primary, primary)
                               if primary else "기타"),
            "etf": primary, "country_code": resolved["country_code"],
            "adj": adj, "market_links": enriched,
        }
        if m is not None:
            out.update({
                "chg_1d": m.chg_1d, "chg_5d": m.chg_5d, "chg_1m": m.chg_1m,
                "vol_ratio": m.vol_ratio, "rsi": m.rsi,
                "rank_5d": m.rank_5d, "total": m.total,
            })
        return out
