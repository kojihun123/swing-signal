"""멀티 LLM 프로바이더 (Google Gemini + Groq) — 부하 분산 + 폴백.

여러 무료티어 키를 라운드로빈으로 번갈아 호출해 일일/분당 한도를 분산하고,
한 프로바이더가 실패(한도초과·일시오류 등)하면 다음 프로바이더로 폴백한다.
LLM 호출의 단일 진입점은 generate_text() 하나다(llm_analyzer가 이를 감싼다).
Groq(groq.com, Llama 등 오픈모델 무료 추론)는 OpenAI 호환 API라 openai SDK에
base_url만 바꿔 재사용한다. (※ xAI의 'Grok'과는 다른 회사이니 혼동 주의.)

env:
  GEMINI_API_KEY / GOOGLE_API_KEY   Gemini 활성화
  GROQ_API_KEY                      Groq 활성화 (키 접두사 gsk_)
  LLM_PROVIDER  = auto | gemini | groq   (기본 auto = 활성 프로바이더 라운드로빈)
  GEMINI_MODEL  (기본 gemini-2.5-flash)
  GROQ_MODEL    (기본 llama-3.3-70b-versatile)
"""
from __future__ import annotations

import itertools
import os
import time

GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
GROQ_BASE_URL = os.environ.get("GROQ_BASE_URL", "https://api.groq.com/openai/v1")

# 일시적 오류(재시도) / 한도·과부하(폴백) 판별용 문자열
_TRANSIENT = ("503", "unavailable", "429", "overloaded", "high demand",
              "resource_exhausted", "500", "rate limit", "timeout",
              "insufficient_quota", "perday", "requestsperday")


def _gemini_key() -> str | None:
    return os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")


def _groq_key() -> str | None:
    return os.environ.get("GROQ_API_KEY") or None


def enabled_providers() -> list[str]:
    """현재 호출 가능한 프로바이더 목록(env 기반). LLM_PROVIDER로 강제 선택 가능."""
    pref = os.environ.get("LLM_PROVIDER", "auto").strip().lower()
    avail: list[str] = []
    if _gemini_key():
        avail.append("gemini")
    if _groq_key():
        avail.append("groq")
    if pref in ("gemini", "groq"):
        return [pref] if pref in avail else []
    return avail


def enabled() -> bool:
    return bool(enabled_providers())


def _call_gemini(prompt: str, label: str) -> str | None:
    """Gemini 호출 → JSON 텍스트. 일시오류는 재시도, 그 외 실패는 None."""
    try:
        from google import genai
        from google.genai import types
    except ImportError:
        print("[LLM] google-genai 미설치 → gemini skip")
        return None
    client = genai.Client(api_key=_gemini_key())
    for attempt in range(3):
        try:
            resp = client.models.generate_content(
                model=GEMINI_MODEL, contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json"))
            return (resp.text or "").strip() or None
        except Exception as e:  # noqa: BLE001
            msg = str(e).lower()
            if any(s in msg for s in _TRANSIENT) and attempt < 2:
                time.sleep(3 * (attempt + 1))
                continue
            print(f"[LLM] gemini {label} 실패: {str(e)[:120]}")
            return None
    return None


def _call_groq(prompt: str, label: str) -> str | None:
    """Groq(OpenAI 호환, JSON 모드) 호출 → JSON 텍스트."""
    try:
        from openai import OpenAI
    except ImportError:
        print("[LLM] openai SDK 미설치 → groq skip")
        return None
    client = OpenAI(api_key=_groq_key(), base_url=GROQ_BASE_URL)
    for attempt in range(3):
        try:
            resp = client.chat.completions.create(
                model=GROQ_MODEL,
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"})
            return (resp.choices[0].message.content or "").strip() or None
        except Exception as e:  # noqa: BLE001
            msg = str(e).lower()
            if any(s in msg for s in _TRANSIENT) and attempt < 2:
                time.sleep(3 * (attempt + 1))
                continue
            print(f"[LLM] groq {label} 실패: {str(e)[:120]}")
            return None
    return None


_CALLS = {"gemini": _call_gemini, "groq": _call_groq}
_rr = itertools.count()  # 라운드로빈 시작점 회전(부하 분산)


def generate_text(prompt: str, label: str = "") -> str | None:
    """활성 프로바이더를 라운드로빈+폴백으로 호출. JSON 문자열 또는 None.

    매 호출마다 시작 프로바이더를 회전시켜 한도를 분산하고, 실패하면 다음
    프로바이더로 폴백한다(모두 실패 시 None).
    """
    provs = enabled_providers()
    if not provs:
        return None
    start = next(_rr) % len(provs)
    order = provs[start:] + provs[:start]
    for i, name in enumerate(order):
        text = _CALLS[name](prompt, label)
        if text:
            return text
        if i < len(order) - 1:
            print(f"[LLM] {name} 실패 → 다음 프로바이더로 폴백")
    return None
