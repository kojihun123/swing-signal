"""LLM 기반 섹터 로테이션 분석 (탑다운 2단계 보강).

GICS 11개 섹터 ETF(sector.ROTATION_ETFS)의 상대강도·추세(이동평균 배열)·
모멘텀·거래량 + 현재 거시환경을 LLM에 넘겨 섹터별 투자매력도를 평가한다.

  run(analyzer, macro_dict) → {ETF: {rating, reason, risk, outlook}, ...}

거시·종목과 동일하게 하루 1회 호출(전 종목 공유)하며, 결과는
cache/sector_rotation.json에 저장한다. LLM 키 없거나 실패 시 None.
"""
from __future__ import annotations

import llm_analyzer

RATINGS = ("Strong Bullish", "Bullish", "Neutral", "Bearish", "Strong Bearish")
OUTLOOKS = ("Positive", "Neutral", "Negative")


def llm_enabled() -> bool:
    return llm_analyzer.llm_enabled()


def _s(v, d: int = 1, suf: str = "") -> str:
    try:
        return f"{float(v):.{d}f}{suf}"
    except (TypeError, ValueError):
        return "N/A"


def _macro_context(macro: dict | None) -> str:
    """섹터 적합도 판단용 거시 요약 1줄."""
    m = (macro or {}).get("metrics", {})
    if not m:
        return "(거시 데이터 없음)"
    bits = [
        f"기준금리 {_s(m.get('fedfunds'),2,'%')}",
        f"10년물 {_s(m.get('y10'),2,'%')}({m.get('y10_trend','')})",
        f"CPI {_s(m.get('cpi_yoy'),1,'%')}",
        f"달러 {_s(m.get('dxy'),1)}({m.get('dxy_trend','')})",
        f"WTI {_s(m.get('wti'),1)}",
        f"하이일드 {_s(m.get('hy_spread'),2,'%')}",
        f"VIX {_s(m.get('vix'),1)}",
    ]
    label = (macro or {}).get("label", "")
    return f"환경 {label} · " + ", ".join(bits)


def _row_line(r: dict) -> str:
    ma = r.get("ma_alignment") or "N/A"
    above = ("200일선↑" if r.get("above_ma200")
             else "200일선↓" if r.get("above_ma200") is False else "")
    rank = (f"{r['rank_5d']}/{r['total']}"
            if r.get("rank_5d") and r.get("total") else "N/A")
    return (
        f"- {r['label']}({r['etf']}): "
        f"SP500대비 1주 {_s(r.get('rs_1w'),1)}/1개월 {_s(r.get('rs_1m'),1)}/"
        f"3개월 {_s(r.get('rs_3m'),1)} · "
        f"수익률 5일 {_s(r.get('chg_5d'),1,'%')}/1개월 {_s(r.get('chg_1m'),1,'%')}/"
        f"3개월 {_s(r.get('chg_3m'),1,'%')} · "
        f"MA {ma} {above} · RSI {_s(r.get('rsi'),0)} · "
        f"거래량 {_s(r.get('vol_ratio'),1,'배')} · 5일순위 {rank}"
    )


def build_prompt(rows: list[dict], macro: dict | None) -> str:
    table = "\n".join(_row_line(r) for r in rows)
    schema = ",\n".join(
        f'  "{r["etf"]}": {{"rating": "", "reason": "", "risk": "", "outlook": ""}}'
        for r in rows)
    return f"""당신은 글로벌 주식시장의 섹터 로테이션(Sector Rotation) 분석 전문가입니다.
현재 시장에서 어떤 섹터가 강세인지 판단하고, 각 섹터의 투자 매력도를 평가하세요.

[현재 거시환경]
{_macro_context(macro)}
참고: 금리 하락→기술/반도체/성장주 우호, 금리 상승→금융 우호, 유가 상승→에너지 우호,
경기침체→필수소비재/유틸리티 우호, 경기확장→산업재/소비재 우호, 달러 강세→소재/원자재 불리.

[섹터 ETF 데이터] (SP500대비=상대강도, MA=이동평균 배열)
{table}

각 항목을 종합적으로 고려해 평가하세요:
① 상대강도(SP500 대비 1주/1개월/3개월) ② 추세(이동평균 배열) ③ 모멘텀(상승률·지속)
④ 거래량(평균 대비 증가·자금유입) ⑤ 거시 적합도

아래 JSON 형식으로만 출력하세요. 모든 문자열은 자연스러운 한국어로 작성하되,
rating은 영문 5단계(Strong Bullish/Bullish/Neutral/Bearish/Strong Bearish),
outlook은 영문 3단계(Positive/Neutral/Negative)로만 작성하세요.
- rating: 현재 상태  - reason: 강세 이유  - risk: 약세 위험요인  - outlook: 향후 전망
{{
{schema}
}}"""


def _normalize(data: dict, rows: list[dict]) -> dict | None:
    if not isinstance(data, dict):
        return None
    out: dict = {}
    for r in rows:
        etf = r["etf"]
        d = data.get(etf)
        if not isinstance(d, dict):
            continue
        rating = str(d.get("rating", "")).strip()
        outlook = str(d.get("outlook", "")).strip()
        out[etf] = {
            "label": r["label"],
            "rating": rating if rating in RATINGS else "Neutral",
            "reason": str(d.get("reason", "")).strip(),
            "risk": str(d.get("risk", "")).strip(),
            "outlook": outlook if outlook in OUTLOOKS else "Neutral",
        }
    return out or None


def analyze(rows: list[dict], macro: dict | None) -> dict | None:
    if not rows or not llm_enabled():
        return None
    data = llm_analyzer.generate_json(build_prompt(rows, macro), label="sector")
    return _normalize(data or {}, rows)


def _signature(rows: list[dict]) -> dict:
    """재평가 트리거 비교용 스냅샷: 섹터별 5일 순위."""
    return {r["etf"]: r.get("rank_5d") for r in rows}


def _materially_changed(prev_sig: dict, rows: list[dict],
                        rank_shift: int = 3) -> bool:
    """이전 평가 이후 섹터 강도가 의미있게 바뀌었나.

    트리거: 선두 섹터 교체 · 임의 섹터 5일순위 rank_shift 이상 이동.
    비교 불가/이전 없음이면 True.
    """
    if not prev_sig:
        return True
    cur = {r["etf"]: r.get("rank_5d") for r in rows}
    # 선두(1위) 섹터 교체
    prev_leader = min((e for e, rk in prev_sig.items() if rk),
                      key=lambda e: prev_sig[e], default=None)
    cur_leader = min((e for e, rk in cur.items() if rk),
                     key=lambda e: cur[e], default=None)
    if prev_leader and cur_leader and prev_leader != cur_leader:
        return True
    for etf, c in cur.items():
        p = prev_sig.get(etf)
        if c is not None and p is not None and abs(c - p) >= rank_shift:
            return True
    return False


def _save(result: dict, rows: list[dict]) -> None:
    try:
        from cache import save_cache
        save_cache("sector_rotation.json", result)
        save_cache("sector_rotation_sig.json", _signature(rows))
    except Exception:  # noqa: BLE001
        pass


def run(analyzer, macro: dict | None) -> dict | None:
    """풀 분석용: 무조건 새로 LLM 분석 후 캐시·시그니처 저장."""
    try:
        rows = analyzer.rotation_table()
    except Exception as e:  # noqa: BLE001
        print(f"[섹터LLM] 지표 수집 실패: {e}")
        return None
    result = analyze(rows, macro)
    if result:
        _save(result, rows)
    return result


def run_if_changed(analyzer, macro: dict | None
                   ) -> tuple[dict | None, bool]:
    """인트라데이용: 섹터 강도가 의미있게 바뀐 경우에만 LLM 재호출.

    반환 (결과, refreshed). 변화 없으면 캐시된 결과를 그대로 반환(refreshed=False).
    """
    from cache import load_data
    try:
        rows = analyzer.rotation_table()
    except Exception as e:  # noqa: BLE001
        print(f"[섹터LLM] 지표 수집 실패: {e}")
        return load_data("sector_rotation.json"), False
    prev = load_data("sector_rotation.json")
    prev_sig = load_data("sector_rotation_sig.json") or {}
    if prev and not _materially_changed(prev_sig, rows):
        return prev, False
    if not llm_enabled():
        return prev, False
    result = analyze(rows, macro)
    if result:
        _save(result, rows)
        return result, True
    return prev, False
