"""섹터 ETF 강도 & 로테이션 분석 (탑다운 2단계).

전체 16개 섹터 ETF의 1일/5일/1개월 수익률, 거래량 비율, RSI(14)를 계산해
자금 유입/유출 섹터를 판별한다. 각 종목에는 소속 섹터의 5일 수익률 순위에
따라 ±5점 보정을 적용한다(상위 30% +5 / 하위 30% -5).

방어섹터(XLU 유틸리티, XLP 필수소비재) 강세는 시장 위험 신호로 해석한다.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd
import yfinance as yf

# ETF 심볼 -> 한글 라벨 (탑다운 전체 섹터)
SECTOR_ETFS: dict[str, str] = {
    "XLK": "기술",
    "SOXX": "반도체",
    "XLV": "헬스케어",
    "XBI": "바이오",
    "XLE": "에너지",
    "XLF": "금융",
    "XLI": "산업재",
    "ROBO": "로봇/AI",
    "ITA": "우주/방산",
    "ITB": "건설",
    "XLU": "유틸리티",
    "XLP": "필수소비재",
    "XLY": "임의소비재",
    "XLB": "소재",
    "XLRE": "부동산",
    "XLC": "통신",
    # 세분화 테마/산업 ETF (yfinance industry 자동 매핑 대상)
    "IGV": "소프트웨어",
    "QTUM": "양자/차세대컴퓨팅",
}

DEFENSIVE_ETFS = ("XLU", "XLP")

# 워치리스트에 섹터 정보가 없을 때 쓰는 폴백: 티커 직접 매핑
TICKER_ETF: dict[str, str] = {
    "NVDA": "SOXX", "AMD": "SOXX", "INTC": "SOXX", "TSM": "SOXX",
    "AVGO": "SOXX", "MU": "SOXX", "ASML": "SOXX", "ARM": "SOXX", "SMCI": "SOXX",
    "AAPL": "XLK", "MSFT": "XLK", "ORCL": "XLK", "CRM": "XLK", "ADBE": "XLK",
    "GOOGL": "XLC", "GOOG": "XLC", "META": "XLC", "NFLX": "XLC",
    "AMZN": "XLY", "TSLA": "XLY", "HD": "XLY", "NKE": "XLY",
    "ISRG": "ROBO", "IONQ": "XLK",
    "LMT": "ITA", "RTX": "ITA", "NOC": "ITA", "BA": "ITA", "PLTR": "ITA",
    "LLY": "XLV", "UNH": "XLV", "PFE": "XLV", "JNJ": "XLV",
    "MRNA": "XBI", "VRTX": "XBI", "REGN": "XBI",
    "XOM": "XLE", "CVX": "XLE", "COP": "XLE",
    "JPM": "XLF", "BAC": "XLF", "GS": "XLF", "V": "XLF", "MA": "XLF",
    "DHI": "ITB", "LEN": "ITB", "CAT": "XLI", "GE": "XLI",
}

# yfinance industry/sector 키워드 -> ETF (자동 분류)
# 구체적인 키워드를 먼저(위에) 둬 가장 세분화된 ETF로 라우팅한다.
KEYWORD_ETF: list[tuple[str, str]] = [
    # 반도체 (가장 구체)
    ("semiconductor", "SOXX"),
    # 소프트웨어 / 데이터 (Software - Infrastructure/Application 등)
    ("software", "IGV"),
    ("information technology services", "IGV"),
    # 헬스케어/바이오
    ("biotech", "XBI"),
    ("drug manufacturer", "XLV"),
    ("pharmaceutical", "XLV"),
    ("health", "XLV"),
    ("medical", "XLV"),
    # 에너지
    ("oil", "XLE"), ("gas", "XLE"), ("energy", "XLE"),
    # 우주/방산
    ("aerospace", "ITA"), ("defense", "ITA"),
    # 금융
    ("bank", "XLF"), ("financial", "XLF"), ("insurance", "XLF"),
    ("capital market", "XLF"), ("asset management", "XLF"),
    # 통신/미디어/인터넷
    ("communication", "XLC"), ("entertainment", "XLC"),
    ("internet content", "XLC"), ("telecom", "XLC"),
    # 소비
    ("consumer electronic", "XLK"),
    ("retail", "XLY"), ("auto", "XLY"), ("apparel", "XLY"),
    ("consumer cyclical", "XLY"), ("restaurant", "XLY"),
    ("consumer defensive", "XLP"), ("beverage", "XLP"),
    ("food", "XLP"), ("household", "XLP"), ("tobacco", "XLP"),
    # 유틸/부동산/소재/산업/건설
    ("utilit", "XLU"),
    ("real estate", "XLRE"), ("reit", "XLRE"),
    ("material", "XLB"), ("chemical", "XLB"), ("mining", "XLB"),
    ("steel", "XLB"), ("gold", "XLB"),
    ("construction", "ITB"), ("homebuild", "ITB"),
    ("residential construction", "ITB"),
    ("industrial", "XLI"), ("machinery", "XLI"), ("railroad", "XLI"),
    ("robot", "ROBO"),
    # 그 외 하드웨어/기술 일반은 광범위 기술로 (최후 폴백)
    ("computer hardware", "XLK"),
    ("electronic", "XLK"),
    ("technology", "XLK"),
]


def resolve_etf(ticker: str, info: dict | None = None,
                sector_etf: str | None = None) -> str | None:
    """티커의 대표 섹터 ETF를 결정.

    우선순위:
      1) watchlist 명시(sector_etf)  — 사용자가 직접 지정한 테마(예: 양자=QTUM)
      2) yfinance industry/sector 키워드 자동 분류  — API 기반(권장)
      3) 티커 직접매핑(TICKER_ETF)  — industry가 비거나 매칭 실패 시 폴백
    """
    if sector_etf and sector_etf in SECTOR_ETFS:
        return sector_etf
    text = " ".join(str((info or {}).get(k, "")).lower()
                    for k in ("industry", "sector"))
    for kw, etf in KEYWORD_ETF:
        if kw in text:
            return etf
    if ticker in TICKER_ETF:
        return TICKER_ETF[ticker]
    return None


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


@dataclass
class SectorMetric:
    etf: str
    label: str
    chg_1d: float | None = None
    chg_5d: float | None = None
    chg_1m: float | None = None
    chg_20d: float | None = None
    chg_60d: float | None = None
    vol_ratio: float | None = None
    rsi: float | None = None
    rs: float | None = None          # SPY 대비 5일 상대강도
    rank_5d: int | None = None      # 1=가장 강함
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

    def load(self) -> SectorRotation:
        for etf, label in SECTOR_ETFS.items():
            self.metrics[etf] = self._fetch(etf, label)
        # SPY 대비 상대강도 (5일)
        spy_5d = self._spy_5d()
        if spy_5d is not None:
            for m in self.metrics.values():
                if m.chg_5d is not None:
                    m.rs = round(m.chg_5d - spy_5d, 2)
        self._rank()
        self._loaded = True
        self._save_cache()
        return self.rotation

    def _spy_5d(self) -> float | None:
        try:
            df = yf.Ticker("SPY").history(period="1mo", interval="1d",
                                          auto_adjust=True)
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            c = df["Close"].dropna()
            return float(c.iloc[-1] / c.iloc[-6] - 1) * 100 if len(c) > 5 else None
        except Exception:  # noqa: BLE001
            return None

    def _save_cache(self) -> None:
        try:
            from cache import save_cache
            etfs = {m.etf: {
                "label": m.label, "chg_1d": m.chg_1d, "chg_5d": m.chg_5d,
                "chg_20d": m.chg_20d, "chg_60d": m.chg_60d,
                "vol_ratio": m.vol_ratio, "rsi": m.rsi, "rs": m.rs,
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
            df = yf.Ticker(etf).history(period="6mo", interval="1d",
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
            m.chg_20d = ret(20)
            m.chg_60d = ret(60)
            if len(vol) >= 20:
                v20 = float(vol.tail(20).mean())
                m.vol_ratio = float(vol.iloc[-1] / v20) if v20 else None
            m.rsi = _rsi(close)
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

    def for_ticker(self, ticker: str, info: dict | None = None,
                   sector_etf: str | None = None) -> dict | None:
        """종목 소속 섹터 정보 + 보정 점수 dict. 못 찾으면 None."""
        if not self._loaded:
            self.load()
        etf = resolve_etf(ticker, info, sector_etf)
        if etf is None:
            return None
        adj, m = self.adjustment(etf)
        if m is None:
            return {"label": SECTOR_ETFS.get(etf, etf), "etf": etf,
                    "chg_5d": None, "adj": 0.0}
        return {
            "label": m.label, "etf": m.etf,
            "chg_1d": m.chg_1d, "chg_5d": m.chg_5d, "chg_1m": m.chg_1m,
            "vol_ratio": m.vol_ratio, "rsi": m.rsi,
            "rank_5d": m.rank_5d, "total": m.total, "adj": adj,
        }
