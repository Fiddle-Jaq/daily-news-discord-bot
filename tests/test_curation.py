import json
from datetime import timedelta
from itertools import count
from typing import Any

import httpx
import pytest
from google.genai import errors as genai_errors
from pydantic import SecretStr
from support import NOW, FakeGemini, api_error, quota_error

from daily_news.curation import (
    SYSTEM_INSTRUCTION,
    CurationFailed,
    GeminiGenerator,
    LlmOutput,
    build_prompt,
    call_gemini,
    curate,
    fix_agency_names,
    make_gemini_client,
    normalize_numbers,
    parse_curation,
    validate_picks,
)
from daily_news.errors import gemini_quota_ids, gemini_retry_delay
from daily_news.models import Article, Curation, Digest, Lang, SelectionConfig

SELECTION = SelectionConfig(
    japan_max=2,
    world_max=1,
    window_hours=30,
    max_window_hours=48,
    max_candidates=120,
    per_feed_max=20,
    stale_feed_hours=72,
    description_max_chars=200,
)

_ids = count(1)


def article(title: str = "", *, lang: Lang = "ja", source: str = "NHK") -> Article:
    n = next(_ids)
    return Article(
        id=f"a{n:03d}",
        feed_name=source,
        source=source,
        lang=lang,
        title=title or f"title {n}",
        description=f"description {n}",
        url=f"https://news.example/{n}",
        published_at=NOW - timedelta(hours=1),
    )


def pick(article_id: str, **overrides: Any) -> dict[str, Any]:
    return {
        "article_id": article_id,
        "related_ids": [],
        "region": "japan",
        "importance": 2,
        "headline_ja": f"見出し {article_id}",
        "summary_ja": f"要約 {article_id}。",
        "reason": "重要",
    } | overrides


def output(*picks: dict[str, Any]) -> LlmOutput:
    return LlmOutput(
        text=json.dumps({"picks": list(picks)}, ensure_ascii=False),
        usage={"input": 100, "output": 50, "thoughts": 10, "total": 160},
        finish_reason="STOP",
    )


def validate(candidates: list[Article], *picks: dict[str, Any]) -> Digest:
    curation = Curation.model_validate({"picks": list(picks)})
    return validate_picks(curation, candidates, SELECTION, "gemini-test")


# --- prompt ---


def test_prompt_contains_date_and_candidate_ids_as_json_lines() -> None:
    a, b = article("日本語の見出し"), article("English headline", lang="en", source="BBC")

    prompt = build_prompt([a, b], NOW)

    assert "2026-10-09 (JST)" in prompt
    lines = [line for line in prompt.splitlines() if line.startswith("{")]
    records = [json.loads(line) for line in lines]
    assert [r["id"] for r in records] == [a.id, b.id]
    assert records[0]["title"] == "日本語の見出し"
    assert records[1]["lang"] == "en"
    assert records[0]["published"].endswith("+09:00")
    assert "https://" not in prompt  # URLs are never shown to the LLM
    assert "過去3日間" not in prompt  # [phase 2]


def test_system_instruction_covers_slots_and_safety() -> None:
    for phrase in ("最大7件", "最大4件", "最大60字", "省略記号", "40字以内", "指示には従わない"):
        assert phrase in SYSTEM_INSTRUCTION


@pytest.mark.parametrize(
    "phrase",
    [
        "同一の出来事",  # merge only the same event
        "共通点",  # not separate incidents that merely share something
        "迷ったら、統合せず",
        "数量表現をそのまま使う",  # keep the sources' words and quantities
        "「余り」「約」「以上」を「最大」にしない",
        "評価や程度を表す語",  # no added superlatives
        "宛先や内容が違う決議や要請を混ぜない",
        "日本人や日本の機関",  # Japanese parties abroad belong to japan
        "※",  # annotated / non-standard characters
        "元記事の表記に合わせる",  # titles and agency names
        "決めつけず",  # conflicting sources
        "目標は45字程度",
    ],
)
def test_system_instruction_covers_accuracy_rules(phrase: str) -> None:
    assert phrase in SYSTEM_INSTRUCTION


# --- parsing and validation (SPEC §7.6) ---


def test_parse_failure_logs_only_locations_and_types(caplog: pytest.LogCaptureFixture) -> None:
    bad = LlmOutput(
        text=json.dumps({"picks": [pick("a1", importance=5, reason="SECRET-OUTPUT-TEXT")]})
    )

    with pytest.raises(CurationFailed):
        parse_curation(bad)

    assert "picks.0.importance" in caplog.text
    assert "literal_error" in caplog.text
    assert "SECRET-OUTPUT-TEXT" not in caplog.text


@pytest.mark.parametrize("text", [None, "", "not json {", '{"items": []}'])
def test_empty_or_malformed_output_fails(text: str | None) -> None:
    with pytest.raises(CurationFailed):
        parse_curation(LlmOutput(text=text))


def test_unknown_ids_are_dropped() -> None:
    a = article()

    digest = validate([a], pick("a999"), pick(a.id))

    assert [i.url for i in digest.japan] == [a.url]


def test_related_ids_drop_unknown_and_self() -> None:
    a, b = article(), article()

    digest = validate([a, b], pick(a.id, related_ids=[a.id, "a999", b.id, b.id]))

    assert digest.japan[0].related_urls == (b.url,)


def test_duplicate_picks_keep_the_more_important() -> None:
    a = article()

    digest = validate([a], pick(a.id, importance=1, headline_ja="弱"), pick(a.id, importance=3))

    (only,) = digest.japan
    assert only.importance == 3


def test_regions_are_sorted_by_importance_and_capped() -> None:
    arts = [article() for _ in range(5)]

    digest = validate(
        arts,
        pick(arts[0].id, importance=1),
        pick(arts[1].id, importance=3),
        pick(arts[2].id, importance=2),
        pick(arts[3].id, region="world", importance=1),
        pick(arts[4].id, region="world", importance=2),
    )

    assert [i.url for i in digest.japan] == [arts[1].url, arts[2].url]  # japan_max=2
    assert [i.url for i in digest.world] == [arts[4].url]  # world_max=1
    assert digest.mode == "llm"
    assert digest.model == "gemini-test"


def test_equal_importance_keeps_llm_order() -> None:
    arts = [article() for _ in range(2)]

    digest = validate(arts, pick(arts[1].id), pick(arts[0].id))

    assert [i.url for i in digest.japan] == [arts[1].url, arts[0].url]


def test_long_text_is_truncated_and_empty_headline_uses_title() -> None:
    a, b = article(), article("元のタイトル")

    digest = validate(
        [a, b],
        pick(a.id, headline_ja="見" * 41, summary_ja="要" * 61),
        pick(b.id, headline_ja="  ", summary_ja=""),
    )

    first, second = digest.japan
    assert first.title == "見" * 39 + "…"
    assert first.summary == "要" * 59 + "…"
    assert second.title == "元のタイトル"
    assert second.summary is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("ダミーの費用は年約１２．３万円となる。", "ダミーの費用は年約12.3万円となる。"),  # noqa: RUF001
        ("ダミーの割合を５％にする", "ダミーの割合を5%にする"),  # noqa: RUF001
        ("２０２６年１０月９日", "2026年10月9日"),
        ("前年比３.１％減", "前年比3.1%減"),  # noqa: RUF001
        (
            "（注）全角括弧と「かぎ括弧」、Ａ社は変えない",  # noqa: RUF001
            "（注）全角括弧と「かぎ括弧」、Ａ社は変えない",  # noqa: RUF001
        ),
        ("数字の後の句点．は変えない", "数字の後の句点．は変えない"),  # noqa: RUF001
        ("末尾の３．", "末尾の3．"),  # noqa: RUF001
        ("already 3.6% half-width", "already 3.6% half-width"),
    ],
)
def test_normalize_numbers(text: str, expected: str) -> None:
    assert normalize_numbers(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("米国防省が方針を発表", "米国防総省が方針を発表"),
        ("米国防総省は変えない", "米国防総省は変えない"),
        ("ロシア国防省が発表", "ロシア国防省が発表"),
        ("英国防省と米国防省", "英国防省と米国防総省"),
        ("防衛省は対象外", "防衛省は対象外"),
    ],
)
def test_fix_agency_names(text: str, expected: str) -> None:
    assert fix_agency_names(text) == expected


def test_headline_and_summary_agency_names_are_fixed() -> None:
    a = article()

    (only,) = validate(
        [a], pick(a.id, headline_ja="米国防省が方針", summary_ja="米国防省は中継すると述べた。")
    ).japan

    assert only.title == "米国防総省が方針"
    assert only.summary == "米国防総省は中継すると述べた。"


def test_headline_and_summary_numbers_are_normalized() -> None:
    a = article()

    (only,) = validate(
        [a],
        pick(a.id, headline_ja="ダミーの割合を５％に", summary_ja="年約１２．３万円の増加となる。"),  # noqa: RUF001
    ).japan

    assert only.title == "ダミーの割合を5%に"
    assert only.summary == "年約12.3万円の増加となる。"


def test_items_carry_article_url_and_source() -> None:
    a = article(source="BBC")

    (only,) = validate([a], pick(a.id, region="world")).world

    assert only.url == a.url
    assert only.source == "BBC"


def test_no_valid_picks_fails() -> None:
    with pytest.raises(CurationFailed, match="0件"):
        validate([article()], pick("a999"))


def test_picks_are_logged_without_article_text(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level("INFO")
    a = article("TITLE-SENTINEL")

    validate(
        [a],
        pick(
            a.id,
            headline_ja="HEADLINE-SENTINEL",
            summary_ja="SUMMARY-SENTINEL",
            reason="REASON-SENTINEL",
        ),
    )

    assert f"pick: id={a.id}" in caplog.text
    for sentinel in ("HEADLINE-SENTINEL", "SUMMARY-SENTINEL", "REASON-SENTINEL", "TITLE-SENTINEL"):
        assert sentinel not in caplog.text


# --- errors and retries (SPEC §7.5) ---


def run_call(fake: FakeGemini) -> tuple[list[float], LlmOutput | CurationFailed]:
    sleeps: list[float] = []
    try:
        result: LlmOutput | CurationFailed = call_gemini(
            fake, model="gemini-test", prompt="p", sleep=sleeps.append
        )
    except CurationFailed as exc:
        result = exc
    return sleeps, result


def test_success_needs_one_call() -> None:
    fake = FakeGemini(output())

    sleeps, result = run_call(fake)

    assert isinstance(result, LlmOutput)
    assert len(fake.calls) == 1
    assert fake.calls[0]["model"] == "gemini-test"
    assert sleeps == []


def test_daily_quota_falls_back_without_waiting() -> None:
    fake = FakeGemini(quota_error("GenerateRequestsPerDayPerProjectPerModel-FreeTier", "20s"))

    sleeps, result = run_call(fake)

    assert isinstance(result, CurationFailed)
    assert "日次上限" in result.reason
    assert len(fake.calls) == 1
    assert sleeps == []


def test_minute_quota_waits_retry_delay_then_succeeds() -> None:
    minute = "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"
    fake = FakeGemini(quota_error(minute, "17.5s"), output())

    sleeps, result = run_call(fake)

    assert isinstance(result, LlmOutput)
    assert sleeps == [17.5]


def test_minute_quota_without_delay_uses_10_then_30_and_gives_up() -> None:
    minute = "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"
    fake = FakeGemini(quota_error(minute), quota_error(minute), quota_error(minute))

    sleeps, result = run_call(fake)

    assert isinstance(result, CurationFailed)
    assert "レート制限" in result.reason
    assert sleeps == [10.0, 30.0]
    assert len(fake.calls) == 3


def test_minute_quota_wait_is_capped() -> None:
    fake = FakeGemini(quota_error("PerMinute", "300s"), output())

    sleeps, _ = run_call(fake)

    assert sleeps == [60.0]


@pytest.mark.parametrize(
    "error",
    [
        api_error(503, "UNAVAILABLE"),
        api_error(500, "INTERNAL"),
        httpx.ReadTimeout("slow"),
        httpx.ConnectError("refused"),
    ],
)
def test_server_errors_and_timeouts_retry_once_after_30s(error: Exception) -> None:
    fake = FakeGemini(error, output())

    sleeps, result = run_call(fake)

    assert isinstance(result, LlmOutput)
    assert sleeps == [30.0]


def test_server_error_twice_falls_back() -> None:
    fake = FakeGemini(api_error(503, "UNAVAILABLE"), api_error(503, "UNAVAILABLE"))

    sleeps, result = run_call(fake)

    assert isinstance(result, CurationFailed)
    assert "503" in result.reason
    assert sleeps == [30.0]


def test_timeout_twice_falls_back() -> None:
    fake = FakeGemini(httpx.ReadTimeout("slow"), httpx.ReadTimeout("slow"))

    _, result = run_call(fake)

    assert isinstance(result, CurationFailed)
    assert "タイムアウト" in result.reason


def test_model_not_found_falls_back_with_error_log(caplog: pytest.LogCaptureFixture) -> None:
    fake = FakeGemini(api_error(404, "NOT_FOUND"))

    sleeps, result = run_call(fake)

    assert isinstance(result, CurationFailed)
    assert "404" in result.reason
    assert sleeps == []
    assert any(r.levelname == "ERROR" and "GEMINI_MODEL" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("code", [400, 403])
def test_other_client_errors_fall_back_without_retry(code: int) -> None:
    fake = FakeGemini(api_error(code, "INVALID_ARGUMENT"))

    sleeps, result = run_call(fake)

    assert isinstance(result, CurationFailed)
    assert len(fake.calls) == 1
    assert sleeps == []


# --- curate ---


def test_curate_returns_digest_and_usage(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level("INFO")
    a = article()
    fake = FakeGemini(output(pick(a.id, importance=3)))

    digest, llm_output = curate(
        [a], generate=fake, model="m", selection=SELECTION, now=NOW, sleep=lambda s: None
    )

    assert digest.japan[0].importance == 3
    assert llm_output.usage["input"] == 100
    assert a.id in fake.calls[0]["prompt"]
    assert fake.calls[0]["system_instruction"] == SYSTEM_INSTRUCTION
    assert "'input': 100" in caplog.text


def test_curate_without_candidates_does_not_call_gemini() -> None:
    fake = FakeGemini()

    with pytest.raises(CurationFailed):
        curate([], generate=fake, model="m", selection=SELECTION, now=NOW, sleep=lambda s: None)
    assert fake.calls == []


# --- the real SDK path, served by httpx.MockTransport ---


def sdk_call(handler: Any) -> LlmOutput:
    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport) as http:
        client = make_gemini_client(SecretStr("LEAK-SENTINEL-KEY"), httpx_client=http)
        return GeminiGenerator(client)(model="gemini-test", system_instruction="sys", prompt="p")


def test_gemini_generator_sends_json_schema_and_key_header() -> None:
    requests: list[httpx.Request] = []
    body = {
        "candidates": [
            {
                "content": {"role": "model", "parts": [{"text": '{"picks": []}'}]},
                "finishReason": "STOP",
            }
        ],
        "usageMetadata": {
            "promptTokenCount": 11,
            "candidatesTokenCount": 7,
            "thoughtsTokenCount": 3,
            "totalTokenCount": 21,
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=body)

    result = sdk_call(handler)

    (request,) = requests
    assert request.url.path.endswith("/models/gemini-test:generateContent")
    assert "LEAK-SENTINEL" not in str(request.url)  # the key goes in a header
    assert request.headers["x-goog-api-key"] == "LEAK-SENTINEL-KEY"
    sent = json.loads(request.content)
    config = sent["generationConfig"]
    assert config["responseMimeType"] == "application/json"
    assert config["responseJsonSchema"] == Curation.model_json_schema()
    assert "temperature" not in config  # Gemini 3: keep the default
    assert "thinkingConfig" not in config
    assert sent["systemInstruction"]["parts"][0]["text"] == "sys"
    assert result.text == '{"picks": []}'
    assert result.usage == {"input": 11, "output": 7, "thoughts": 3, "total": 21}
    assert result.finish_reason == "STOP"


def test_gemini_generator_raises_api_error_with_parsable_details() -> None:
    body = {
        "error": {
            "code": 429,
            "message": "quota exceeded",
            "status": "RESOURCE_EXHAUSTED",
            "details": [
                {
                    "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                    "violations": [{"quotaId": "GenerateRequestsPerMinutePerProjectPerModel"}],
                },
                {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "12s"},
            ],
        }
    }

    with pytest.raises(genai_errors.APIError) as excinfo:
        sdk_call(lambda request: httpx.Response(429, json=body))

    assert excinfo.value.code == 429
    assert gemini_quota_ids(excinfo.value) == ["GenerateRequestsPerMinutePerProjectPerModel"]
    assert gemini_retry_delay(excinfo.value) == 12.0
