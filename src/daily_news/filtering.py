"""First-pass filtering of fetched articles before the LLM (SPEC §6).

URLs are normalized first so that posted-URL exclusion and URL-based deduplication
compare like with like (``state.json`` stores normalized URLs).
"""

import logging
import re
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from itertools import zip_longest
from urllib.parse import unquote_plus, urlsplit, urlunsplit

from daily_news.models import Article, FiltersConfig, SelectionConfig

logger = logging.getLogger(__name__)

TRACKING_PARAMS = frozenset({"at_medium", "at_campaign", "traffic_source", "ref", "source"})
TRACKING_PREFIXES = ("utm_",)
DUPLICATE_TITLE_THRESHOLD = 0.6


@dataclass(frozen=True)
class FilterResult:
    eligible: tuple[Article, ...]
    """After the time window, posted-URL and URL-pattern exclusion (used by the fallback)."""
    candidates: tuple[Article, ...]
    """``eligible`` after deduplication and candidate limits (passed to the LLM)."""


def _is_tracking_param(pair: str) -> bool:
    key = unquote_plus(pair.split("=", 1)[0]).lower()
    return key in TRACKING_PARAMS or key.startswith(TRACKING_PREFIXES)


def normalize_url(url: str) -> str:
    """https, lowercase host, no tracking query params, no fragment, no trailing slash."""
    parts = urlsplit(url.strip())
    # Split the raw query instead of re-encoding it so the remaining params stay byte-identical.
    query = "&".join(p for p in parts.query.split("&") if p and not _is_tracking_param(p))
    path = parts.path.rstrip("/") or "/"
    return urlunsplit(("https", parts.netloc.lower(), path, query, ""))


def window_start(
    now: datetime, last_posted_at: datetime | None, selection: SelectionConfig
) -> datetime:
    """``max(now - max_window, min(now - window, last_posted_at))``."""
    normal = now - timedelta(hours=selection.window_hours)
    if last_posted_at is not None:
        normal = min(normal, last_posted_at)
    return max(now - timedelta(hours=selection.max_window_hours), normal)


def title_bigrams(title: str) -> frozenset[str]:
    """Character bigrams of a title, ignoring case and whitespace."""
    text = re.sub(r"\s+", "", title.lower())
    if len(text) < 2:
        return frozenset({text}) if text else frozenset()
    return frozenset(text[i : i + 2] for i in range(len(text) - 1))


def jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _url_path_excluded(url: str, patterns: Sequence[str]) -> bool:
    path = urlsplit(url).path
    return any(pattern in path for pattern in patterns)


def _replaces(new: Article, old: Article, *, same_url: bool) -> bool:
    """Whether ``new`` should be kept instead of the earlier duplicate ``old``.

    Same URL: the longer description wins. Similar title: the newer article wins, so a
    daily recurring article dated yesterday does not survive over today's; then the longer
    description. Remaining ties keep ``old``.
    """
    if not same_url and new.published_at != old.published_at:
        return new.published_at > old.published_at
    return len(new.description) > len(old.description)


def deduplicate(articles: Sequence[Article]) -> list[Article]:
    """Drop articles with the same URL or a similar title in the same language.

    The surviving article takes the position of the first one seen (see ``_replaces``).
    """
    bigrams = {a.id: title_bigrams(a.title) for a in articles}
    kept: list[Article] = []
    for article in articles:
        for i, other in enumerate(kept):
            same_url = article.url == other.url
            similar = article.lang == other.lang and (
                jaccard(bigrams[article.id], bigrams[other.id]) >= DUPLICATE_TITLE_THRESHOLD
            )
            if same_url or similar:
                if _replaces(article, other, same_url=same_url):
                    kept[i] = article
                break
        else:
            kept.append(article)
    return kept


def limit_candidates(articles: Sequence[Article], selection: SelectionConfig) -> list[Article]:
    """Newest ``per_feed_max`` per feed, then round-robin across feeds up to ``max_candidates``.

    Feeds take turns in the order they first appear in ``articles`` (the feeds.toml order).
    """
    by_feed: dict[str, list[Article]] = {}
    for article in articles:
        by_feed.setdefault(article.feed_name, []).append(article)
    per_feed = [
        sorted(group, key=lambda a: a.published_at, reverse=True)[: selection.per_feed_max]
        for group in by_feed.values()
    ]
    interleaved = [a for row in zip_longest(*per_feed) for a in row if a is not None]
    return interleaved[: selection.max_candidates]


def filter_candidates(
    articles: Sequence[Article],
    *,
    now: datetime,
    last_posted_at: datetime | None,
    posted_urls: Collection[str],
    selection: SelectionConfig,
    filters: FiltersConfig,
) -> FilterResult:
    """Apply SPEC §6 steps 1-6. Returned articles carry normalized URLs."""
    normalized = [a.model_copy(update={"url": normalize_url(a.url)}) for a in articles]

    start = window_start(now, last_posted_at, selection)
    in_window = [a for a in normalized if min(a.published_at, now) >= start]

    posted = {normalize_url(url) for url in posted_urls}
    not_posted = [a for a in in_window if a.url not in posted]

    eligible = [
        a for a in not_posted if not _url_path_excluded(a.url, filters.exclude_url_patterns)
    ]
    unique = deduplicate(eligible)
    candidates = limit_candidates(unique, selection)

    logger.info(
        "filtering: fetched=%d in_window=%d not_posted=%d eligible=%d unique=%d candidates=%d "
        "window_start=%s",
        len(articles),
        len(in_window),
        len(not_posted),
        len(eligible),
        len(unique),
        len(candidates),
        start.isoformat(),
    )
    return FilterResult(eligible=tuple(eligible), candidates=tuple(candidates))
