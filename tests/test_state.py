import json
import os
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from support import NOW, WEBHOOK_URL

from daily_news.models import Digest, DigestItem
from daily_news.state import (
    POSTED_URL_RETENTION_DAYS,
    State,
    jst_date,
    load_state,
    posted_today,
    record_post,
    save_state,
)

TODAY = date(2026, 10, 9)  # NOW (00:00 UTC) is 09:00 JST


def digest(*urls: str, related: tuple[str, ...] = ()) -> Digest:
    items = tuple(
        DigestItem(title="t", url=url, source="s", related_urls=related if i == 0 else ())
        for i, url in enumerate(urls)
    )
    return Digest(mode="fallback", fallback_reason="r", japan=items)


def test_missing_file_gives_empty_state(state_path: Path) -> None:
    assert load_state(state_path) == State()


@pytest.mark.parametrize(
    "text",
    [
        "{not json",
        "",
        '{"version": 2}',
        '{"last_posted_date": "yesterday"}',
        '{"last_posted_at": "2026-10-09T06:00:00"}',  # naive datetime
        "[]",
    ],
)
def test_broken_file_gives_empty_state_with_error_log(
    state_path: Path, text: str, caplog: pytest.LogCaptureFixture
) -> None:
    state_path.write_text(text, encoding="utf-8")

    assert load_state(state_path) == State()
    assert any(r.levelname == "ERROR" for r in caplog.records)
    if text:
        assert text not in caplog.text  # the file contents are not logged


def test_unknown_keys_are_ignored(state_path: Path) -> None:
    state_path.write_text(
        '{"version": 1, "last_posted_date": "2026-10-08", "recent_headlines": []}',
        encoding="utf-8",
    )

    assert load_state(state_path).last_posted_date == date(2026, 10, 8)


def test_round_trip_and_file_format(state_path: Path) -> None:
    state = State(
        last_posted_date=TODAY,
        last_posted_at=NOW,
        posted_urls={"https://b.example/2": TODAY, "https://a.example/1": TODAY},
    )

    save_state(state_path, state)

    assert load_state(state_path) == state
    data = json.loads(state_path.read_text(encoding="utf-8"))
    assert data == {
        "version": 1,
        "last_posted_date": "2026-10-09",
        "last_posted_at": "2026-10-09T00:00:00Z",
        "posted_urls": {"https://a.example/1": "2026-10-09", "https://b.example/2": "2026-10-09"},
    }
    assert list(data["posted_urls"]) == sorted(data["posted_urls"])  # stable diffs


def test_failed_write_keeps_the_old_file_and_leaves_no_temp(
    state_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_state(state_path, State(last_posted_date=date(2026, 10, 8)))
    before = state_path.read_bytes()

    def broken_replace(src: object, dst: object) -> None:
        raise OSError("rename failed")

    monkeypatch.setattr(os, "replace", broken_replace)

    with pytest.raises(OSError):
        save_state(state_path, State(last_posted_date=TODAY))

    assert state_path.read_bytes() == before
    assert [p.name for p in state_path.parent.iterdir()] == [state_path.name]


def test_record_post_sets_date_time_and_urls() -> None:
    later = NOW + timedelta(hours=3, minutes=7)

    state = record_post(
        State(), digest("https://a.example/1", related=("https://r.example/9",)), later
    )

    assert state.last_posted_date == TODAY
    assert state.last_posted_at == later
    assert state.last_posted_at is not None
    assert state.last_posted_at.utcoffset() == timedelta(0)  # stored in UTC
    assert state.posted_urls == {"https://a.example/1": TODAY, "https://r.example/9": TODAY}


def test_record_post_prunes_urls_older_than_14_days() -> None:
    keep = TODAY - timedelta(days=POSTED_URL_RETENTION_DAYS)
    drop = keep - timedelta(days=1)
    old = State(posted_urls={"https://keep.example": keep, "https://drop.example": drop})

    state = record_post(old, digest("https://new.example"), NOW)

    assert state.posted_urls == {"https://keep.example": keep, "https://new.example": TODAY}


def test_record_post_refreshes_the_date_of_a_url_posted_again() -> None:
    old = State(posted_urls={"https://a.example/1": TODAY - timedelta(days=3)})

    state = record_post(old, digest("https://a.example/1"), NOW)

    assert state.posted_urls == {"https://a.example/1": TODAY}


@pytest.mark.parametrize(
    ("moment", "expected"),
    [
        (datetime(2026, 10, 8, 21, 7, tzinfo=UTC), date(2026, 10, 9)),  # 06:07 JST
        (datetime(2026, 10, 8, 14, 59, tzinfo=UTC), date(2026, 10, 8)),  # 23:59 JST
        (datetime(2026, 10, 8, 15, 0, tzinfo=UTC), date(2026, 10, 9)),  # 00:00 JST
        (datetime(2026, 10, 9, 23, 59, tzinfo=UTC), date(2026, 10, 10)),  # 08:59 JST
    ],
)
def test_jst_date(moment: datetime, expected: date) -> None:
    assert jst_date(moment) == expected


def test_posted_today_compares_jst_dates() -> None:
    state = State(last_posted_date=date(2026, 10, 9))

    assert posted_today(state, datetime(2026, 10, 8, 21, 7, tzinfo=UTC))  # 10-09 06:07 JST
    assert posted_today(state, datetime(2026, 10, 9, 14, 59, tzinfo=UTC))  # 10-09 23:59 JST
    assert not posted_today(state, datetime(2026, 10, 9, 23, 59, tzinfo=UTC))  # 10-10 08:59 JST
    assert not posted_today(State(), NOW)


def test_state_file_contains_no_secrets(state_path: Path) -> None:
    save_state(state_path, record_post(State(), digest("https://a.example/1"), NOW))

    text = state_path.read_text(encoding="utf-8")
    assert WEBHOOK_URL not in text
    assert "LEAK-SENTINEL" not in text
    assert "api/webhooks" not in text
