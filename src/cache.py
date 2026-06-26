"""공통 캐시 모듈.

모든 분석 모듈은 결과를 JSON 캐시로 저장하고, 다른 모듈/LLM은 캐시에서
읽어 통합한다. 모든 캐시는 updated_at(KST ISO) 타임스탬프를 포함한다.

캐시 래퍼 형식:
  {"updated_at": "2026-06-27T23:00:00+09:00", "data": { ... }}
"""
from __future__ import annotations

import json
import os
from datetime import datetime

from utils import now_notify

CACHE_DIR = os.environ.get("CACHE_DIR", "data/cache")


def _path(filename: str, cache_dir: str | None = None) -> str:
    return os.path.join(cache_dir or CACHE_DIR, filename)


def save_cache(filename: str, data, cache_dir: str | None = None) -> str:
    """data를 updated_at 래퍼로 감싸 JSON 저장. 저장 경로 반환."""
    path = _path(filename, cache_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    wrap = {"updated_at": now_notify().isoformat(), "data": data}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(wrap, f, ensure_ascii=False, indent=2, default=str)
    return path


def load_cache(filename: str, cache_dir: str | None = None) -> dict | None:
    """캐시 래퍼 전체({updated_at, data})를 반환. 없으면 None."""
    path = _path(filename, cache_dir)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return None


def load_data(filename: str, cache_dir: str | None = None):
    """캐시의 data 부분만 반환. 없으면 None."""
    wrap = load_cache(filename, cache_dir)
    return wrap.get("data") if wrap else None


def cache_age_hours(filename: str, cache_dir: str | None = None) -> float | None:
    """캐시 갱신 후 경과 시간(시간). 없으면 None."""
    wrap = load_cache(filename, cache_dir)
    if not wrap or "updated_at" not in wrap:
        return None
    try:
        ts = datetime.fromisoformat(wrap["updated_at"])
    except (ValueError, TypeError):
        return None
    now = now_notify()
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=now.tzinfo)
    return (now - ts).total_seconds() / 3600.0


def is_cache_fresh(filename: str, hours: float,
                   cache_dir: str | None = None) -> bool:
    """캐시가 N시간 이내면 True."""
    age = cache_age_hours(filename, cache_dir)
    return age is not None and age <= hours
