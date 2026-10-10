from pathlib import Path

import pytest
from pydantic import ValidationError

from daily_news.config import load_feeds, load_settings, require_gemini_key
from daily_news.errors import ConfigError

REPO_FEEDS = Path(__file__).parent.parent / "feeds.toml"

VALID = """
[selection]
japan_max = 7
world_max = 4
window_hours = 30
max_window_hours = 48
max_candidates = 120
per_feed_max = 20
stale_feed_hours = 72
description_max_chars = 200

[[feeds]]
name = "NHK 主要"
url = "https://news.web.nhk/n-data/conf/na/rss/cat0.xml"
lang = "ja"
source = "NHK"
"""


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "feeds.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_repository_feeds_toml_loads() -> None:
    feeds_file = load_feeds(REPO_FEEDS)

    assert len(feeds_file.feeds) == 12
    assert feeds_file.selection.stale_feed_hours == 72
    assert "/sports/" in feeds_file.filters.exclude_url_patterns
    fallback = sorted(
        (f.fallback_region, f.fallback_priority, f.name)
        for f in feeds_file.feeds
        if f.fallback_region is not None
    )
    assert fallback == [
        ("japan", 1, "NHK 主要"),
        ("japan", 2, "BBC 日本語"),
        ("world", 1, "BBC World"),
        ("world", 2, "Guardian World"),
    ]


def test_minimal_file_uses_empty_filters(tmp_path: Path) -> None:
    feeds_file = load_feeds(write(tmp_path, VALID))

    assert feeds_file.filters.exclude_url_patterns == ()
    assert feeds_file.feeds[0].fallback_region is None


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ('lang = "ja"', 'lang = "fr"'),
        ('url = "https://news.web.nhk/n-data/conf/na/rss/cat0.xml"', 'url = "not a url"'),
        ("stale_feed_hours = 72", "stale_feed_hours = 0"),
        ('source = "NHK"', 'source = "NHK"\nfallback_region = "japan"'),
        ('source = "NHK"', 'source = "NHK"\nunknown_key = 1'),
    ],
)
def test_invalid_values_are_rejected(tmp_path: Path, old: str, new: str) -> None:
    with pytest.raises(ValidationError):
        load_feeds(write(tmp_path, VALID.replace(old, new)))


def test_duplicate_feed_names_are_rejected(tmp_path: Path) -> None:
    feed = VALID[VALID.index("[[feeds]]") :]

    with pytest.raises(ValidationError):
        load_feeds(write(tmp_path, VALID + feed))


def test_settings_use_defaults_for_empty_values() -> None:
    settings = load_settings(
        {
            "DISCORD_WEBHOOK_URL": " https://discord.example/hook ",
            "GEMINI_API_KEY": "",
            "GEMINI_MODEL": " ",
        }
    )

    assert settings.discord_webhook_url.get_secret_value() == "https://discord.example/hook"
    assert settings.gemini_api_key is None
    assert settings.gemini_model == "gemini-3.5-flash"


def test_settings_read_given_values() -> None:
    settings = load_settings(
        {"DISCORD_WEBHOOK_URL": "https://d.example/h", "GEMINI_API_KEY": "k", "GEMINI_MODEL": "m"}
    )

    assert settings.gemini_api_key is not None
    assert settings.gemini_api_key.get_secret_value() == "k"
    assert settings.gemini_model == "m"


@pytest.mark.parametrize(
    "environ", [{}, {"DISCORD_WEBHOOK_URL": ""}, {"DISCORD_WEBHOOK_URL": "  "}]
)
def test_missing_webhook_url_is_an_error(environ: dict[str, str]) -> None:
    with pytest.raises(ConfigError, match="DISCORD_WEBHOOK_URL"):
        load_settings(environ)


def test_gemini_key_is_required_only_when_asked_for() -> None:
    settings = load_settings({"DISCORD_WEBHOOK_URL": "https://d.example/h", "GEMINI_API_KEY": " "})

    with pytest.raises(ConfigError, match="GEMINI_API_KEY"):
        require_gemini_key(settings)


def test_gemini_key_is_returned_as_secret() -> None:
    settings = load_settings({"DISCORD_WEBHOOK_URL": "https://d.example/h", "GEMINI_API_KEY": "k"})

    assert require_gemini_key(settings).get_secret_value() == "k"
