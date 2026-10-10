"""Shared test helpers: a fake network serving feed fixtures and a Discord webhook."""

import logging
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from google.genai import errors as genai_errors
from pydantic import SecretStr

from daily_news.curation import Generator, LlmOutput

FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 10, 9, 0, 0, tzinfo=UTC)

WEBHOOK_URL = "https://discord.com/api/webhooks/0/LEAK-SENTINEL-WEBHOOK"
API_KEY = "LEAK-SENTINEL-KEY"
ENV = {"DISCORD_WEBHOOK_URL": WEBHOOK_URL, "GEMINI_API_KEY": API_KEY}

_FEED_FIXTURES = {
    "news.web.nhk": "nhk_cat0.xml",
    "www.theguardian.com": "guardian_world.xml",
    "www.aljazeera.com": "aljazeera.xml",
}


def feed_fixture(url: httpx.URL) -> bytes:
    if url.host == "feeds.bbci.co.uk":
        name = "bbc_japanese.xml" if url.path.startswith("/japanese") else "guardian_world.xml"
    else:
        name = _FEED_FIXTURES.get(url.host, "aljazeera.xml")
    return (FIXTURES / name).read_bytes()


def ok_message(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"id": "1"})


class FakeNetwork:
    """Feeds answer with fixtures; requests to discord.com go to ``discord`` and are recorded."""

    def __init__(self, discord: Callable[[httpx.Request], httpx.Response] = ok_message) -> None:
        self.discord = discord
        self.posts: list[httpx.Request] = []
        self.transport = httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "discord.com":
            self.posts.append(request)
            return self.discord(request)
        return httpx.Response(200, content=feed_fixture(request.url))


def no_sleep(seconds: float) -> None:
    pass


def all_output(out: str, err: str, records: list[logging.LogRecord]) -> str:
    return out + err + "\n".join(r.getMessage() for r in records)


def api_error(
    code: int, status: str, details: Sequence[dict[str, Any]] = (), message: str = "error"
) -> genai_errors.APIError:
    """A Gemini error shaped like the REST error body."""
    body = {"error": {"code": code, "message": message, "status": status, "details": [*details]}}
    if code >= 500:
        return genai_errors.ServerError(code, body)
    return genai_errors.ClientError(code, body)


def quota_error(quota_id: str, retry_delay: str | None = None) -> genai_errors.APIError:
    details: list[dict[str, Any]] = [
        {
            "@type": "type.googleapis.com/google.rpc.QuotaFailure",
            "violations": [{"quotaMetric": "generate_content_requests", "quotaId": quota_id}],
        }
    ]
    if retry_delay is not None:
        details.append(
            {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": retry_delay}
        )
    return api_error(429, "RESOURCE_EXHAUSTED", details)


class FakeGemini:
    """Returns the queued outputs (or raises the queued errors) in order and records calls."""

    def __init__(self, *responses: LlmOutput | Exception) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, str]] = []
        self.keys: list[str] = []

    def __call__(self, *, model: str, system_instruction: str, prompt: str) -> LlmOutput:
        self.calls.append(
            {"model": model, "system_instruction": system_instruction, "prompt": prompt}
        )
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    @contextmanager
    def factory(self, api_key: SecretStr) -> Iterator[Generator]:
        self.keys.append(api_key.get_secret_value())
        yield self
