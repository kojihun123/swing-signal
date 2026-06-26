"""기술적 분석 (지표 + 패턴 인식 + 0~100 점수).

배점: 추세 35 + 모멘텀 30 + 거래량 20 + 패턴 15
외부 TA 라이브러리 없이 pandas/numpy로 직접 구현.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

# ---------- 지표 ----------


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    ag = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    al = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = ag / al.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(100)


def macd(close: pd.Series):
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    line = ema12 - ema26
    signal = line.ewm(span=9, adjust=False).mean()
    return line, signal, line - signal


def bollinger(close: pd.Series, period: int = 20, k: float = 2.0):
    mid = close.rolling(period).mean()
    std = close.rolling(period).std()
    upper, lower = mid + k * std, mid - k * std
    width = (upper - lower)
    pctb = (close - lower) / width.replace(0, np.nan)
    return mid, upper, lower, pctb, width


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high, low, close = df["High"], df["Low"], df["Close"]
    pc = close.shift(1)
    tr = pd.concat([high - low, (high - pc).abs(), (low - pc).abs()],
                   axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()


def _last(s: pd.Series, default=np.nan):
    s = s.dropna()
    return float(s.iloc[-1]) if len(s) else default


def _ret(close: pd.Series, lb: int):
    c = close.dropna()
    return float(c.iloc[-1] / c.iloc[-1 - lb] - 1) * 100 if len(c) > lb else None


# ---------- 패턴 ----------


def detect_patterns(df: pd.DataFrame, ind: dict) -> list[str]:
    pats = []
    close = df["Close"].dropna()
    if len(close) < 30:
        return pats
    price = ind["price"]

    # 신고가 돌파: 52주 고가(최근 252봉) ±2% 이내
    win = close.tail(252)
    hi52 = float(win.max())
    if hi52 and price >= hi52 * 0.98:
        pats.append("신고가 돌파")

    # 갭 상승: 당일 시가가 전일 종가 대비 +2% 이상
    if "Open" in df.columns and len(close) >= 2:
        open_t = float(df["Open"].dropna().iloc[-1])
        close_p = float(close.iloc[-2])
        if close_p and open_t / close_p - 1 >= 0.02:
            pats.append("갭상승")

    # 골든/데드 크로스: MA20 vs MA50 최근 3봉 내 교차
    ma20 = close.rolling(20).mean()
    ma50 = close.rolling(50).mean()
    if ma20.notna().sum() > 3 and ma50.notna().sum() > 3:
        diff = (ma20 - ma50).dropna()
        if len(diff) >= 4:
            recent = diff.iloc[-3:]
            prev = diff.iloc[-4]
            if (prev <= 0) and (recent.iloc[-1] > 0):
                pats.append("골든크로스")
            elif (prev >= 0) and (recent.iloc[-1] < 0):
                pats.append("데드크로스")

    # 볼린저 스퀴즈: 밴드폭이 최근 20일 최저
    width = ind.get("bb_width_series")
    if width is not None:
        w = width.dropna()
        if len(w) >= 20 and w.iloc[-1] <= w.tail(20).min() * 1.001:
            pats.append("볼린저 스퀴즈")

    # 거래량 급증: 20일 평균 2배 이상
    vr = ind.get("vol_ratio")
    if vr is not None and vr >= 2.0:
        pats.append("거래량 급증")

    return pats


# ---------- 본체 ----------


def analyze_technical(daily: pd.DataFrame, weekly: pd.DataFrame | None = None,
                      bench_daily: pd.DataFrame | None = None) -> dict:
    out = {"score": 0.0, "parts": {}, "indicators": {}, "patterns": []}
    if daily is None or daily.empty:
        return out

    close = daily["Close"]
    price = _last(close)
    change_pct = _ret(close, 1)

    rsi_d = _last(rsi(close))
    rsi_w = _last(rsi(weekly["Close"])) if (weekly is not None and not weekly.empty) else np.nan
    ml_s, ms_s, mh_s = macd(close)
    ml, ms, mh = _last(ml_s), _last(ms_s), _last(mh_s)
    mh_prev = float(mh_s.dropna().iloc[-2]) if len(mh_s.dropna()) >= 2 else np.nan

    mid, upper, lower, pctb, width = bollinger(close)
    bb_pctb = _last(pctb)
    atr14 = _last(atr(daily))

    ma20 = _last(close.rolling(20).mean())
    ma50 = _last(close.rolling(50).mean())
    ma60 = _last(close.rolling(60).mean())
    ma120 = _last(close.rolling(120).mean())
    ma200 = _last(close.rolling(200).mean())

    vol = daily["Volume"] if "Volume" in daily.columns else pd.Series(dtype=float)
    vol20 = _last(vol.rolling(20).mean()) if not vol.empty else np.nan
    vol_ratio = (_last(vol) / vol20) if (vol20 and vol20 == vol20) else None

    # VWAP 근사 (최근 20봉 거래량 가중 평균가)
    vwap = None
    if not vol.empty and "High" in daily.columns:
        tp = (daily["High"] + daily["Low"] + daily["Close"]) / 3
        tail = pd.concat([tp, vol], axis=1).dropna().tail(20)
        if not tail.empty and tail.iloc[:, 1].sum():
            vwap = float((tail.iloc[:, 0] * tail.iloc[:, 1]).sum()
                         / tail.iloc[:, 1].sum())

    # MACD 상태
    if mh == mh and mh > 0 and (mh_prev != mh_prev or mh_prev <= 0):
        macd_state = "골든크로스"
    elif mh == mh and mh < 0 and (mh_prev != mh_prev or mh_prev >= 0):
        macd_state = "데드크로스"
    elif mh == mh and mh < 0 and mh_prev == mh_prev and mh > mh_prev:
        macd_state = "골든크로스 임박"
    elif mh == mh and mh > 0:
        macd_state = "상승 유지"
    else:
        macd_state = "하락 유지"

    aligned = all(x == x for x in (ma20, ma50, ma200)) and ma20 > ma50 > ma200

    rs = None
    if bench_daily is not None and not bench_daily.empty:
        r1 = _ret(close, 21)
        b1 = _ret(bench_daily["Close"], 21)
        if r1 is not None and b1 is not None:
            rs = r1 - b1

    ind = {
        "price": price, "change_pct": change_pct,
        "rsi_d": rsi_d, "rsi_w": rsi_w,
        "macd_state": macd_state, "macd_line": ml, "macd_sig": ms,
        "bb_pctb": bb_pctb, "bb_width_series": width,
        "atr14": atr14, "vwap": vwap,
        "ma20": ma20, "ma50": ma50, "ma60": ma60, "ma120": ma120, "ma200": ma200,
        "aligned": aligned, "vol_ratio": vol_ratio, "rs": rs,
        "ret_5d": _ret(close, 5), "ret_20d": _ret(close, 20),
        "ret_60d": _ret(close, 60),
    }
    patterns = detect_patterns(daily, ind)
    ind.pop("bb_width_series", None)  # 직렬화 제외

    # ---- 점수 ----
    trend = _score_trend(ind, price)
    momentum = _score_momentum(ind)
    volume = _score_volume(vol_ratio)
    pattern = _score_pattern(patterns)
    score = round(trend + momentum + volume + pattern, 1)

    out.update({
        "score": min(score, 100.0),
        "parts": {"trend": round(trend, 1), "momentum": round(momentum, 1),
                  "volume": round(volume, 1), "pattern": round(pattern, 1)},
        "indicators": ind,
        "patterns": patterns,
    })
    return out


def _score_trend(ind: dict, price: float) -> float:
    s = 0.0
    if ind["aligned"]:
        s += 18
    # 가격이 주요 MA 위
    for ma in ("ma20", "ma50", "ma200"):
        v = ind.get(ma)
        if v == v and v and price >= v:
            s += 4
    # 장기 MA 대비 위치
    ma200 = ind.get("ma200")
    if ma200 == ma200 and ma200:
        if price >= ma200 * 1.05:
            s += 5
    return min(s, 35)


def _score_momentum(ind: dict) -> float:
    s = 0.0
    rd = ind["rsi_d"]
    if rd == rd:
        if rd < 30:
            s += 10
        elif rd < 45:
            s += 12
        elif rd < 60:
            s += 14
        elif rd < 70:
            s += 9
        else:
            s += 3
    else:
        s += 7
    s += {"골든크로스": 16, "상승 유지": 13, "골든크로스 임박": 11,
          "하락 유지": 4, "데드크로스": 0}.get(ind["macd_state"], 7)
    return min(s, 30)


def _score_volume(vr) -> float:
    if vr is None or vr != vr:
        return 10.0
    if vr >= 2.0:
        return 20.0
    if vr >= 1.5:
        return 16.0
    if vr >= 1.0:
        return 12.0
    if vr >= 0.7:
        return 8.0
    return 4.0


def _score_pattern(patterns: list[str]) -> float:
    bullish = {"신고가 돌파": 6, "골든크로스": 6, "갭상승": 4,
               "거래량 급증": 4, "볼린저 스퀴즈": 3}
    bearish = {"데드크로스": -6}
    s = 7.0  # 중립 기준
    for p in patterns:
        s += bullish.get(p, 0) + bearish.get(p, 0)
    return max(0.0, min(s, 15.0))
