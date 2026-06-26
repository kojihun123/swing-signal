"""성장성 분석 (뉴스 기반 베이스라인 + 가이던스 방향).

뉴스 헤드라인에서 성장 시그널(AI투자·신제품·M&A·해외진출·CAPEX·가이던스)을
탐지해 0~100 성장성 점수와 가이던스 방향을 만든다. LLM이 더 정교한
growth_score를 반환하면 scorer에서 그 값을 우선 사용한다.
"""
from __future__ import annotations

# 카테고리 -> 키워드
GROWTH_SIGNALS = {
    "AI/신사업 투자": ("ai", "artificial intelligence", "data center",
                       "datacenter", "invest", "new business"),
    "신제품 출시": ("launch", "unveil", "new product", "release", "rollout"),
    "M&A/인수합병": ("acqui", "merger", "buyout", "takeover", "deal"),
    "해외 진출": ("overseas", "expansion", "global", "enter the", "international"),
    "CAPEX 증가": ("capex", "capital expenditure", "capacity", "fab",
                   "factory", "plant"),
    "점유율 확대": ("market share", "share gain", "leading", "dominant"),
}
GUIDANCE_UP = ("raised", "raises", "hikes guidance", "above consensus",
               "boosts outlook", "lifts forecast", "beat")
GUIDANCE_DOWN = ("lowered", "lowers", "cuts guidance", "below consensus",
                 "slashes outlook", "warns", "miss")


def analyze_growth(news: list) -> dict:
    signals = []
    for n in news or []:
        hl = (getattr(n, "headline", "") or "").lower()
        for cat, kws in GROWTH_SIGNALS.items():
            if any(k in hl for k in kws) and cat not in [s["category"] for s in signals]:
                signals.append({"category": cat,
                                "headline": getattr(n, "headline", "")})

    guidance = "유지"
    for n in news or []:
        hl = (getattr(n, "headline", "") or "").lower()
        if any(k in hl for k in GUIDANCE_UP):
            guidance = "상향"
            break
        if any(k in hl for k in GUIDANCE_DOWN):
            guidance = "하향"

    if not news:
        return {"score": 50.0, "signals": [], "guidance": "판단불가"}

    score = 50 + len(signals) * 6
    if guidance == "상향":
        score += 12
    elif guidance == "하향":
        score -= 12
    score = max(0.0, min(100.0, float(score)))
    return {"score": round(score, 1), "signals": signals[:4],
            "guidance": guidance}
