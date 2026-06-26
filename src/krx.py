"""한국거래소(KRX) 공매도 데이터 레이어 — 무인증 JSON.

네이버증권 '공매도' 탭은 data.krx.co.kr 위젯(MDCSTAT300)을 iframe으로 임베드한
것이라, 동일 데이터를 KRX 데이터포털 API에서 직접 가져온다.

  POST data.krx.co.kr/comm/bldAttendant/getJsonData.cmd
  · 6자리코드 → KR 표준코드   bld=dbms/comm/finder/get_srtisu (예 005930→KR7005930003)
  · 공매도 거래추이           bld=dbms/MDC_OUT/STAT/srt/MDCSTAT30001_OUT

bld의 '_OUT' 접미사가 임베드(무인증)용. 일반 코드는 로그인 필요('LOGOUT' 반환).
KR 종목(.KS/.KQ)만 대상이며 모든 실패는 None/[]로 폴백한다.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

from cache import load_data, save_cache

KST = ZoneInfo("Asia/Seoul")
_EP = "https://data.krx.co.kr/comm/bldAttendant/getJsonData.cmd"
_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"),
    "Referer": "https://data.krx.co.kr/comm/srt/srtLoader/index.cmd?screenId=MDCSTAT300",
    "X-Requested-With": "XMLHttpRequest",
}
_ISU_CACHE = "krx_isucodes.json"   # 6자리 → KR 표준코드 (영속)


def _is_kr(symbol: str) -> bool:
    return symbol.upper().endswith((".KS", ".KQ"))


def _code(symbol: str) -> str:
    return symbol.split(".")[0]


def _post(data: dict, timeout: float = 10.0):
    try:
        r = requests.post(_EP, data=data, headers=_HEADERS, timeout=timeout)
        r.raise_for_status()
        txt = r.text.strip()
        if not txt or txt == "LOGOUT":
            return None
        return r.json()
    except Exception as e:  # noqa: BLE001
        print(f"[KRX] 요청 실패: {e}")
        return None


def _num(v):
    if v in (None, "", "-"):
        return None
    try:
        return float(str(v).replace(",", "").strip())
    except (ValueError, TypeError):
        return None


def isu_code(symbol: str) -> str | None:
    """6자리 종목코드 → KRX 표준코드(KR7005930003). finder 1회 호출 후 캐시."""
    if not _is_kr(symbol):
        return None
    code = _code(symbol)
    cache = load_data(_ISU_CACHE) or {}
    if code in cache:
        return cache[code] or None
    j = _post({"bld": "dbms/comm/finder/get_srtisu", "locale": "ko_KR",
               "isuCd": code})
    out = (j or {}).get("output") if isinstance(j, dict) else None
    full = None
    if isinstance(out, list):
        for it in out:
            if isinstance(it, dict) and it.get("code"):
                # tbox '005930/삼성전자'에서 6자리 일치 우선
                if str(it.get("tbox", "")).startswith(code) or len(out) == 1:
                    full = it.get("code")
                    break
    cache[code] = full or ""
    try:
        save_cache(_ISU_CACHE, cache)
    except Exception:  # noqa: BLE001
        pass
    return full


def short_selling(symbol: str, days: int = 20) -> list[dict]:
    """일별 공매도 거래추이 (KRX MDCSTAT30001).

    [{date, short_volume(공매도 거래량), short_value(공매도 거래대금),
      uptick_volume, uptick_excl_volume}]  최신순.
    US 종목·실패는 [].
    """
    if not _is_kr(symbol):
        return []
    isu = isu_code(symbol)
    if not isu:
        return []
    today = datetime.now(KST).date()
    start = today - timedelta(days=max(days, 1) * 2 + 10)  # 거래일 여유분
    j = _post({
        "bld": "dbms/MDC_OUT/STAT/srt/MDCSTAT30001_OUT",
        "locale": "ko_KR", "isuCd": isu,
        "strtDd": start.strftime("%Y%m%d"), "endDd": today.strftime("%Y%m%d"),
        "share": "1", "money": "1", "csvxls_isNo": "false",
    })
    rows = (j or {}).get("OutBlock_1") if isinstance(j, dict) else None
    if not isinstance(rows, list):
        return []
    out: list[dict] = []
    for it in rows:
        if not isinstance(it, dict):
            continue
        try:
            d = datetime.strptime(str(it.get("TRD_DD")), "%Y/%m/%d").date()
        except (ValueError, TypeError):
            continue
        out.append({
            "date": d.isoformat(),
            "short_volume": _num(it.get("CVSRTSELL_TRDVOL")),
            "short_value": _num(it.get("CVSRTSELL_TRDVAL")),
            "uptick_volume": _num(it.get("UPTICKRULE_APPL_TRDVOL")),
            "uptick_excl_volume": _num(it.get("UPTICKRULE_EXCPT_TRDVOL")),
        })
    out.sort(key=lambda r: r["date"], reverse=True)
    return out[:days]
