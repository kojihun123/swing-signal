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


def divergence(close: pd.Series, ind: pd.Series, bullish: bool = False,
               lookback: int = 40) -> bool:
    """다이버전스. bullish=True: 가격 저점↓(LL)+지표 저점↑(HH) → 바닥신호.
    bullish=False: 가격 고점↑(HH)+지표 고점↓(LH) → 과열신호.

    최근 구간을 둘로 나눠 각 구간의 가격 극점과 그 시점 지표값을 비교한다.
    """
    c = close.dropna()
    if len(c) < lookback or ind.dropna().empty:
        return False
    c = c.tail(lookback)
    s = ind.reindex(c.index)
    half = len(c) // 2
    c1, c2 = c.iloc[:half], c.iloc[half:]
    if c1.empty or c2.empty:
        return False
    if bullish:
        p1, p2 = c1.idxmin(), c2.idxmin()
        if not (c2[p2] < c1[p1]):
            return False
        s1, s2 = s.get(p1), s.get(p2)
        return s1 == s1 and s2 == s2 and s2 > s1
    p1, p2 = c1.idxmax(), c2.idxmax()
    if not (c2[p2] > c1[p1]):
        return False
    s1, s2 = s.get(p1), s.get(p2)
    return s1 == s1 and s2 == s2 and s2 < s1


def bb_overheat(df: pd.DataFrame, upper: pd.Series, lookback: int = 5) -> bool:
    """볼린저 매도신호: 최근 N봉 내 상단밴드 이탈 후 밴드 안 음봉 재진입 → 단기 고점."""
    c = df["Close"].dropna()
    if len(c) < 2 or "Open" not in df.columns:
        return False
    tail = c.tail(lookback + 1)
    u = upper.reindex(tail.index)
    pierced = any(tail.iloc[i] > u.iloc[i]
                  for i in range(len(tail) - 1) if u.iloc[i] == u.iloc[i])
    last_u = u.iloc[-1]
    now_inside = last_u != last_u or tail.iloc[-1] <= last_u
    open_t = float(df["Open"].dropna().iloc[-1])
    return bool(pierced and now_inside and open_t > tail.iloc[-1])


def bb_bounce(df: pd.DataFrame, lower: pd.Series, lookback: int = 5) -> bool:
    """볼린저 매수신호: 최근 N봉 내 하단밴드 터치 후 밴드 안 양봉 반등 → 단기 바닥."""
    c = df["Close"].dropna()
    if len(c) < 2 or "Open" not in df.columns:
        return False
    tail = c.tail(lookback + 1)
    lo = lower.reindex(tail.index)
    touched = any(tail.iloc[i] <= lo.iloc[i]
                  for i in range(len(tail) - 1) if lo.iloc[i] == lo.iloc[i])
    last_lo = lo.iloc[-1]
    now_inside = last_lo != last_lo or tail.iloc[-1] >= last_lo
    open_t = float(df["Open"].dropna().iloc[-1])
    return bool(touched and now_inside and tail.iloc[-1] > open_t)


def ichimoku(df: pd.DataFrame):
    """일목균형표. (전환선, 기준선, 선행스팬A, 선행스팬B, 구름상단, 구름하단).

    구름(선행스팬)은 26봉 선행이라, 현재가와 비교할 구름은 26봉 전 값으로 산출한다.
    """
    high, low = df["High"], df["Low"]
    conv = (high.rolling(9).max() + low.rolling(9).min()) / 2
    base = (high.rolling(26).max() + low.rolling(26).min()) / 2
    span_a = (conv + base) / 2                       # 미래 26봉으로 투영되는 값
    span_b = (high.rolling(52).max() + low.rolling(52).min()) / 2
    # 현재 시점에 걸린 구름 = 26봉 전에 산출된 선행스팬
    a_now = _last(span_a.shift(26))
    b_now = _last(span_b.shift(26))
    top = max(a_now, b_now) if (a_now == a_now and b_now == b_now) else np.nan
    bot = min(a_now, b_now) if (a_now == a_now and b_now == b_now) else np.nan
    return _last(conv), _last(base), a_now, b_now, top, bot


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

    # ── 매수/매도 시그널 (RSI·볼린저·MACD·일목 + 주봉 과열) ──
    rsi_s = rsi(close)
    rsi_prev = (float(rsi_s.dropna().iloc[-2])
                if len(rsi_s.dropna()) >= 2 else np.nan)
    conv, base, ich_a, ich_b, cloud_top, cloud_bot = ichimoku(daily)
    prev_close = float(close.dropna().iloc[-2]) if len(close.dropna()) >= 2 else np.nan
    ich_pos = ("구름 위" if (cloud_top == cloud_top and price > cloud_top)
               else "구름 아래" if (cloud_bot == cloud_bot and price < cloud_bot)
               else "구름 속" if cloud_top == cloud_top else None)

    buy_sig: list[str] = []
    sell_sig: list[str] = []
    # RSI: 과매도 탈출 / 과열 이탈 / 다이버전스
    if rsi_prev == rsi_prev and rsi_prev < 30 <= rsi_d:
        buy_sig.append("RSI 과매도 탈출")
    if rsi_prev == rsi_prev and rsi_d < 70 <= rsi_prev:
        sell_sig.append("RSI 과열 이탈")
    if divergence(close, rsi_s, bullish=True):
        buy_sig.append("RSI 상승 다이버전스")
    if divergence(close, rsi_s) or divergence(close, mh_s):
        sell_sig.append("하락 다이버전스")
    # 볼린저: 하단 반등 / 상단 과열복귀
    if bb_bounce(daily, lower):
        buy_sig.append("볼린저 하단 반등")
    if bb_overheat(daily, upper):
        sell_sig.append("볼린저 상단 과열")
    # MACD: 0선 아래 골든크로스 / 0선 위 데드크로스
    if macd_state == "골든크로스" and ml == ml and ml < 0:
        buy_sig.append("MACD 저점 골든크로스")
    if macd_state == "데드크로스" and ml == ml and ml > 0:
        sell_sig.append("MACD 고점 데드크로스")
    # 일목균형표: 구름 상향돌파 / 하향이탈
    if (cloud_top == cloud_top and prev_close == prev_close
            and price > cloud_top >= prev_close):
        buy_sig.append("일목 구름 상향돌파")
    if (cloud_bot == cloud_bot and prev_close == prev_close
            and price < cloud_bot <= prev_close):
        sell_sig.append("일목 구름 하향이탈")
    # 주봉 과열 (강한 경고) — 주봉 RSI 과열 또는 주봉 볼린저 상단 이탈
    weekly_overheat = False
    if weekly is not None and not weekly.empty and len(weekly) >= 20:
        w_rsi = _last(rsi(weekly["Close"]))
        _, _, _, w_pctb_s, _ = bollinger(weekly["Close"])
        w_pctb = _last(w_pctb_s)
        if (w_rsi == w_rsi and w_rsi >= 70) or (w_pctb == w_pctb and w_pctb >= 1.0):
            weekly_overheat = True
            sell_sig.append("주봉 과열")

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
        "ichimoku_pos": ich_pos, "weekly_overheat": weekly_overheat,
        "buy_signals": buy_sig, "sell_signals": sell_sig,
    }
    patterns = detect_patterns(daily, ind)
    ind.pop("bb_width_series", None)  # 직렬화 제외

    # ---- 점수 ----
    trend = _score_trend(ind, price)
    momentum = _score_momentum(ind)
    volume = _score_volume(vol_ratio)
    pattern = _score_pattern(patterns)
    # 시그널 보정: 매수 +2/개(최대+8), 매도(과열) -3/개(최대-12)
    signal_adj = min(8, 2 * len(buy_sig)) - min(12, 3 * len(sell_sig))
    score = round(trend + momentum + volume + pattern + signal_adj, 1)
    score = max(0.0, min(score, 100.0))

    out.update({
        "score": score,
        "parts": {"trend": round(trend, 1), "momentum": round(momentum, 1),
                  "volume": round(volume, 1), "pattern": round(pattern, 1),
                  "signal": signal_adj},
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
