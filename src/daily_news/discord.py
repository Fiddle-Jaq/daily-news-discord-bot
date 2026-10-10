"""Discord webhook payload building and posting (SPEC §8).

The webhook URL is a credential: it is revealed only in ``post_payload`` and errors are
logged through ``describe_error`` (SPEC §9.7).
"""

import logging
import re
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from pydantic import SecretStr

from daily_news.errors import describe_error
from daily_news.models import Digest, DigestItem

logger = logging.getLogger(__name__)

JST = ZoneInfo("Asia/Tokyo")
WEEKDAYS = "月火水木金土日"

JAPAN_TITLE = "🇯🇵 日本"
WORLD_TITLE = "🌏 世界"
JAPAN_COLOR = 0xD7263D
WORLD_COLOR = 0x1E88E5
TOP_IMPORTANCE_MARK = "🔴"
NO_NEWS_TEXT = "本日は該当ニュースなし"

# Discord limits (SPEC §3.3).
DESCRIPTION_MAX_CHARS = 4096
EMBEDS_TOTAL_MAX_CHARS = 6000

RETRY_DELAY_SECONDS = 5.0
MAX_RETRY_AFTER_SECONDS = 60.0

_MARKDOWN_SPECIAL = re.compile(r"([\\`*_~|>\[\]()])")

Payload = dict[str, Any]


def escape_markdown(text: str) -> str:
    return _MARKDOWN_SPECIAL.sub(r"\\\1", text)


def escape_url(url: str) -> str:
    """Keep a ``)`` in the URL from closing the Markdown link."""
    return url.replace(")", "%29")


def format_header(now: datetime) -> str:
    today = now.astimezone(JST)
    weekday = WEEKDAYS[today.weekday()]
    return f"📰 **{today.year}年{today.month}月{today.day}日({weekday}) 朝のニュース**"


def format_item(item: DigestItem) -> str:
    link = f"**[{escape_markdown(item.title)}]({escape_url(item.url)})**"
    if item.importance == 3:
        link = f"{TOP_IMPORTANCE_MARK} {link}"
    source = f"({escape_markdown(item.source)})"
    if item.summary:
        return f"{link}\n{escape_markdown(item.summary)} {source}"
    return f"{link} {source}"


def format_footer(digest: Digest) -> str:
    if digest.mode == "fallback":
        return f"AI選定・要約なし(理由: {digest.fallback_reason})。各社RSSの新着から機械的に抽出"
    return (
        f"要約はAI({digest.model})が生成。"
        "誤りを含む可能性があるため正確な内容は元記事を確認してください"
    )


def _embed(title: str, color: int, lines: Sequence[str], footer: str) -> Payload:
    return {
        "title": title,
        "description": "\n\n".join(lines),
        "color": color,
        "footer": {"text": footer},
    }


def _embed_chars(embed: Payload) -> int:
    return len(embed["title"]) + len(embed["description"]) + len(embed["footer"]["text"])


def build_payload(digest: Digest, now: datetime) -> Payload:
    """One message with a Japan and a World embed; empty regions are left out.

    If an embed exceeds Discord's limits, its last (least important) items are dropped.
    """
    footer = format_footer(digest)
    sections = [
        (JAPAN_TITLE, JAPAN_COLOR, [format_item(i) for i in digest.japan]),
        (WORLD_TITLE, WORLD_COLOR, [format_item(i) for i in digest.world]),
    ]
    sections = [s for s in sections if s[2]]

    while True:
        embeds = [_embed(title, color, lines, footer) for title, color, lines in sections]
        over = [i for i, e in enumerate(embeds) if len(e["description"]) > DESCRIPTION_MAX_CHARS]
        if not over and sum(_embed_chars(e) for e in embeds) > EMBEDS_TOTAL_MAX_CHARS:
            over = [max(range(len(embeds)), key=lambda i: _embed_chars(embeds[i]))]
        if not over:
            break
        title, _, lines = sections[over[0]]
        lines.pop()
        logger.warning("discord payload over the length limit; dropped the last item of %s", title)
        sections = [s for s in sections if s[2]]

    content = format_header(now)
    if not embeds:
        logger.warning("no news items to post")
        content = f"{content}\n{NO_NEWS_TEXT}"
    return {"content": content, "embeds": embeds, "allowed_mentions": {"parse": []}}


def _retry_after(response: httpx.Response) -> float:
    try:
        delay = float(response.json()["retry_after"])
    except (ValueError, KeyError, TypeError):
        delay = RETRY_DELAY_SECONDS
    return min(max(delay, 0.0), MAX_RETRY_AFTER_SECONDS)


def post_payload(
    client: httpx.Client,
    webhook_url: SecretStr,
    payload: Payload,
    *,
    sleep: Callable[[float], None],
) -> None:
    """POST with ``?wait=true``; retry once on 429 (after ``retry_after``), 5xx or network errors.

    Raises ``httpx.HTTPError`` when the message could not be created.
    """
    url = webhook_url.get_secret_value()  # the only place the webhook URL is revealed
    for attempt in (1, 2):
        try:
            response = client.post(url, params={"wait": "true"}, json=payload)
        except httpx.TransportError as exc:
            if attempt == 2:
                raise
            delay = RETRY_DELAY_SECONDS
            reason = describe_error(exc)
        else:
            if attempt == 2 or not (response.status_code == 429 or response.is_server_error):
                response.raise_for_status()
                return
            delay = _retry_after(response) if response.status_code == 429 else RETRY_DELAY_SECONDS
            reason = f"status={response.status_code}"
        logger.warning("discord_post retrying in %.1fs: %s", delay, reason)
        sleep(delay)
