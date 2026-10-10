from datetime import UTC, datetime, timedelta
from itertools import count

import pytest

from daily_news.filtering import (
    deduplicate,
    filter_candidates,
    jaccard,
    limit_candidates,
    normalize_url,
    title_bigrams,
    window_start,
)
from daily_news.models import Article, FiltersConfig, Lang, SelectionConfig

NOW = datetime(2026, 10, 9, 0, 0, tzinfo=UTC)
SELECTION = SelectionConfig(
    japan_max=7,
    world_max=4,
    window_hours=30,
    max_window_hours=48,
    max_candidates=120,
    per_feed_max=20,
    stale_feed_hours=72,
    description_max_chars=200,
)
FILTERS = FiltersConfig(exclude_url_patterns=("/sport", "/sports/", "/entertainment"))

_ids = count(1)


def article(
    title: str = "",
    url: str = "",
    *,
    hours_ago: float = 1,
    feed: str = "Feed",
    lang: Lang = "en",
    description: str = "d",
) -> Article:
    n = next(_ids)
    return Article(
        id=f"a{n:03d}",
        feed_name=feed,
        source=feed,
        lang=lang,
        title=title or f"Unique headline number {n} zq{n}x",
        description=description,
        url=url or f"https://news.example/{n}",
        published_at=NOW - timedelta(hours=hours_ago),
    )


def run(
    articles: list[Article],
    *,
    last_posted_at: datetime | None = None,
    posted_urls: tuple[str, ...] = (),
    selection: SelectionConfig = SELECTION,
) -> tuple[list[Article], list[Article]]:
    result = filter_candidates(
        articles,
        now=NOW,
        last_posted_at=last_posted_at,
        posted_urls=posted_urls,
        selection=selection,
        filters=FILTERS,
    )
    return list(result.eligible), list(result.candidates)


# --- time window ---------------------------------------------------------------


def test_window_normal_is_30_hours() -> None:
    assert window_start(NOW, None, SELECTION) == NOW - timedelta(hours=30)
    # A recent last post does not shorten the window.
    assert window_start(NOW, NOW - timedelta(hours=24), SELECTION) == NOW - timedelta(hours=30)


def test_window_extends_back_to_last_post() -> None:
    last = NOW - timedelta(hours=40)
    assert window_start(NOW, last, SELECTION) == last


def test_window_capped_at_48_hours() -> None:
    assert window_start(NOW, NOW - timedelta(days=5), SELECTION) == NOW - timedelta(hours=48)


def test_filter_applies_window() -> None:
    inside, edge, outside = article(hours_ago=29), article(hours_ago=30), article(hours_ago=31)

    eligible, _ = run([inside, edge, outside])
    assert [a.id for a in eligible] == [inside.id, edge.id]

    eligible, _ = run([inside, edge, outside], last_posted_at=NOW - timedelta(hours=40))
    assert [a.id for a in eligible] == [inside.id, edge.id, outside.id]


def test_filter_treats_future_dates_as_now() -> None:
    future = article(hours_ago=-5)

    eligible, _ = run([future])
    assert [a.id for a in eligible] == [future.id]


# --- URL normalization -----------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            "https://www.bbc.com/news/articles/x1?at_medium=RSS&at_campaign=rss",
            "https://www.bbc.com/news/articles/x1",
        ),
        (
            "https://example.com/a?utm_source=rss&UTM_Medium=x&id=5&Ref=home&page=2",
            "https://example.com/a?id=5&page=2",
        ),
        ("https://example.com/a?traffic_source=rss&source=feed", "https://example.com/a"),
        ("http://Example.COM/News/", "https://example.com/News"),
        ("https://example.com", "https://example.com/"),
        ("https://example.com/", "https://example.com/"),
        ("https://example.com/a#comments", "https://example.com/a"),
        ("https://example.com/a?q=a%20b&x=1+2", "https://example.com/a?q=a%20b&x=1+2"),
    ],
)
def test_normalize_url(raw: str, expected: str) -> None:
    assert normalize_url(raw) == expected


def test_filter_returns_normalized_urls() -> None:
    a = article(url="http://News.example/x/?utm_source=rss")

    eligible, candidates = run([a])
    assert eligible[0].url == candidates[0].url == "https://news.example/x"


# --- posted URLs and exclusion patterns -------------------------------------------


def test_filter_excludes_posted_urls_after_normalization() -> None:
    posted = article(url="http://news.example/old/?at_medium=RSS")
    fresh = article()

    eligible, _ = run([posted, fresh], posted_urls=("https://news.example/old",))
    assert [a.id for a in eligible] == [fresh.id]


def test_filter_normalizes_stored_posted_urls_too() -> None:
    posted = article(url="https://news.example/old")

    eligible, _ = run([posted], posted_urls=("http://NEWS.example/old/",))
    assert eligible == []


def test_filter_excludes_url_patterns_in_path_only() -> None:
    sports = article(url="https://www.aljazeera.com/sports/2026/10/8/match")
    sport_section = article(url="https://www.bbc.com/sport/football/1")
    world = article(url="https://www.aljazeera.com/news/2026/10/8/talks")
    sport_in_host = article(url="https://sport.example/news/1")
    sport_in_query = article(url="https://news.example/a?section=/sports/")

    eligible, _ = run([sports, sport_section, world, sport_in_host, sport_in_query])
    assert [a.id for a in eligible] == [world.id, sport_in_host.id, sport_in_query.id]


# --- deduplication ---------------------------------------------------------------


def test_title_bigrams_ignore_case_and_whitespace() -> None:
    assert title_bigrams("Ab C") == title_bigrams("abc") == frozenset({"ab", "bc"})
    assert title_bigrams("あ") == frozenset({"あ"})
    assert title_bigrams(" ") == frozenset()
    assert jaccard(frozenset(), frozenset()) == 0.0


def test_dedup_similar_titles_keeps_newer_even_if_older_is_longer() -> None:
    older = article(
        "【随時更新】ダミー国際情勢の動き(10月8日)",
        lang="ja",
        hours_ago=20,
        description="前日版のダミー本文で、こちらの方が長い説明になっている",
    )
    newer = article(
        "【随時更新】ダミー国際情勢の動き(10月9日)", lang="ja", hours_ago=2, description="短い"
    )
    assert jaccard(title_bigrams(older.title), title_bigrams(newer.title)) >= 0.6

    assert deduplicate([older, newer]) == [newer]
    assert deduplicate([newer, older]) == [newer]


def test_dedup_similar_titles_same_time_keeps_longer_description() -> None:
    short = article("政府が新しい経済対策を発表", lang="ja", description="短い")
    long = article("政府、新しい経済対策を発表", lang="ja", description="こちらの方が長い説明")
    assert short.published_at == long.published_at
    assert jaccard(title_bigrams(short.title), title_bigrams(long.title)) >= 0.6

    assert deduplicate([short, long]) == [long]
    assert deduplicate([long, short]) == [long]


def test_dedup_same_url_keeps_longer_description_even_if_older() -> None:
    newer = article("Title one", url="https://news.example/same", hours_ago=1, description="x")
    older = article(
        "Title one", url="https://news.example/same", hours_ago=5, description="longer text"
    )

    assert deduplicate([newer, older]) == [older]
    assert deduplicate([older, newer]) == [older]


def test_dedup_tie_keeps_first() -> None:
    first = article("Leaders agree on new climate deal", description="same")
    second = article("Leaders agree on new climate deal!", description="same")

    assert deduplicate([first, second]) == [first]


def test_dedup_below_threshold_keeps_both() -> None:
    a = article("Central bank raises interest rates")
    b = article("Central bank holds interest rates steady amid inflation fears")
    assert jaccard(title_bigrams(a.title), title_bigrams(b.title)) < 0.6

    assert deduplicate([a, b]) == [a, b]


def test_dedup_does_not_merge_across_languages() -> None:
    ja = article("G7 Summit 2026", lang="ja")
    en = article("G7 Summit 2026", lang="en")

    assert deduplicate([ja, en]) == [ja, en]


def test_dedup_same_url_across_languages() -> None:
    a = article("Title one", url="https://news.example/same", lang="en", description="x")
    b = article("全く別の見出し", url="https://news.example/same", lang="ja", description="longer")

    assert deduplicate([a, b]) == [b]


def test_filter_dedups_urls_differing_only_by_tracking() -> None:
    a = article(url="https://www.bbc.com/news/x?at_medium=RSS", feed="BBC World")
    b = article(url="https://www.bbc.com/news/x/", feed="BBC 日本語")

    eligible, candidates = run([a, b])
    assert len(eligible) == 2  # the fallback still sees both
    assert [c.id for c in candidates] == [a.id]


# --- candidate limits ----------------------------------------------------------------


def test_limit_per_feed_keeps_newest() -> None:
    selection = SELECTION.model_copy(update={"per_feed_max": 2})
    old, new, mid = article(hours_ago=10), article(hours_ago=1), article(hours_ago=5)

    assert limit_candidates([old, new, mid], selection) == [new, mid]


def test_limit_round_robin_prevents_one_feed_dominating() -> None:
    selection = SELECTION.model_copy(update={"per_feed_max": 10, "max_candidates": 5})
    big = [article(feed="Big", hours_ago=h) for h in range(1, 9)]
    small_a = [article(feed="SmallA", hours_ago=h) for h in (1, 2)]
    small_b = [article(feed="SmallB", hours_ago=3)]

    result = limit_candidates([*big, *small_a, *small_b], selection)
    assert [a.feed_name for a in result] == ["Big", "SmallA", "SmallB", "Big", "SmallA"]
    assert [a.id for a in result if a.feed_name == "Big"] == [big[0].id, big[1].id]


def test_limit_round_robin_continues_after_short_feeds_run_out() -> None:
    selection = SELECTION.model_copy(update={"per_feed_max": 3, "max_candidates": 100})
    a = [article(feed="A", hours_ago=h) for h in (1, 2, 3, 4)]
    b = [article(feed="B", hours_ago=1)]

    result = limit_candidates([*a, *b], selection)
    assert [x.feed_name for x in result] == ["A", "B", "A", "A"]


def test_filter_result_separates_eligible_and_candidates() -> None:
    selection = SELECTION.model_copy(update={"max_candidates": 2})
    dup1 = article("Earthquake hits northern region", feed="F1", description="a")
    dup2 = article("Earthquake hits northern region", feed="F2", description="abc")
    other = [article(feed="F1"), article(feed="F2")]
    excluded = article(url="https://news.example/sport/1")

    eligible, candidates = run([dup1, dup2, *other, excluded], selection=selection)
    assert [a.id for a in eligible] == [dup1.id, dup2.id, other[0].id, other[1].id]
    assert len(candidates) == 2
    assert dup1.id not in {c.id for c in candidates}
