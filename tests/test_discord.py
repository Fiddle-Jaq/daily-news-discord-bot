import json

import httpx
import pytest
from pydantic import SecretStr
from support import NOW, WEBHOOK_URL

from daily_news.discord import (
    DESCRIPTION_MAX_CHARS,
    EMBEDS_TOTAL_MAX_CHARS,
    MAX_RETRY_AFTER_SECONDS,
    Payload,
    build_payload,
    escape_markdown,
    escape_url,
    format_header,
    post_payload,
)
from daily_news.feeds import make_client
from daily_news.models import Digest, DigestItem, Importance


def item(
    title: str = "見出し",
    *,
    summary: str | None = None,
    importance: Importance | None = None,
    url: str = "https://news.example/1",
    source: str = "NHK",
) -> DigestItem:
    return DigestItem(title=title, url=url, source=source, summary=summary, importance=importance)


def llm_digest(japan: tuple[DigestItem, ...] = (), world: tuple[DigestItem, ...] = ()) -> Digest:
    return Digest(mode="llm", model="gemini-test", japan=japan, world=world)


def embed_chars(payload: Payload) -> int:
    return sum(
        len(e["title"]) + len(e["description"]) + len(e["footer"]["text"])
        for e in payload["embeds"]
    )


def test_escape_markdown_escapes_special_characters() -> None:
    assert escape_markdown("a*b_c~d`e|f>g[h](i)\\j") == (
        "a\\*b\\_c\\~d\\`e\\|f\\>g\\[h\\]\\(i\\)\\\\j"
    )


def test_escape_url_encodes_closing_parenthesis() -> None:
    assert escape_url("https://x.example/a_(b)") == "https://x.example/a_(b%29"


def test_header_uses_the_jst_date() -> None:
    # 2026-10-09 00:00 UTC is Friday 09:00 JST.
    assert format_header(NOW) == "📰 **2026年10月9日(金) 朝のニュース**"


def test_llm_payload_layout() -> None:
    digest = llm_digest(
        japan=(
            item("重大[速報]", summary="要約*です", importance=3, url="https://n.example/a)"),
            item("通常", summary="要約2", importance=2),
        ),
        world=(item("World", summary="s", importance=1, source="BBC"),),
    )

    payload = build_payload(digest, NOW)

    assert payload["allowed_mentions"] == {"parse": []}
    japan, world = payload["embeds"]
    assert japan["title"] == "🇯🇵 日本"
    assert world["title"] == "🌏 世界"
    assert japan["color"] != world["color"]
    first, second = japan["description"].split("\n\n")
    assert first == "🔴 **[重大\\[速報\\]](https://n.example/a%29)**\n要約\\*です (NHK)"
    assert second == "**[通常](https://news.example/1)**\n要約2 (NHK)"
    assert "🔴" not in world["description"]
    assert "AI(gemini-test)" in japan["footer"]["text"]
    assert "元記事を確認" in world["footer"]["text"]


def test_fallback_payload_has_title_links_and_reason_footer() -> None:
    digest = Digest(
        mode="fallback",
        fallback_reason="--no-llm 指定",
        world=(item("Plain title", source="BBC"),),
    )

    payload = build_payload(digest, NOW)

    (world,) = payload["embeds"]
    assert world["title"] == "🌏 世界"
    assert world["description"] == "**[Plain title](https://news.example/1)** (BBC)"
    assert world["footer"]["text"] == (
        "AI選定・要約なし(理由: --no-llm 指定)。各社RSSの新着から機械的に抽出"
    )


def test_no_items_posts_no_news_message(caplog: pytest.LogCaptureFixture) -> None:
    payload = build_payload(llm_digest(), NOW)

    assert payload["embeds"] == []
    assert payload["content"].endswith("本日は該当ニュースなし")
    assert "no news items" in caplog.text


def test_mentions_in_titles_are_not_parsed() -> None:
    payload = build_payload(llm_digest(japan=(item("@everyone 速報"),)), NOW)

    assert payload["allowed_mentions"] == {"parse": []}


def long_url(n: int) -> str:
    return f"https://www.theguardian.com/world/2026/oct/08/{'x' * 80}-{n}"


@pytest.mark.parametrize("mode", ["llm", "fallback"])
def test_maximum_items_at_maximum_length_fit_in_one_message(mode: str) -> None:
    def items(count: int) -> tuple[DigestItem, ...]:
        if mode == "llm":
            return tuple(
                item(
                    "見" * 40, summary="要" * 60, importance=3, url=long_url(n), source="Al Jazeera"
                )
                for n in range(count)
            )
        return tuple(item("T" * 100, url=long_url(n), source="Japan Times") for n in range(count))

    digest = Digest(
        mode="llm" if mode == "llm" else "fallback",
        model="gemini-3.5-flash" if mode == "llm" else None,
        fallback_reason=None if mode == "llm" else "--no-llm 指定",
        japan=items(7),
        world=items(4),
    )

    payload = build_payload(digest, NOW)

    assert [len(e["description"].split("\n\n")) for e in payload["embeds"]] == [7, 4]
    assert embed_chars(payload) <= EMBEDS_TOTAL_MAX_CHARS


def test_over_limit_drops_the_last_items(caplog: pytest.LogCaptureFixture) -> None:
    huge = tuple(item(f"{n}" + "長" * 900, importance=2) for n in range(8))
    digest = llm_digest(japan=huge, world=(item("w"),))

    payload = build_payload(digest, NOW)

    japan, world = payload["embeds"]
    lines = japan["description"].split("\n\n")
    assert len(japan["description"]) <= DESCRIPTION_MAX_CHARS
    assert embed_chars(payload) <= EMBEDS_TOTAL_MAX_CHARS
    assert [line.split("長")[0] for line in lines] == [
        f"**[{n}" for n in range(len(lines))
    ]  # the first items survive
    assert world["description"].startswith("**[w]")
    assert "dropped the last item" in caplog.text


def test_over_total_limit_trims_the_longest_embed() -> None:
    japan = tuple(item("日" * 700) for _ in range(5))
    world = tuple(item("世" * 700) for _ in range(4))

    payload = build_payload(llm_digest(japan=japan, world=world), NOW)

    assert embed_chars(payload) <= EMBEDS_TOTAL_MAX_CHARS
    counts = [len(e["description"].split("\n\n")) for e in payload["embeds"]]
    assert counts[1] == 4  # the shorter embed is untouched
    assert counts[0] < 5


class Recorder:
    def __init__(self, *responses: httpx.Response | Exception) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []
        self.sleeps: list[float] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def post(self) -> None:
        with make_client(httpx.MockTransport(self.handle)) as client:
            post_payload(client, SecretStr(WEBHOOK_URL), {"content": "x"}, sleep=self.sleeps.append)


def test_post_uses_wait_true_and_sends_json() -> None:
    recorder = Recorder(httpx.Response(200, json={"id": "1"}))

    recorder.post()

    (request,) = recorder.requests
    assert request.method == "POST"
    assert request.url.params["wait"] == "true"
    assert request.url.path == "/api/webhooks/0/LEAK-SENTINEL-WEBHOOK"
    assert json.loads(request.content) == {"content": "x"}
    assert recorder.sleeps == []


def test_429_waits_retry_after_and_retries_once() -> None:
    recorder = Recorder(
        httpx.Response(429, json={"retry_after": 1.5, "global": False}),
        httpx.Response(200, json={"id": "1"}),
    )

    recorder.post()

    assert len(recorder.requests) == 2
    assert recorder.sleeps == [1.5]


def test_429_wait_is_capped() -> None:
    recorder = Recorder(
        httpx.Response(429, json={"retry_after": 3600}), httpx.Response(200, json={"id": "1"})
    )

    recorder.post()

    assert recorder.sleeps == [MAX_RETRY_AFTER_SECONDS]


def test_429_twice_raises() -> None:
    recorder = Recorder(
        httpx.Response(429, json={"retry_after": 1}), httpx.Response(429, json={"retry_after": 1})
    )

    with pytest.raises(httpx.HTTPStatusError):
        recorder.post()
    assert len(recorder.requests) == 2


@pytest.mark.parametrize(
    "first", [httpx.Response(503), httpx.ConnectError("refused"), httpx.ReadTimeout("slow")]
)
def test_server_and_network_errors_retry_after_5_seconds(first: httpx.Response | Exception) -> None:
    recorder = Recorder(first, httpx.Response(200, json={"id": "1"}))

    recorder.post()

    assert len(recorder.requests) == 2
    assert recorder.sleeps == [5.0]


def test_network_error_twice_raises() -> None:
    recorder = Recorder(httpx.ConnectError("refused"), httpx.ConnectError("refused"))

    with pytest.raises(httpx.ConnectError):
        recorder.post()


@pytest.mark.parametrize("status", [400, 401, 404])
def test_client_errors_are_not_retried(status: int) -> None:
    recorder = Recorder(httpx.Response(status))

    with pytest.raises(httpx.HTTPStatusError):
        recorder.post()
    assert len(recorder.requests) == 1
    assert recorder.sleeps == []
