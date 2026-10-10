"""Command-line entry point (`daily-news`)."""

import argparse
import json
import logging
import os
import sys
import time
import unicodedata
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO
from zoneinfo import ZoneInfo

import httpx
from pydantic import SecretStr

from daily_news.config import load_feeds, load_settings, require_gemini_key
from daily_news.curation import (
    CurationFailed,
    GeminiGenerator,
    Generator,
    LlmOutput,
    curate,
    fallback_digest,
    make_gemini_client,
)
from daily_news.discord import build_payload, post_payload
from daily_news.errors import describe_error
from daily_news.feeds import fetch_all, make_client
from daily_news.filtering import FilterResult, filter_candidates
from daily_news.models import Digest, FeedResult, FeedsFile
from daily_news.state import load_state, posted_today, record_post, save_state

logger = logging.getLogger(__name__)

# These libraries log request URLs (which may contain secrets) at INFO/DEBUG (SPEC §9.7).
# google-genai logs under "google_genai.*", plus "google.genai" when GOOGLE_GENAI_DEBUG is set.
QUIET_LOGGERS = ("httpx", "httpcore", "google_genai", "google.genai")

FEEDS_PATH = Path("feeds.toml")
DEFAULT_STATE_PATH = Path("state.json")
JST = ZoneInfo("Asia/Tokyo")
NO_LLM_REASON = "--no-llm 指定"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="daily-news",
        description="Curate the day's important Japan and world news and post it to Discord.",
    )
    parser.add_argument(
        "--check-feeds",
        action="store_true",
        help="show HTTP status, article count, newest date and staleness of every feed; "
        "exit 1 if any feed failed or is stale",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the Discord payloads and a report as JSON instead of posting",
    )
    parser.add_argument(
        "--no-llm",
        action="store_true",
        help="skip Gemini and use the mechanical fallback selection",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="run even if today's (JST) news has already been posted",
    )
    parser.add_argument(
        "--state",
        type=Path,
        default=DEFAULT_STATE_PATH,
        metavar="PATH",
        help=f"state file to read and update (default: {DEFAULT_STATE_PATH})",
    )
    return parser


class _StderrHandler(logging.StreamHandler[TextIO]):
    """Marker type so ``setup_logging`` replaces only its own handler."""


def setup_logging() -> None:
    """Configure logging to stderr; never set the root logger to DEBUG.

    Only a previously installed handler of ours is replaced; other root handlers
    (e.g. pytest's caplog) are left alone.
    """
    root = logging.getLogger()
    for handler in [h for h in root.handlers if isinstance(h, _StderrHandler)]:
        root.removeHandler(handler)
    handler = _StderrHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    for name in QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


def _display_width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


def _pad(text: str, width: int) -> str:
    return text + " " * max(width - _display_width(text), 0)


def format_feed_table(results: Sequence[FeedResult]) -> str:
    rows = [("FEED", "HTTP", "ARTICLES", "LATEST (JST)", "STATE")]
    for r in results:
        state = f"FAILED {r.error}" if r.error else "STALE" if r.stale else "ok"
        latest = r.latest_at.astimezone(JST).strftime("%Y-%m-%d %H:%M") if r.latest_at else "-"
        status = str(r.status_code) if r.status_code is not None else "-"
        rows.append((r.feed.name, status, str(len(r.articles)), latest, state))
    widths = [max(_display_width(row[i]) for row in rows) for i in range(len(rows[0]) - 1)]
    return "\n".join(
        "  ".join([*(_pad(cell, w) for cell, w in zip(row, widths, strict=False)), row[-1]])
        for row in rows
    )


def check_feeds(feeds_file: FeedsFile, client: httpx.Client, now: datetime) -> int:
    """Print the feed health table; exit code 1 if any feed failed or is stale."""
    results = fetch_all(client, feeds_file.feeds, feeds_file.selection, now)
    print(format_feed_table(results))
    return 0 if all(r.ok for r in results) else 1


def build_report(
    digest: Digest,
    candidates: int,
    results: Sequence[FeedResult],
    llm_output: LlmOutput | None,
) -> dict[str, Any]:
    """The ``report`` part of the ``--dry-run`` output (SPEC §10). Contains no secrets."""
    return {
        "mode": digest.mode,
        "model": digest.model,
        "fallback_reason": digest.fallback_reason,
        "candidates": candidates,
        "japan": len(digest.japan),
        "world": len(digest.world),
        "usage": llm_output.usage if llm_output else None,
        "feeds": [
            {
                "name": r.feed.name,
                "status": r.status_code,
                "articles": len(r.articles),
                "stale": r.stale,
                "error": r.error,
            }
            for r in results
        ],
    }


GeminiFactory = Callable[[SecretStr], AbstractContextManager[Generator]]


@contextmanager
def gemini_generator(api_key: SecretStr) -> Iterator[Generator]:
    with make_gemini_client(api_key) as client:
        yield GeminiGenerator(client)


def select(
    args: argparse.Namespace,
    filtered: FilterResult,
    feeds_file: FeedsFile,
    *,
    model: str,
    api_key: SecretStr | None,
    gemini: GeminiFactory,
    now: datetime,
    sleep: Callable[[float], None],
) -> tuple[Digest, LlmOutput | None]:
    """Gemini curation, or the mechanical fallback when disabled or failed (SPEC §7.5, §7.7)."""

    def fallback(reason: str) -> tuple[Digest, LlmOutput | None]:
        digest = fallback_digest(filtered.eligible, feeds_file.feeds, feeds_file.selection, reason)
        return digest, None

    if args.no_llm or api_key is None:
        return fallback(NO_LLM_REASON)
    try:
        with gemini(api_key) as generate:
            digest, output = curate(
                filtered.candidates,
                generate=generate,
                model=model,
                selection=feeds_file.selection,
                now=now,
                sleep=sleep,
            )
    except CurationFailed as exc:
        return fallback(exc.reason)
    return digest, output


def daily(
    args: argparse.Namespace,
    *,
    environ: Mapping[str, str],
    client: httpx.Client,
    gemini: GeminiFactory,
    now: datetime,
    sleep: Callable[[float], None],
) -> int:
    """Fetch, filter, select and post (or print with ``--dry-run``)."""
    settings = load_settings(environ)
    api_key = None if args.no_llm else require_gemini_key(settings)
    state = load_state(args.state)
    if posted_today(state, now) and not args.force:
        logger.info("already posted today (%s JST); nothing to do", state.last_posted_date)
        return 0
    feeds_file = load_feeds(FEEDS_PATH)

    results = fetch_all(client, feeds_file.feeds, feeds_file.selection, now)
    filtered = filter_candidates(
        [a for r in results for a in r.articles],
        now=now,
        last_posted_at=state.last_posted_at,
        posted_urls=state.posted_urls.keys(),
        selection=feeds_file.selection,
        filters=feeds_file.filters,
    )
    digest, llm_output = select(
        args,
        filtered,
        feeds_file,
        model=settings.gemini_model,
        api_key=api_key,
        gemini=gemini,
        now=now,
        sleep=sleep,
    )
    payload = build_payload(digest, now)

    if args.dry_run:
        report = build_report(digest, len(filtered.candidates), results, llm_output)
        print(json.dumps({"payloads": [payload], "report": report}, ensure_ascii=False, indent=2))
        return 0

    try:
        post_payload(client, settings.discord_webhook_url, payload, sleep=sleep)
    except Exception as exc:
        logger.error("discord_post failed: %s", describe_error(exc))
        return 1
    logger.info("discord_post ok: japan=%d world=%d", len(digest.japan), len(digest.world))

    try:
        save_state(args.state, record_post(state, digest, now))
    except Exception as exc:
        # Posted but not recorded: tomorrow may post the same items again, so fail loudly.
        logger.error("state save failed after posting: %s", describe_error(exc))
        return 1
    logger.info("state saved: %s", args.state)
    return 0


def run(
    args: argparse.Namespace,
    *,
    environ: Mapping[str, str],
    transport: httpx.BaseTransport | None,
    gemini: GeminiFactory,
    now: Callable[[], datetime],
    sleep: Callable[[float], None],
) -> int:
    with make_client(transport) as client:
        if args.check_feeds:
            return check_feeds(load_feeds(FEEDS_PATH), client, now())
        return daily(args, environ=environ, client=client, gemini=gemini, now=now(), sleep=sleep)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def main(
    argv: list[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    transport: httpx.BaseTransport | None = None,
    gemini: GeminiFactory = gemini_generator,
    now: Callable[[], datetime] = _utc_now,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Run the CLI; any unexpected exception is logged secret-free and becomes exit 1.

    The keyword arguments inject external I/O for tests.
    """
    args = build_parser().parse_args(argv)
    setup_logging()
    try:
        return run(
            args,
            environ=os.environ if environ is None else environ,
            transport=transport,
            gemini=gemini,
            now=now,
            sleep=sleep,
        )
    except Exception as exc:
        logger.error("unexpected failure: %s", describe_error(exc))
        return 1
