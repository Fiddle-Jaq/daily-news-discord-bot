from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from daily_news.cli import setup_logging
from daily_news.feeds import (
    USER_AGENT,
    NotAFeedError,
    article_ids,
    fetch_all,
    make_client,
    parse_feed,
)
from daily_news.models import Article, FeedConfig, SelectionConfig

FIXTURES = Path(__file__).parent / "fixtures"
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

Handler = Callable[[httpx.Request], httpx.Response]


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def make_feed(name: str, url: str, *, source: str = "Test", lang: str = "en") -> FeedConfig:
    return FeedConfig.model_validate({"name": name, "url": url, "lang": lang, "source": source})


NHK = make_feed("NHK 主要", "https://nhk.example/cat0.xml", source="NHK", lang="ja")
BBC_JA = make_feed("BBC 日本語", "https://bbc.example/japanese.xml", source="BBC", lang="ja")
GUARDIAN = make_feed("Guardian World", "https://guardian.example/world.xml")
ALJAZEERA = make_feed("Al Jazeera", "https://aljazeera.example/all.xml")


def parse(name: str, feed: FeedConfig) -> list[Article]:
    return parse_feed(fixture(name), feed, NOW, description_max_chars=200, ids=article_ids())


def rss(items: str) -> bytes:
    channel = f"<channel><title>t</title>{items}</channel>"
    return f'<?xml version="1.0"?><rss version="2.0">{channel}</rss>'.encode()


def item(title: str, link: str, pub_date: str | None) -> str:
    date = f"<pubDate>{pub_date}</pubDate>" if pub_date else ""
    return (
        f"<item><title>{title}</title><link>{link}</link><description>d</description>{date}</item>"
    )


def client_for(routes: dict[str, Handler], calls: dict[str, int] | None = None) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if calls is not None:
            calls[url] = calls.get(url, 0) + 1
        return routes[url](request)

    return make_client(httpx.MockTransport(handler))


def ok(body: bytes) -> Handler:
    return lambda request: httpx.Response(200, content=body)


def status(code: int) -> Handler:
    return lambda request: httpx.Response(code)


# --- parsing -----------------------------------------------------------------


def test_parse_nhk_fixture() -> None:
    articles = parse("nhk_cat0.xml", NHK)

    assert [a.id for a in articles] == ["a001", "a002", "a003"]
    first = articles[0]
    assert first.title == "ダミー見出し1 政府が新しい制度を発表"
    assert first.description == "これはテスト用のダミー本文1です。 二段落目のダミー本文です。"
    assert first.url == "https://news.web.nhk/newsweb/na/nd-20261009de00001"
    assert first.published_at == datetime(2026, 10, 8, 17, 49, 22, tzinfo=UTC)
    assert first.published_at.utcoffset() == timedelta(0)
    assert (first.feed_name, first.source, first.lang) == ("NHK 主要", "NHK", "ja")


def test_parse_bbc_japanese_cdata_and_raw_url() -> None:
    articles = parse("bbc_japanese.xml", BBC_JA)

    assert len(articles) == 2
    # The ideographic space in the source title is normalized like any other whitespace.
    assert articles[0].title == "ダミー見出しA 海外で選挙の結果が判明"
    # URL normalization is the filtering stage's job; the raw link is kept here.
    assert articles[0].url.endswith("?at_medium=RSS&at_campaign=rss")


def test_parse_guardian_strips_html_and_truncates() -> None:
    articles = parse("guardian_world.xml", GUARDIAN)

    long_desc, short_desc = articles[0].description, articles[1].description
    assert long_desc.startswith("Dummy standfirst sentence for testing & parsing. This is")
    assert len(long_desc) == 200
    assert long_desc.endswith("…")
    assert short_desc == "Short dummy standfirst. Continue reading..."
    for text in (long_desc, short_desc):
        assert "<" not in text
        assert "&amp;" not in text


def test_parse_feed_without_xml_declaration() -> None:
    articles = parse("aljazeera.xml", ALJAZEERA)

    assert [a.title for a in articles] == [
        "Dummy news headline with \u2018quotes\u2019",
        "Dummy sports headline",
    ]


def test_parse_drops_undated_articles_and_clamps_future_dates() -> None:
    content = rss(
        item("dated", "https://x.example/1", "Thu, 08 Oct 2026 12:00:00 +0000")
        + item("undated", "https://x.example/2", None)
        + item("future", "https://x.example/3", "Sat, 10 Oct 2026 12:00:00 +0000")
    )

    articles = parse_feed(content, GUARDIAN, NOW, description_max_chars=200, ids=article_ids())

    assert [a.title for a in articles] == ["dated", "future"]
    assert articles[1].published_at == NOW


def test_parse_drops_articles_without_title_or_link() -> None:
    content = rss(
        item("", "https://x.example/1", "Thu, 08 Oct 2026 12:00:00 +0000")
        + item("no link", "", "Thu, 08 Oct 2026 12:00:00 +0000")
    )

    assert parse_feed(content, GUARDIAN, NOW, description_max_chars=200, ids=article_ids()) == []


def test_parse_rejects_html_page() -> None:
    html = b"<!DOCTYPE html><html><head><title>Blocked</title></head><body>Denied</body></html>"

    with pytest.raises(NotAFeedError):
        parse_feed(html, NHK, NOW, description_max_chars=200, ids=article_ids())


# --- fetching ----------------------------------------------------------------


def test_client_sends_user_agent_and_follows_redirects() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/old.xml":
            return httpx.Response(301, headers={"Location": "https://nhk.example/cat0.xml"})
        return httpx.Response(200, content=fixture("nhk_cat0.xml"))

    old = make_feed("NHK 主要", "https://nhk.example/old.xml", source="NHK", lang="ja")
    with make_client(httpx.MockTransport(handler)) as client:
        [result] = fetch_all(client, [old], SELECTION, NOW)

    assert result.ok
    assert result.status_code == 200
    assert len(result.articles) == 3
    assert all(r.headers["User-Agent"] == USER_AGENT for r in seen)


def test_fetch_all_numbers_ids_across_feeds() -> None:
    routes = {
        str(NHK.url): ok(fixture("nhk_cat0.xml")),
        str(GUARDIAN.url): ok(fixture("guardian_world.xml")),
    }
    with client_for(routes) as client:
        results = fetch_all(client, [NHK, GUARDIAN], SELECTION, NOW)

    ids = [a.id for r in results for a in r.articles]
    assert ids == ["a001", "a002", "a003", "a004", "a005"]
    assert results[0].latest_at == datetime(2026, 10, 8, 17, 49, 22, tzinfo=UTC)


def test_one_failing_feed_does_not_stop_the_others() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    calls: dict[str, int] = {}
    routes = {
        str(NHK.url): status(500),
        str(BBC_JA.url): refuse,
        str(GUARDIAN.url): ok(fixture("guardian_world.xml")),
    }
    with client_for(routes, calls) as client:
        nhk, bbc, guardian = fetch_all(client, [NHK, BBC_JA, GUARDIAN], SELECTION, NOW)

    assert (nhk.error, nhk.status_code) == ("HTTPStatusError(status=500)", 500)
    assert (bbc.error, bbc.status_code) == ("ConnectError", None)
    assert guardian.ok
    assert len(guardian.articles) == 2
    # 5xx is retried once; connection errors are not.
    assert calls == {str(NHK.url): 2, str(BBC_JA.url): 1, str(GUARDIAN.url): 1}


def test_timeout_is_retried_once_then_succeeds() -> None:
    attempts = 0

    def flaky(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ReadTimeout("timed out", request=request)
        return httpx.Response(200, content=fixture("nhk_cat0.xml"))

    with client_for({str(NHK.url): flaky}) as client:
        [result] = fetch_all(client, [NHK], SELECTION, NOW)

    assert result.ok
    assert attempts == 2


def test_timeout_twice_fails() -> None:
    def hang(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out", request=request)

    calls: dict[str, int] = {}
    with client_for({str(NHK.url): hang}, calls) as client:
        [result] = fetch_all(client, [NHK], SELECTION, NOW)

    assert result.error == "ConnectTimeout"
    assert calls[str(NHK.url)] == 2


def test_client_error_is_not_retried() -> None:
    calls: dict[str, int] = {}
    with client_for({str(NHK.url): status(404)}, calls) as client:
        [result] = fetch_all(client, [NHK], SELECTION, NOW)

    assert (result.error, result.status_code) == ("HTTPStatusError(status=404)", 404)
    assert calls[str(NHK.url)] == 1


def test_html_body_with_200_is_a_failure_not_a_crash() -> None:
    html = b"<!DOCTYPE html><html><body><h1>Service unavailable</h1></body></html>"
    routes = {str(NHK.url): ok(html), str(GUARDIAN.url): ok(fixture("guardian_world.xml"))}
    with client_for(routes) as client:
        nhk, guardian = fetch_all(client, [NHK, GUARDIAN], SELECTION, NOW)

    assert not nhk.ok
    assert (nhk.status_code, nhk.error) == (200, "NotAFeedError")
    assert nhk.articles == ()
    assert guardian.ok


# --- staleness ---------------------------------------------------------------


def test_feed_with_only_old_articles_is_stale() -> None:
    old = rss(item("old", "https://x.example/1", "Sun, 04 Oct 2026 12:00:00 +0000"))
    with client_for({str(NHK.url): ok(old)}) as client:
        [result] = fetch_all(client, [NHK], SELECTION, NOW)

    assert result.error is None
    assert result.stale
    assert not result.ok
    assert len(result.articles) == 1


def test_feed_just_inside_stale_window_is_fresh() -> None:
    recent = rss(item("recent", "https://x.example/1", "Tue, 06 Oct 2026 01:00:00 +0000"))
    with client_for({str(NHK.url): ok(recent)}) as client:
        [result] = fetch_all(client, [NHK], SELECTION, NOW)

    assert not result.stale


def test_empty_feed_is_stale() -> None:
    with client_for({str(NHK.url): ok(rss(""))}) as client:
        [result] = fetch_all(client, [NHK], SELECTION, NOW)

    assert result.stale
    assert result.latest_at is None


# --- logging -----------------------------------------------------------------


def test_failure_logs_do_not_contain_urls(capsys: pytest.CaptureFixture[str]) -> None:
    # Same logging setup as production (SPEC §12.2): httpx itself logs request URLs at INFO.
    setup_logging()
    leaky_a = make_feed("A", "https://leak.example/LEAK-SENTINEL-A/api/webhooks/0")
    leaky_b = make_feed("B", "https://leak.example/LEAK-SENTINEL-B/api/webhooks/1")

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot reach {request.url}", request=request)

    routes = {str(leaky_a.url): status(503), str(leaky_b.url): refuse}
    with client_for(routes) as client:
        fetch_all(client, [leaky_a, leaky_b], SELECTION, NOW)

    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert "fetch_feed failed: feed=A HTTPStatusError(status=503)" in output
    assert "fetch_feed failed: feed=B ConnectError" in output
    assert "LEAK-SENTINEL" not in output
    assert "api/webhooks" not in output


def test_warns_when_every_nhk_feed_is_down(caplog: pytest.LogCaptureFixture) -> None:
    nhk_politics = make_feed("NHK 政治", "https://nhk.example/cat4.xml", source="NHK", lang="ja")
    routes = {
        str(NHK.url): status(404),
        str(nhk_politics.url): ok(rss("")),
        str(BBC_JA.url): ok(fixture("bbc_japanese.xml")),
    }
    with client_for(routes) as client:
        fetch_all(client, [NHK, nhk_politics, BBC_JA], SELECTION, NOW)

    assert "all NHK feeds failed or are stale" in caplog.text


def test_no_nhk_warning_while_one_nhk_feed_works(caplog: pytest.LogCaptureFixture) -> None:
    nhk_politics = make_feed("NHK 政治", "https://nhk.example/cat4.xml", source="NHK", lang="ja")
    routes = {str(NHK.url): status(404), str(nhk_politics.url): ok(fixture("nhk_cat0.xml"))}
    with client_for(routes) as client:
        fetch_all(client, [NHK, nhk_politics], SELECTION, NOW)

    assert "all NHK feeds" not in caplog.text
