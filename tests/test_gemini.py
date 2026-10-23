import asyncio
import json
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from google.genai import errors
from tenacity import wait_none

from ttmon.ai.base import NO_CHAT, ChatLine, ClassifyInput, VoiceInput, describe, describe_voice
from ttmon.ai.factory import AIConfigError, build_classifier, load_prompt
from ttmon.ai.gemini import AIUnavailable, GeminiClassifier, quota_exhausted, retry_delay_seconds
from ttmon.ai.schema import InvalidAnswer, parse_answer, response_schema
from ttmon.logging import REDACTED, redact
from ttmon.settings import AppConfig, Settings, load_config

CFG = load_config()
CATEGORIES = CFG.categories

VALID = {
    "is_selling": True,
    "confidence": 0.93,
    "category": "Одежда женская",
    "products": ["куртка"],
    "prices": [
        {"item": "Куртка", "raw": "3.5к", "amount": 35, "currency": "KZT", "source": "screen"}
    ],
    "reason": "Показывает куртки с ценой на экране.",
}
INPUT = ClassifyInput(
    image_jpeg=b"\xff\xd8jpeg",
    username="shop.almaty.test",
    nickname="Женская одежда Алматы",
    title="",
    viewers=120,
)


def _response(payload: Any) -> SimpleNamespace:
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    usage = SimpleNamespace(
        prompt_token_count=1200, candidates_token_count=80, thoughts_token_count=None
    )
    return SimpleNamespace(text=text, usage_metadata=usage)


class FakeModels:
    def __init__(self, outcomes: list[Any]) -> None:
        self._outcomes: Iterator[Any] = iter(outcomes)
        self.calls: list[dict[str, Any]] = []
        self.clients_for: list[str] = []

    async def generate_content(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        outcome = next(self._outcomes)
        if isinstance(outcome, BaseException):
            raise outcome
        return _response(outcome)


def _classifier(
    outcomes: list[Any], keys: tuple[str, ...] = ("key",)
) -> tuple[GeminiClassifier, FakeModels]:
    models = FakeModels(outcomes)

    def client_factory(key: str) -> SimpleNamespace:
        models.clients_for.append(key)
        return SimpleNamespace(aio=SimpleNamespace(models=models))

    classifier = GeminiClassifier(
        keys,
        "gemini-test",
        CFG.ai,
        CATEGORIES,
        "prompt",
        "voice prompt",
        client_factory=client_factory,
        retry_wait=wait_none(),
    )
    return classifier, models


def _api_error(code: int) -> errors.APIError:
    return errors.APIError(code, {"error": {"code": code, "message": "x", "status": "S"}})


async def test_valid_answer_with_price_normalization() -> None:
    classifier, models = _classifier([VALID])

    result = await classifier.classify(INPUT)

    assert result.classification is not None
    assert result.classification.prices[0].amount == 3500
    assert result.model == "gemini-test"
    assert result.usage.prompt_tokens == 1200
    config = models.calls[0]["config"]
    assert config.response_json_schema["properties"]["category"]["enum"] == CATEGORIES
    assert config.temperature == 0


async def test_invalid_answer_retried_once_then_none() -> None:
    bad = {**VALID, "category": "Космос"}
    classifier, models = _classifier([bad, "not json"])

    result = await classifier.classify(INPUT)

    assert result.classification is None
    assert result.raw == "not json"
    assert result.error is not None
    assert len(models.calls) == CFG.ai.invalid_answer_retries + 1


async def test_invalid_then_valid() -> None:
    classifier, _ = _classifier(["{}", VALID])
    assert (await classifier.classify(INPUT)).classification is not None


async def test_rate_limit_retried() -> None:
    classifier, models = _classifier([_api_error(429), _api_error(503), VALID])
    assert (await classifier.classify(INPUT)).classification is not None
    assert len(models.calls) == 3


@pytest.mark.parametrize("code", [400, 401, 403, 404])
async def test_bad_key_or_model_is_fatal(code: int) -> None:
    classifier, models = _classifier([_api_error(code)])
    with pytest.raises(AIUnavailable):
        await classifier.classify(INPUT)
    assert len(models.calls) == 1


def test_parse_answer_rejects_unknown_category() -> None:
    with pytest.raises(InvalidAnswer, match="категория"):
        parse_answer(json.dumps({**VALID, "category": "Космос"}), CATEGORIES)


def test_schema_requires_all_fields() -> None:
    schema = response_schema(CATEGORIES)
    assert set(schema["required"]) == set(VALID)


def test_describe_chat() -> None:
    assert NO_CHAT in describe(INPUT)
    with_chat = ClassifyInput(**{**INPUT.__dict__, "chat": [ChatLine("aru", "қаншадан?")]})
    assert "- aru: қаншадан?" in describe(with_chat)


def test_prompt_lists_categories() -> None:
    prompt = load_prompt(CFG)
    assert "{categories}" not in prompt
    assert all(f"- {c}" in prompt for c in CATEGORIES)


def _settings(**env: str) -> Settings:
    return Settings(_env_file=None, **env)  # type: ignore[arg-type]


def test_reserve_keys_parsed_in_order_without_duplicates() -> None:
    settings = _settings(
        gemini_api_key="primary-key-1", gemini_api_keys_reserve=" key-2 ,,primary-key-1, key-3 "
    )
    assert settings.gemini_api_keys() == ["primary-key-1", "key-2", "key-3"]


def test_reserve_keys_without_primary_are_ignored() -> None:
    assert _settings(gemini_api_keys_reserve="key-2,key-3").gemini_api_keys() == []


def test_factory_registers_all_keys_as_secrets() -> None:
    settings = _settings(
        gemini_api_key="primary-secret-1", gemini_api_keys_reserve="reserve-secret-2"
    )
    build_classifier(CFG, settings)
    assert redact("a primary-secret-1 b reserve-secret-2") == f"a {REDACTED} b {REDACTED}"


def test_missing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(AIConfigError, match="GEMINI_API_KEY"):
        build_classifier(CFG, _settings())


def test_claude_not_implemented() -> None:
    cfg: AppConfig = CFG.model_copy(update={"ai": CFG.ai.model_copy(update={"provider": "claude"})})
    with pytest.raises(AIConfigError, match="claude"):
        build_classifier(cfg, _settings())


DAILY = "GenerateRequestsPerDayPerProjectPerModel-FreeTier"


def _quota_error(quota_id: str, delay: str) -> errors.APIError:
    return errors.APIError(
        429,
        {
            "error": {
                "code": 429,
                "message": "You exceeded your current quota",
                "status": "RESOURCE_EXHAUSTED",
                "details": [
                    {
                        "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                        "violations": [{"quotaId": quota_id, "quotaValue": "20"}],
                    },
                    {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": delay},
                ],
            }
        },
    )


async def test_daily_quota_is_fatal_without_retries() -> None:
    error = _quota_error(DAILY, "12480s")
    classifier, models = _classifier([error])
    with pytest.raises(AIUnavailable, match="квота"):
        await classifier.classify(INPUT)
    assert len(models.calls) == 1


async def test_per_minute_quota_retried_with_server_delay() -> None:
    error = _quota_error("GenerateRequestsPerMinutePerProjectPerModel-FreeTier", "0s")
    classifier, models = _classifier([error, VALID])
    assert (await classifier.classify(INPUT)).classification is not None
    assert len(models.calls) == 2


def test_long_retry_delay_counts_as_exhausted() -> None:
    error = _quota_error("SomethingPerMinute", "3600s")
    assert quota_exhausted(error, max_wait_s=30) is not None
    assert retry_delay_seconds(error) == 3600


async def test_daily_quota_switches_to_next_key() -> None:
    classifier, models = _classifier([_quota_error(DAILY, "12480s"), VALID], keys=("k1", "k2"))

    assert (await classifier.classify(INPUT)).classification is not None
    assert models.clients_for == ["k1", "k2"]
    assert len(models.calls) == 2


def _invalid_key_error() -> errors.APIError:
    details = [{"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": "API_KEY_INVALID"}]
    return errors.APIError(
        400, {"error": {"code": 400, "status": "INVALID_ARGUMENT", "details": details}}
    )


@pytest.mark.parametrize("error", [_api_error(401), _api_error(403), _invalid_key_error()])
async def test_rejected_key_skipped(error: errors.APIError) -> None:
    classifier, models = _classifier([error, VALID], keys=("k1", "k2"))
    assert (await classifier.classify(INPUT)).classification is not None
    assert models.clients_for == ["k1", "k2"]


async def test_bad_request_fatal_without_trying_reserve_keys() -> None:
    classifier, models = _classifier([_api_error(400)], keys=("k1", "k2"))
    with pytest.raises(AIUnavailable, match="400"):
        await classifier.classify(INPUT)
    assert models.clients_for == ["k1"]


async def test_per_minute_quota_waited_on_same_key(monkeypatch: pytest.MonkeyPatch) -> None:
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    error = _quota_error("GenerateRequestsPerMinutePerProjectPerModel-FreeTier", "43s")
    classifier, models = _classifier([error, VALID], keys=("k1", "k2"))

    assert (await classifier.classify(INPUT)).classification is not None
    assert models.clients_for == ["k1"]
    assert slept == [43]


async def test_model_not_found_fatal_despite_reserve_keys() -> None:
    classifier, models = _classifier([_api_error(404)], keys=("k1", "k2"))
    with pytest.raises(AIUnavailable, match="404"):
        await classifier.classify(INPUT)
    assert models.clients_for == ["k1"]


async def test_all_keys_exhausted_stays_on_last_key() -> None:
    quota = _quota_error(DAILY, "12480s")
    classifier, models = _classifier([quota, quota, quota], keys=("k1", "k2"))

    with pytest.raises(AIUnavailable, match="ключей: 2"):
        await classifier.classify(INPUT)
    with pytest.raises(AIUnavailable, match="квота"):
        await classifier.classify(INPUT)

    assert models.clients_for == ["k1", "k2"]
    assert len(models.calls) == 3


VOICE_INPUT = VoiceInput(
    audio_mp3=b"ID3audio",
    username="shop.almaty.test",
    nickname="Женская одежда Алматы",
    title="",
    products=["куртка", "платье"],
)


async def test_listen_returns_voice_prices_normalized() -> None:
    answer = {
        "speech": "Продавец называет цену куртки.",
        "prices": [{"item": "Куртка", "raw": "8.9к", "amount": 89, "currency": "KZT"}],
    }
    classifier, models = _classifier([answer])

    result = await classifier.listen(VOICE_INPUT)

    assert [(p.amount, p.source) for p in result.prices] == [(8900, "voice")]
    assert result.speech == "Продавец называет цену куртки."
    assert result.error is None and result.usage.prompt_tokens == 1200
    call = models.calls[0]
    assert call["config"].response_json_schema["required"] == ["speech", "prices"]
    assert call["config"].system_instruction == "voice prompt"
    audio, text = call["contents"].parts
    assert audio.inline_data.mime_type == "audio/mp3"
    assert "куртка, платье" in text.text


async def test_listen_invalid_answer_gives_error_without_prices() -> None:
    classifier, models = _classifier([{"prices": []}, "not json"])

    result = await classifier.listen(VOICE_INPUT)

    assert result.prices == [] and result.error is not None
    assert len(models.calls) == CFG.ai.invalid_answer_retries + 1


async def test_listen_switches_key_on_daily_quota() -> None:
    classifier, models = _classifier(
        [_quota_error(DAILY, "12480s"), {"speech": "Музыка.", "prices": []}], keys=("k1", "k2")
    )

    result = await classifier.listen(VOICE_INPUT)

    assert result.speech == "Музыка."
    assert models.clients_for == ["k1", "k2"]


def test_describe_voice_lists_products() -> None:
    text = describe_voice(VOICE_INPUT)
    assert text.startswith("<данные_эфира>") and "Товары на экране: куртка, платье" in text
    assert "Товары на экране: —" in describe_voice(
        VoiceInput(audio_mp3=b"", username="u", nickname="n", title="")
    )
