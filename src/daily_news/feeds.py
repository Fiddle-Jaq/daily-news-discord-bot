"""RSS fetching, parsing and freshness checks (SPEC §5).

feedparser results are untyped, so they are converted to ``Article`` models inside
``parse_feed`` and nothing else touches them.
"""

import calendar
import logging
import re
import time
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from itertools import count
from typing import Any

import feedparser
import httpx

from daily_news import __version__
from daily_news.errors import describe_error
from daily_news.models import Article, FeedConfig, FeedResult, SelectionConfig

logger = logging.getLogger(__name__)

USER_AGENT = (
    f"daily-news-discord-bot/{__version__} (+https://github.com/Fiddle-Jaq/daily-news-discord-bot)"
)
TIMEOUT = httpx.Timeout(15.0, connect=10.0)
NHK_SOURCE = "NHK"


class NotAFeedError(Exception):
    """The response was not RSS/Atom (e.g. an HTML block or error page served with 200)."""


def make_client(transport: httpx.BaseTransport | None = None) -> httpx.Client:
    return httpx.Client(
        timeout=TIMEOUT,
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT},
        transport=transport,
    )


def article_ids() -> Iterator[str]:
    """Short per-run article IDs: a001, a002, ..."""
    return (f"a{n:03d}" for n in count(1))


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        self.parts.append(" ")


def clean_text(raw: str) -> str:
    """Strip HTML tags, unescape entities and collapse whitespace."""
    extractor = _TextExtractor()
    extractor.feed(raw)
    extractor.close()
    return re.sub(r"\s+", " ", "".join(extractor.parts)).strip()


def truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1] + "…"


def _entry_str(entry: Any, key: str) -> str:
    value = entry.get(key)
    return value if isinstance(value, str) else ""


def _entry_datetime(entry: Any) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        value = entry.get(key)
        if isinstance(value, time.struct_time):
            # feedparser normalizes parsed dates to UTC.
            return datetime.fromtimestamp(calendar.timegm(value), UTC)
    return None


def parse_feed(
    content: bytes,
    feed: FeedConfig,
    now: datetime,
    *,
    description_max_chars: int,
    ids: Iterator[str],
) -> list[Article]:
    """Convert raw feed bytes into articles. Raises ``NotAFeedError`` for non-feed content."""
    parsed = feedparser.parse(content)
    if not parsed.get("version"):
        raise NotAFeedError
    articles: list[Article] = []
    for entry in parsed.entries:
        title = clean_text(_entry_str(entry, "title"))
        url = _entry_str(entry, "link").strip()
        published_at = _entry_datetime(entry)
        if not title or not url.startswith(("http://", "https://")) or published_at is None:
            continue
        articles.append(
            Article(
                id=next(ids),
                feed_name=feed.name,
                source=feed.source,
                lang=feed.lang,
                title=title,
                description=truncate(
                    clean_text(_entry_str(entry, "summary")), description_max_chars
                ),
                url=url,
                published_at=min(published_at, now),
            )
        )
    return articles


def _get(client: httpx.Client, url: str) -> httpx.Response:
    """GET with a single immediate retry on timeouts and 5xx responses."""
    for attempt in (1, 2):
        try:
            response = client.get(url)
            response.raise_for_status()
            return response
        except httpx.TimeoutException:
            if attempt == 2:
                raise
        except httpx.HTTPStatusError as exc:
            if attempt == 2 or not exc.response.is_server_error:
                raise
    raise AssertionError("unreachable")


def fetch_feed(
    client: httpx.Client,
    feed: FeedConfig,
    selection: SelectionConfig,
    now: datetime,
    ids: Iterator[str],
) -> FeedResult:
    """Fetch and parse one feed; failures are recorded in the result, never raised."""
    status_code: int | None = None
    try:
        response = _get(client, str(feed.url))
        status_code = response.status_code
        articles = parse_feed(
            response.content,
            feed,
            now,
            description_max_chars=selection.description_max_chars,
            ids=ids,
        )
    except Exception as exc:
        if isinstance(exc, httpx.HTTPStatusError):
            status_code = exc.response.status_code
        error = describe_error(exc)
        logger.warning("fetch_feed failed: feed=%s %s", feed.name, error)
        return FeedResult(feed=feed, status_code=status_code, error=error)

    latest_at = max((a.published_at for a in articles), default=None)
    stale = latest_at is None or latest_at < now - timedelta(hours=selection.stale_feed_hours)
    if stale:
        logger.warning(
            "fetch_feed stale: feed=%s articles=%d latest=%s",
            feed.name,
            len(articles),
            latest_at.isoformat() if latest_at else None,
        )
    else:
        logger.info("fetch_feed ok: feed=%s articles=%d", feed.name, len(articles))
    return FeedResult(
        feed=feed,
        status_code=status_code,
        articles=tuple(articles),
        latest_at=latest_at,
        stale=stale,
    )


def fetch_all(
    client: httpx.Client,
    feeds: Sequence[FeedConfig],
    selection: SelectionConfig,
    now: datetime,
) -> list[FeedResult]:
    """Fetch every feed in order, numbering articles across all feeds."""
    ids = article_ids()
    results = [fetch_feed(client, feed, selection, now, ids) for feed in feeds]
    nhk = [r for r in results if r.feed.source == NHK_SOURCE]
    if nhk and not any(r.ok for r in nhk):
        logger.warning("all NHK feeds failed or are stale; Japan relies on backup sources")
    return results
