import argparse
import json
import logging
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from support import ENV, NOW, FakeGemini, FakeNetwork, all_output, api_error, no_sleep

from daily_news import cli
from daily_news.config import load_feeds
from daily_news.curation import LlmOutput
from daily_news.feeds import make_client
from daily_news.models import FeedsFile
from daily_news.state import State, load_state, save_state


def test_help_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--help"])

    assert excinfo.value.code == 0
    assert "daily-news" in capsys.readouterr().out


GEMINI_OUTPUT = LlmOutput(
    text=json.dumps(
        {
            "picks": [
                {
                    "article_id": "a001",
                    "related_ids": ["a002"],
                    "region": "japan",
                    "importance": 3,
                    "headline_ja": "重要な見出し",
                    "summary_ja": "要約した。",
                    "reason": "影響が大きい",
                }
            ]
        },
        ensure_ascii=False,
    ),
    usage={"input": 1000, "output": 200, "thoughts": 300, "total": 1500},
    finish_reason="STOP",
)


def main(
    argv: list[str],
    state_path: Path,
    *,
    network: FakeNetwork | None = None,
    gemini: FakeGemini | None = None,
    environ: dict[str, str] = ENV,
    now: datetime = NOW,
) -> int:
    """``cli.main`` with fakes; ``--state`` always points at a temporary file."""
    return cli.main(
        [*argv, "--state", str(state_path)],
        environ=environ,
        transport=(network or FakeNetwork()).transport,
        gemini=(gemini or FakeGemini()).factory,
        now=lambda: now,
        sleep=no_sleep,
    )


def test_dry_run_with_gemini_reports_llm_mode(
    capsys: pytest.CaptureFixture[str], state_path: Path
) -> None:
    network = FakeNetwork()
    gemini = FakeGemini(GEMINI_OUTPUT)

    code = main(
        ["--dry-run"],
        state_path,
        network=network,
        gemini=gemini,
        environ=ENV | {"GEMINI_MODEL": "gemini-test"},
    )

    assert code == 0
    assert network.posts == []
    assert len(gemini.calls) == 1
    assert gemini.calls[0]["model"] == "gemini-test"
    assert gemini.keys == [ENV["GEMINI_API_KEY"]]
    result = json.loads(capsys.readouterr().out)
    report = result["report"]
    assert report["mode"] == "llm"
    assert report["model"] == "gemini-test"
    assert report["fallback_reason"] is None
    assert report["usage"]["input"] == 1000
    (japan,) = result["payloads"][0]["embeds"]
    assert japan["description"].startswith("🔴 **[重要な見出し]")
    assert "AI(gemini-test)" in japan["footer"]["text"]


def test_gemini_failure_falls_back_and_still_posts(state_path: Path) -> None:
    network = FakeNetwork()

    code = main([], state_path, network=network, gemini=FakeGemini(api_error(404, "NOT_FOUND")))

    assert code == 0
    (post,) = network.posts
    footer = json.loads(post.content)["embeds"][0]["footer"]["text"]
    assert footer.startswith("AI選定・要約なし(理由: モデル gemini-3.5-flash が見つからない(404))")


def test_missing_gemini_key_is_an_error_unless_no_llm(
    capsys: pytest.CaptureFixture[str], state_path: Path
) -> None:
    gemini = FakeGemini()

    code = main(
        ["--dry-run"],
        state_path,
        gemini=gemini,
        environ={"DISCORD_WEBHOOK_URL": ENV["DISCORD_WEBHOOK_URL"], "GEMINI_API_KEY": " "},
    )

    assert code == 1
    assert gemini.calls == []
    assert "GEMINI_API_KEY is not set" in capsys.readouterr().err


def test_no_llm_never_creates_a_gemini_client(
    capsys: pytest.CaptureFixture[str], state_path: Path
) -> None:
    gemini = FakeGemini()

    code = main(["--dry-run", "--no-llm"], state_path, gemini=gemini)

    assert code == 0
    assert gemini.keys == []
    assert json.loads(capsys.readouterr().out)["report"]["usage"] is None


def test_dry_run_prints_payload_and_report_without_posting(
    capsys: pytest.CaptureFixture[str], state_path: Path
) -> None:
    network = FakeNetwork()

    assert main(["--dry-run", "--no-llm"], state_path, network=network) == 0

    result = json.loads(capsys.readouterr().out)
    assert network.posts == []
    assert not state_path.exists()  # dry runs never write the state
    assert set(result) == {"payloads", "report"}
    payloads = result["payloads"]
    assert len(payloads) == 1
    assert [e["title"] for e in payloads[0]["embeds"]] == ["🇯🇵 日本", "🌏 世界"]
    assert payloads[0]["allowed_mentions"] == {"parse": []}
    report = result["report"]
    assert report["mode"] == "fallback"
    assert report["model"] is None
    assert report["fallback_reason"] == cli.NO_LLM_REASON
    assert isinstance(report["candidates"], int)
    assert report["candidates"] > 0
    assert len(report["feeds"]) == 12
    assert set(report["feeds"][0]) == {"name", "status", "articles", "stale", "error"}


def test_dry_run_does_not_need_the_gemini_key(
    capsys: pytest.CaptureFixture[str], state_path: Path
) -> None:
    code = main(
        ["--dry-run", "--no-llm"],
        state_path,
        environ={"DISCORD_WEBHOOK_URL": ENV["DISCORD_WEBHOOK_URL"], "GEMINI_API_KEY": ""},
    )

    assert code == 0
    assert json.loads(capsys.readouterr().out)["report"]["mode"] == "fallback"


def test_missing_webhook_url_fails_with_variable_name(
    capsys: pytest.CaptureFixture[str], state_path: Path
) -> None:
    code = main(["--dry-run", "--no-llm"], state_path, environ={"DISCORD_WEBHOOK_URL": "  "})

    assert code == 1
    assert "DISCORD_WEBHOOK_URL is not set" in capsys.readouterr().err


def test_posting_sends_the_payload_once(
    capsys: pytest.CaptureFixture[str], state_path: Path
) -> None:
    network = FakeNetwork()

    code = main(["--no-llm"], state_path, network=network)

    assert code == 0
    assert len(network.posts) == 1
    assert network.posts[0].url.params["wait"] == "true"
    assert json.loads(network.posts[0].content)["embeds"]
    assert capsys.readouterr().out == ""


SENTINEL_OUTPUT = LlmOutput(
    text=json.dumps(
        {
            "picks": [
                {
                    "article_id": "a001",
                    "related_ids": [],
                    "region": "japan",
                    "importance": 3,
                    "headline_ja": "HEADLINE-SENTINEL",
                    "summary_ja": "SUMMARY-SENTINEL",
                    "reason": "REASON-SENTINEL",
                }
            ]
        }
    ),
    usage={"input": 1000, "output": 200, "thoughts": 300, "total": 1500},
    finish_reason="STOP",
)


@pytest.mark.parametrize("argv", [[], ["--no-llm"]])
def test_logs_contain_no_article_text(
    argv: list[str],
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
    state_path: Path,
) -> None:
    # Logs are public in a public repo (SPEC §9.5). Every fixture title and description
    # contains "dummy" or "ダミー".
    caplog.set_level(logging.INFO)
    network = FakeNetwork()

    code = main(argv, state_path, network=network, gemini=FakeGemini(SENTINEL_OUTPUT))

    assert code == 0
    posted = network.posts[0].content.decode()
    assert "ダミー" in posted or "SENTINEL" in posted  # the post itself carries the text
    captured = capsys.readouterr()
    output = all_output(captured.out, captured.err, caplog.records)
    assert "pick: id=" in output or "fallback:" in output
    for text in ("dummy", "ダミー", "sentinel"):
        assert text not in output.lower()


# --- state.json and the double-post guard (SPEC §9.1, §9.2) ---


def test_first_successful_post_creates_the_state_file(state_path: Path) -> None:
    network = FakeNetwork()
    assert not state_path.exists()

    assert main(["--no-llm"], state_path, network=network) == 0

    state = load_state(state_path)
    assert state.last_posted_date == date(2026, 10, 9)
    assert state.last_posted_at == NOW
    posted = json.loads(network.posts[0].content)
    links = [
        line.split("](", 1)[1].split(")**", 1)[0]
        for embed in posted["embeds"]
        for line in embed["description"].split("\n\n")
    ]
    assert set(links) <= set(state.posted_urls)
    assert set(state.posted_urls.values()) == {date(2026, 10, 9)}


def test_post_failure_exits_one_and_writes_no_state(state_path: Path) -> None:
    network = FakeNetwork(lambda request: httpx.Response(404, json={"code": 10015}))

    code = main(["--no-llm"], state_path, network=network)

    assert code == 1
    assert len(network.posts) == 1
    assert not state_path.exists()


def test_post_failure_keeps_the_previous_state(state_path: Path) -> None:
    save_state(state_path, State(last_posted_date=date(2026, 10, 8)))
    before = state_path.read_bytes()
    network = FakeNetwork(lambda request: httpx.Response(500))

    assert main(["--no-llm"], state_path, network=network) == 1
    assert state_path.read_bytes() == before


def test_already_posted_today_skips_without_fetching(
    capsys: pytest.CaptureFixture[str], state_path: Path
) -> None:
    save_state(state_path, State(last_posted_date=date(2026, 10, 9)))
    before = state_path.read_bytes()
    requests: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(500)

    network = FakeNetwork()
    network.transport = httpx.MockTransport(record)

    assert main(["--no-llm"], state_path, network=network) == 0

    assert requests == []
    assert state_path.read_bytes() == before
    assert capsys.readouterr().out == ""


def test_force_posts_again_on_the_same_day(state_path: Path) -> None:
    save_state(state_path, State(last_posted_date=date(2026, 10, 9)))
    network = FakeNetwork()

    assert main(["--no-llm", "--force"], state_path, network=network) == 0

    assert len(network.posts) == 1


def test_running_twice_posts_once(state_path: Path) -> None:
    network = FakeNetwork()

    assert main(["--no-llm"], state_path, network=network) == 0
    before = state_path.read_bytes()
    assert main(["--no-llm"], state_path, network=network) == 0

    assert len(network.posts) == 1
    assert state_path.read_bytes() == before


@pytest.mark.parametrize(
    ("posted_at_utc", "now_utc", "skipped"),
    [
        # Posted 06:07 JST on 10-09 (21:07 UTC on 10-08); still 10-09 in JST at 23:59 UTC 10-08.
        (datetime(2026, 10, 8, 21, 7, tzinfo=UTC), datetime(2026, 10, 8, 23, 59, tzinfo=UTC), True),
        # Same JST day, later the same morning.
        (datetime(2026, 10, 8, 21, 7, tzinfo=UTC), datetime(2026, 10, 9, 1, 0, tzinfo=UTC), True),
        # Posted 06:07 JST 10-09; 23:59 UTC 10-09 is 08:59 JST 10-10, a new day.
        (
            datetime(2026, 10, 8, 21, 7, tzinfo=UTC),
            datetime(2026, 10, 9, 23, 59, tzinfo=UTC),
            False,
        ),
        # 14:59 UTC 10-09 is 23:59 JST 10-09 (same day); 15:00 UTC is 00:00 JST 10-10.
        (datetime(2026, 10, 8, 21, 7, tzinfo=UTC), datetime(2026, 10, 9, 14, 59, tzinfo=UTC), True),
        (datetime(2026, 10, 8, 21, 7, tzinfo=UTC), datetime(2026, 10, 9, 15, 0, tzinfo=UTC), False),
    ],
)
def test_today_is_decided_in_jst(
    state_path: Path, posted_at_utc: datetime, now_utc: datetime, skipped: bool
) -> None:
    network = FakeNetwork()
    assert main(["--no-llm"], state_path, network=network, now=posted_at_utc) == 0
    assert load_state(state_path).last_posted_date == date(2026, 10, 9)

    assert main(["--no-llm"], state_path, network=network, now=now_utc) == 0

    assert len(network.posts) == (1 if skipped else 2)


def test_dry_run_respects_the_daily_guard_unless_forced(
    capsys: pytest.CaptureFixture[str], state_path: Path
) -> None:
    save_state(state_path, State(last_posted_date=date(2026, 10, 9)))
    before = state_path.read_bytes()

    assert main(["--dry-run", "--no-llm"], state_path) == 0
    assert capsys.readouterr().out == ""
    assert main(["--dry-run", "--no-llm", "--force"], state_path) == 0
    assert json.loads(capsys.readouterr().out)["report"]["mode"] == "fallback"
    assert state_path.read_bytes() == before


def test_posted_urls_are_excluded_and_last_post_extends_the_window(
    capsys: pytest.CaptureFixture[str], state_path: Path
) -> None:
    # First a normal dry run to learn which URLs the fallback picks.
    assert main(["--dry-run", "--no-llm"], state_path) == 0
    first = json.loads(capsys.readouterr().out)["payloads"][0]["embeds"][0]["description"]
    first_url = first.split("](", 1)[1].split(")**", 1)[0]

    # Yesterday's post included that URL; the window reaches back to it (40 h > 30 h).
    save_state(
        state_path,
        State(
            last_posted_date=date(2026, 10, 8),
            last_posted_at=NOW - timedelta(hours=40),
            posted_urls={first_url: date(2026, 10, 8)},
        ),
    )
    assert main(["--dry-run", "--no-llm"], state_path) == 0
    second = json.loads(capsys.readouterr().out)["payloads"][0]["embeds"][0]["description"]

    assert first_url not in second


def test_state_save_failure_after_posting_exits_one(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], state_path: Path
) -> None:
    def fail(path: Path, state: State) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(cli, "save_state", fail)
    network = FakeNetwork()

    assert main(["--no-llm"], state_path, network=network) == 1
    assert len(network.posts) == 1
    assert "state save failed after posting: OSError" in capsys.readouterr().err


def test_setup_logging_quiets_url_logging_libraries() -> None:
    cli.setup_logging()

    assert {"httpx", "httpcore", "google_genai", "google.genai"} <= set(cli.QUIET_LOGGERS)

    assert logging.getLogger().level == logging.INFO
    for name in cli.QUIET_LOGGERS:
        assert logging.getLogger(name).level == logging.WARNING


def test_unexpected_exception_is_logged_without_message(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def boom(args: argparse.Namespace, **kwargs: object) -> int:
        raise RuntimeError("https://discord.com/api/webhooks/0/LEAK-SENTINEL-WEBHOOK")

    monkeypatch.setattr(cli, "run", boom)

    assert cli.main(["--state", "/nonexistent/state.json"]) == 1
    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert "RuntimeError" in output
    assert "LEAK-SENTINEL" not in output
    assert "api/webhooks" not in output
    assert "Traceback" not in output


FIXTURES = Path(__file__).parent / "fixtures"
OLD_RSS = (
    b'<?xml version="1.0"?><rss version="2.0"><channel><title>t</title><item><title>old</title>'
    b"<link>https://x.example/1</link><pubDate>Sun, 04 Oct 2026 12:00:00 +0000</pubDate>"
    b"</item></channel></rss>"
)


def feeds_file(*urls: str) -> FeedsFile:
    return FeedsFile.model_validate(
        {
            "selection": load_feeds(Path(__file__).parent.parent / "feeds.toml").selection,
            "feeds": [
                {"name": f"Feed {i}", "url": url, "lang": "ja", "source": "Test"}
                for i, url in enumerate(urls)
            ],
        }
    )


def serve(routes: dict[str, bytes]) -> httpx.Client:
    return make_client(
        httpx.MockTransport(lambda request: httpx.Response(200, content=routes[str(request.url)]))
    )


def test_check_feeds_succeeds_when_all_feeds_are_fresh(capsys: pytest.CaptureFixture[str]) -> None:
    nhk = (FIXTURES / "nhk_cat0.xml").read_bytes()
    with serve({"https://a.example/rss": nhk}) as client:
        code = cli.check_feeds(feeds_file("https://a.example/rss"), client, NOW)

    out = capsys.readouterr().out
    assert code == 0
    assert "Feed 0" in out
    assert "2026-10-09 02:49" in out  # newest article shown in JST
    assert out.splitlines()[1].endswith("ok")


def test_check_feeds_fails_when_a_feed_is_stale(capsys: pytest.CaptureFixture[str]) -> None:
    nhk = (FIXTURES / "nhk_cat0.xml").read_bytes()
    routes = {"https://a.example/rss": nhk, "https://b.example/rss": OLD_RSS}
    with serve(routes) as client:
        code = cli.check_feeds(
            feeds_file("https://a.example/rss", "https://b.example/rss"), client, NOW
        )

    lines = capsys.readouterr().out.splitlines()
    assert code == 1
    assert lines[2].startswith("Feed 1")
    assert lines[2].endswith("STALE")


def test_check_feeds_fails_when_a_feed_is_not_xml(capsys: pytest.CaptureFixture[str]) -> None:
    with serve({"https://a.example/rss": b"<html><body>blocked</body></html>"}) as client:
        code = cli.check_feeds(feeds_file("https://a.example/rss"), client, NOW)

    assert code == 1
    assert capsys.readouterr().out.splitlines()[1].endswith("FAILED NotAFeedError")


def test_check_feeds_option_is_wired(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[FeedsFile] = []

    def fake_check(feeds: FeedsFile, client: httpx.Client, now: datetime) -> int:
        seen.append(feeds)
        assert now.tzinfo is not None
        return 1

    monkeypatch.setattr(cli, "check_feeds", fake_check)

    assert cli.main(["--check-feeds"]) == 1
    assert len(seen[0].feeds) == 12
