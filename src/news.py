"""종목별 뉴스 수집 (Finviz 종목 페이지 스크래핑).

Finviz는 2025년 개편으로 종목별 RSS(rss.ashx)가 더 이상 동작하지 않는다.
대신 종목 페이지(quote.ashx → /stock 으로 리다이렉트)의 뉴스 테이블
(id="news-table")을 직접 파싱한다.

테이블 행 구조:
  <td align="right"> Jun-23-26 02:51PM </td>   # 같은 날이면 시간만 표시
  <a class="tab-link-news" href="..."> 헤드라인 </a>
  <span>(출처)</span>

날짜는 미국 동부시간(ET) 기준. 최근 N개(기본 5개)를 수집하되,
24시간 이내 항목만 사용한다. 24시간 내 뉴스가 없으면 가장 최근 N개를
폴백으로 반환한다(종목에 따라 뉴스가 며칠에 한 번씩만 나오기 때문).
외부 파싱 의존성 없이 표준 라이브러리(re/html)로 처리한다.
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests

# Finviz 뉴스 타임스탬프는 미국 동부시간 기준
ET = ZoneInfo("America/New_York")

QUOTE_URL = "https://finviz.com/quote.ashx?t={ticker}"


def _is_kr(ticker: str) -> bool:
    """한국 종목(.KS/.KQ)은 Finviz(미국 전용)에 없으므로 바로 yfinance를 쓴다."""
    return ticker.upper().endswith((".KS", ".KQ"))
# Finviz는 기본 requests UA를 차단하므로 브라우저 UA를 흉내낸다.
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# 뉴스 테이블 블록 / 개별 행 / 행 내부(날짜셀 + 헤드라인 앵커) 추출
_TABLE_RE = re.compile(r'id="news-table".*?</table>', re.S)
_ROW_RE = re.compile(r"<tr\b.*?</tr>", re.S)
_CELL_RE = re.compile(
    r'<td[^>]*align="right"[^>]*>\s*(?P<date>.*?)\s*</td>.*?'
    r'<a[^>]*class="tab-link-news"[^>]*href="(?P<href>[^"]+)"[^>]*>'
    r"\s*(?P<title>.*?)\s*</a>",
    re.S,
)


@dataclass
class NewsItem:
    headline: str
    url: str
    published: datetime          # tz-aware (UTC)
    age: str                     # "6시간 전" 등 상대 시간

    def as_dict(self) -> dict:
        return {"headline": self.headline, "url": self.url,
                "published": self.published.isoformat(), "age": self.age}


def _age_str(published: datetime, now: datetime) -> str:
    """현재 기준 상대 시간 문자열."""
    secs = (now - published).total_seconds()
    if secs < 0:
        return "방금"
    if secs < 3600:
        return f"{int(secs // 60)}분 전"
    if secs < 86400:
        return f"{int(secs // 3600)}시간 전"
    return f"{int(secs // 86400)}일 전"


def _parse_rows(htmltext: str) -> list[tuple[datetime, str, str]]:
    """뉴스 테이블에서 (published_utc, headline, url) 목록을 추출."""
    m = _TABLE_RE.search(htmltext)
    if not m:
        return []
    block = m.group(0)

    out: list[tuple[datetime, str, str]] = []
    last_date: str | None = None  # 시간만 있는 행이 이어받을 날짜

    for row in _ROW_RE.findall(block):
        cm = _CELL_RE.search(row)
        if not cm:
            continue
        dtxt = re.sub(r"\s+", " ", cm.group("date")).strip()
        href = html.unescape(cm.group("href").strip())
        title = html.unescape(re.sub(r"\s+", " ", cm.group("title")).strip())
        if not dtxt or not title:
            continue

        # "Jun-23-26 02:51PM" (날짜+시간) 또는 "10:12AM" (시간만)
        if " " in dtxt:
            date_part, time_part = dtxt.split(" ", 1)
            last_date = date_part
        else:
            time_part = dtxt
        if last_date is None:
            continue
        try:
            dt = datetime.strptime(
                f"{last_date} {time_part}", "%b-%d-%y %I:%M%p"
            ).replace(tzinfo=ET)
        except ValueError:
            continue

        if href.startswith("/"):
            href = "https://finviz.com" + href
        out.append((dt.astimezone(timezone.utc), title, href))

    return out


def _yf_news_rows(ticker: str) -> list[tuple[datetime, str, str]]:
    """yfinance 뉴스 폴백 (Finviz가 비는 한국/비미국 종목 등에 사용).

    yfinance 뉴스는 신/구 두 가지 포맷이 있어 모두 처리한다.
    """
    try:
        import yfinance as yf
        items = yf.Ticker(ticker).news or []
    except Exception as e:  # noqa: BLE001
        print(f"[뉴스] {ticker} yfinance 폴백 실패: {e}")
        return []

    out: list[tuple[datetime, str, str]] = []
    for it in items:
        content = it.get("content") if isinstance(it, dict) else None
        title = url = pub = None
        if isinstance(content, dict):  # 신 포맷
            title = content.get("title")
            pub = content.get("pubDate") or content.get("displayTime")
            url = ((content.get("canonicalUrl") or {}).get("url")
                   or (content.get("clickThroughUrl") or {}).get("url"))
            dt = None
            if pub:
                try:
                    dt = datetime.fromisoformat(str(pub).replace("Z", "+00:00"))
                except ValueError:
                    dt = None
        else:  # 구 포맷
            title = it.get("title")
            url = it.get("link")
            ts = it.get("providerPublishTime")
            dt = (datetime.fromtimestamp(ts, tz=timezone.utc)
                  if ts else None)
        if not title:
            continue
        if dt is None:
            dt = datetime.now(timezone.utc)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        out.append((dt.astimezone(timezone.utc), html.unescape(title.strip()),
                    url or ""))
    return out


def fetch_news(ticker: str, limit: int = 5, within_hours: int = 24,
               timeout: float = 10.0) -> list[NewsItem]:
    """종목 뉴스를 수집. Finviz(미국) 우선, 비면 yfinance로 폴백.

    24시간 이내 뉴스를 우선 반환하고, 없으면 가장 최근 N개로 폴백한다.
    """
    rows: list[tuple[datetime, str, str]] = []
    # 네이버 우선(한국어): KR 국내 뉴스 / US 한국어 번역뉴스(worldnews)
    try:
        import naver
        rows = naver.news_rows(ticker)
    except Exception as e:  # noqa: BLE001
        print(f"[뉴스] {ticker} 네이버 수집 실패: {e}")

    # US 종목: 네이버가 비면 Finviz(영어) 폴백
    if not rows and not _is_kr(ticker):
        url = QUOTE_URL.format(ticker=ticker.upper())
        try:
            r = requests.get(url, headers=_HEADERS, timeout=timeout)
            r.raise_for_status()
            rows = _parse_rows(r.text)
        except Exception as e:  # noqa: BLE001
            print(f"[뉴스] {ticker} Finviz 수집 실패: {e}")

    # 위 소스가 비면 yfinance 뉴스로 최종 폴백
    if not rows:
        rows = _yf_news_rows(ticker)
    if not rows:
        print(f"[뉴스] {ticker} 뉴스 없음")
        return []

    # 최신순 정렬
    rows.sort(key=lambda x: x[0], reverse=True)

    now = datetime.now(timezone.utc)
    cutoff = within_hours * 3600
    recent = [row for row in rows if (now - row[0]).total_seconds() <= cutoff]

    # 24시간 내 뉴스가 없으면 가장 최근 항목으로 폴백
    chosen = recent[:limit] if recent else rows[:limit]

    return [
        NewsItem(headline=title, url=href, published=dt, age=_age_str(dt, now))
        for dt, title, href in chosen
    ]


def fetch_latest(ticker: str, hours: int = 1, limit: int = 5) -> str:
    """긴급 분석용: 최근 N시간 뉴스를 프롬프트에 넣을 텍스트로 반환.

    해당 시간 내 뉴스가 없으면 가장 최근 항목으로 폴백한다(없으면 안내 문구).
    """
    items = fetch_news(ticker, limit=limit, within_hours=hours)
    if not items:
        return "(최근 뉴스 없음)"
    return "\n".join(f'- "{n.headline}" ({n.age})' for n in items)
