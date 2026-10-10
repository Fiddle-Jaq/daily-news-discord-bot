"""Secrets never reach logs, stdout/stderr or the dry-run output (SPEC §9.7, §12.2)."""

import json
import logging
import tempfile
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from support import (
    API_KEY,
    ENV,
    NOW,
    WEBHOOK_URL,
    FakeGemini,
    FakeNetwork,
    all_output,
    api_error,
    no_sleep,
    quota_error,
)

from daily_news import cli
from daily_news.config import load_settings
from daily_news.curation import LlmOutput
from daily_news.errors import ConfigError, describe_error


def assert_no_secret(text: str) -> None:
    assert "LEAK-SENTINEL" not in text
    assert "api/webhooks" not in text


def test_http_status_error_keeps_only_class_and_status() -> None:
    request = httpx.Request("POST", WEBHOOK_URL)
    response = httpx.Response(503, request=request)
    exc = httpx.HTTPStatusError(
        f"Server error for url '{WEBHOOK_URL}'", request=request, response=response
    )

    described = describe_error(exc)

    assert described == "HTTPStatusError(status=503)"
    assert_no_secret(described)


def test_connect_error_keeps_only_class_name() -> None:
    exc = httpx.ConnectError(f"failed to connect to {WEBHOOK_URL}")

    described = describe_error(exc)

    assert described == "ConnectError"
    assert_no_secret(described)


def test_timeout_keeps_only_class_name() -> None:
    exc = httpx.ReadTimeout(f"timed out reading {WEBHOOK_URL}")

    assert describe_error(exc) == "ReadTimeout"


def test_unexpected_exception_keeps_only_class_name() -> None:
    exc = RuntimeError(f"boom {WEBHOOK_URL} key={API_KEY}")

    described = describe_error(exc)

    assert described == "RuntimeError"
    assert_no_secret(described)


def test_config_error_keeps_its_safe_message() -> None:
    assert describe_error(ConfigError("DISCORD_WEBHOOK_URL is not set")) == (
        "ConfigError(DISCORD_WEBHOOK_URL is not set)"
    )


def test_settings_repr_and_str_hide_secrets() -> None:
    settings = load_settings(ENV)

    for text in (repr(settings), str(settings), repr(settings.model_dump())):
        assert_no_secret(text)
    assert settings.discord_webhook_url.get_secret_value() == WEBHOOK_URL


def run_main(
    network: FakeNetwork,
    argv: list[str],
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
    gemini: FakeGemini | None = None,
) -> tuple[int, str]:
    caplog.set_level(logging.DEBUG, logger="daily_news")
    with tempfile.TemporaryDirectory() as state_dir:
        code = cli.main(
            [*argv, "--state", str(Path(state_dir) / "state.json")],
            environ=ENV,
            transport=network.transport,
            gemini=(gemini or FakeGemini()).factory,
            now=lambda: NOW,
            sleep=no_sleep,
        )
    captured = capsys.readouterr()
    assert caplog.records, "setup_logging must keep caplog's handler"
    assert captured.err, "logs must reach stderr"
    return code, all_output(captured.out, captured.err, caplog.records)


def respond(status: int) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"message": f"bad {request.url}"})

    return handler


@pytest.mark.parametrize("status", [400, 401, 404, 500, 503])
def test_discord_error_status_does_not_leak(
    status: int, capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    network = FakeNetwork(respond(status))

    code, output = run_main(network, ["--no-llm"], capsys, caplog)

    assert code == 1
    assert f"HTTPStatusError(status={status})" in output
    assert "discord_post" in output
    assert_no_secret(output)
    assert "Traceback" not in output


def test_discord_connect_error_does_not_leak(
    capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot connect to {request.url}", request=request)

    network = FakeNetwork(refuse)

    code, output = run_main(network, ["--no-llm"], capsys, caplog)

    assert code == 1
    assert len(network.posts) == 2  # retried once
    assert "ConnectError" in output
    assert_no_secret(output)


def test_unexpected_exception_reaching_main_does_not_leak(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    def boom(*args: object, **kwargs: object) -> object:
        raise RuntimeError(f"unexpected {WEBHOOK_URL} key={API_KEY}")

    monkeypatch.setattr(cli, "build_payload", boom)

    code, output = run_main(FakeNetwork(), ["--no-llm"], capsys, caplog)

    assert code == 1
    assert "unexpected failure: RuntimeError" in output
    assert_no_secret(output)
    assert "Traceback" not in output


def test_dry_run_does_not_leak(
    capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    network = FakeNetwork()

    code, output = run_main(network, ["--dry-run", "--no-llm"], capsys, caplog)

    assert code == 0
    assert network.posts == []
    assert '"payloads"' in output
    assert_no_secret(output)


def test_gemini_error_keeps_only_code_status_and_quota_facts() -> None:
    exc = quota_error("GenerateRequestsPerDayPerProjectPerModel-FreeTier", "17s")

    described = describe_error(exc)

    assert described == (
        "ClientError(code=429 status=RESOURCE_EXHAUSTED "
        "quota_ids=GenerateRequestsPerDayPerProjectPerModel-FreeTier retry_delay=17s)"
    )


def test_gemini_error_message_is_not_described() -> None:
    exc = api_error(400, "INVALID_ARGUMENT", message=f"API key not valid: {API_KEY}")

    assert API_KEY in str(exc)  # the SDK's own message would leak it
    assert_no_secret(describe_error(exc))


@pytest.mark.parametrize(
    "error",
    [
        api_error(400, "INVALID_ARGUMENT", message=f"API key not valid: {API_KEY}"),
        api_error(
            503, "UNAVAILABLE", message=f"overloaded https://x.example/?key={API_KEY} {WEBHOOK_URL}"
        ),
        quota_error("GenerateRequestsPerDayPerProjectPerModel-FreeTier"),
    ],
)
def test_gemini_api_error_with_key_does_not_leak(
    error: Exception, capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    network = FakeNetwork()
    gemini = FakeGemini(error, error)

    code, output = run_main(network, ["--dry-run"], capsys, caplog, gemini)

    assert code == 0  # falls back
    assert '"mode": "fallback"' in output
    assert_no_secret(output)


def test_gemini_dry_run_does_not_leak(
    capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    gemini = FakeGemini(
        LlmOutput(
            text=json.dumps(
                {
                    "picks": [
                        {
                            "article_id": "a001",
                            "related_ids": [],
                            "region": "japan",
                            "importance": 2,
                            "headline_ja": "見出し",
                            "summary_ja": "要約。",
                            "reason": "r",
                        }
                    ]
                }
            )
        )
    )

    code, output = run_main(FakeNetwork(), ["--dry-run"], capsys, caplog, gemini)

    assert code == 0
    assert '"mode": "llm"' in output
    assert_no_secret(output)
