"""state.json: what was posted and when (SPEC §9.1, §9.2).

The file is committed by the bot every morning, so it must never contain secrets.
A missing or broken file is treated as an empty state: missing a day is worse than
the risk of posting twice.
"""

import json
import logging
import os
import tempfile
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import AwareDatetime, BaseModel, ConfigDict, ValidationError

from daily_news.errors import describe_error
from daily_news.models import Digest

logger = logging.getLogger(__name__)

JST = ZoneInfo("Asia/Tokyo")
POSTED_URL_RETENTION_DAYS = 14


class State(BaseModel):
    # Unknown keys are ignored so that one added field never turns into "broken state".
    model_config = ConfigDict(frozen=True, extra="ignore")

    version: Literal[1] = 1
    last_posted_date: date | None = None
    """The JST date of the last successful post."""
    last_posted_at: AwareDatetime | None = None
    """When the last post succeeded, stored in UTC."""
    posted_urls: dict[str, date] = {}
    """Normalized URL -> JST date it was posted (representative and related articles)."""


def jst_date(moment: datetime) -> date:
    return moment.astimezone(JST).date()


def posted_today(state: State, now: datetime) -> bool:
    return state.last_posted_date == jst_date(now)


def load_state(path: Path) -> State:
    """Read the state; a missing, unreadable or invalid file gives an empty state."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        logger.info("state: %s not found; starting with an empty state", path)
        return State()
    except OSError as exc:
        logger.error("state: cannot read %s: %s; using an empty state", path, describe_error(exc))
        return State()
    try:
        return State.model_validate_json(text)
    except ValidationError as exc:
        logger.error(
            "state: %s is broken (%d errors); using an empty state", path, exc.error_count()
        )
        return State()


def save_state(path: Path, state: State) -> None:
    """Write atomically: a temporary file in the same directory, then ``os.replace``."""
    data = state.model_dump(mode="json")
    data["posted_urls"] = dict(sorted(data["posted_urls"].items()))
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def record_post(state: State, digest: Digest, now: datetime) -> State:
    """The state after a successful post: today's date, the time, and the posted URLs.

    Posted URLs older than ``POSTED_URL_RETENTION_DAYS`` are pruned here.
    """
    today = jst_date(now)
    oldest = today - timedelta(days=POSTED_URL_RETENTION_DAYS)
    posted = {url: day for url, day in state.posted_urls.items() if day >= oldest}
    for item in (*digest.japan, *digest.world):
        for url in (item.url, *item.related_urls):
            posted[url] = today
    return state.model_copy(
        update={
            "last_posted_date": today,
            "last_posted_at": now.astimezone(UTC),
            "posted_urls": posted,
        }
    )
