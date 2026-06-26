"""뉴스 감성 분석 (키워드 기반 베이스라인).

뉴스 헤드라인에서 긍정/부정 키워드를 세어 0~100 감성 점수를 만든다.
이 점수는 베이스라인이며, LLM(llm_analyzer)이 더 정교한 감성을 반환하면
scorer에서 그 값을 우선 사용한다.
"""
from __future__ import annotations

POS_WORDS = (
    "beat", "beats", "surge", "soar", "rally", "record", "raised", "raises",
    "upgrade", "upgraded", "outperform", "strong", "growth", "wins", "win",
    "partnership", "approval", "approved", "expansion", "breakthrough",
    "high", "jumps", "gains", "bullish", "boost", "milestone", "demand",
)
NEG_WORDS = (
    "miss", "misses", "plunge", "drop", "fall", "falls", "cut", "cuts",
    "lowered", "downgrade", "downgraded", "underperform", "weak", "lawsuit",
    "probe", "investigation", "recall", "warning", "warns", "decline",
    "loss", "losses", "bearish", "halt", "delay", "concern", "slump", "fraud",
)


def analyze_sentiment(news: list) -> dict:
    pos_hits, neg_hits = [], []
    for n in news or []:
        hl = (getattr(n, "headline", "") or "").lower()
        if any(w in hl for w in POS_WORDS):
            pos_hits.append(getattr(n, "headline", ""))
        if any(w in hl for w in NEG_WORDS):
            neg_hits.append(getattr(n, "headline", ""))

    if not news:
        return {"score": 50.0, "label": "중립",
                "events": {"positive": [], "negative": []}}

    score = 50 + (len(pos_hits) - len(neg_hits)) * 8
    score = max(0.0, min(100.0, float(score)))
    label = "긍정" if score >= 60 else "부정" if score <= 40 else "중립"
    return {
        "score": round(score, 1), "label": label,
        "events": {"positive": pos_hits[:3], "negative": neg_hits[:3]},
    }
