from collections.abc import Callable, Sequence
from typing import Any

import httpx
import structlog
from google import genai
from google.genai import errors, types
from tenacity import (
    AsyncRetrying,
    RetryCallState,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)

from ttmon.ai.base import (
    ClassifyInput,
    ClassifyResult,
    Usage,
    VoiceInput,
    VoiceResult,
    describe,
    describe_voice,
)
from ttmon.ai.prices import normalize
from ttmon.ai.schema import (
    InvalidAnswer,
    parse_answer,
    parse_voice,
    response_schema,
    voice_schema,
)
from ttmon.settings import AIConfig

log = structlog.get_logger(__name__)

RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}
# 400 — отказ ключа только с ErrorInfo.reason API_KEY_*, иначе битый запрос (на всех ключах)
FATAL_STATUS = {400, 401, 403, 404}
KEY_STATUS = {401, 403}


class AIUnavailable(RuntimeError):
    pass


def _error_details(exc: errors.APIError) -> list[dict[str, Any]]:
    payload = exc.details if isinstance(exc.details, dict) else {}
    details = payload.get("error", {}).get("details", [])
    return [d for d in details if isinstance(d, dict)]


def retry_delay_seconds(exc: BaseException) -> float | None:
    """RetryInfo.retryDelay из ответа 429 («12480s»): сколько сервер просит подождать."""
    if not isinstance(exc, errors.APIError):
        return None
    for detail in _error_details(exc):
        if detail.get("@type", "").endswith("RetryInfo"):
            try:
                return float(str(detail.get("retryDelay", "")).rstrip("s"))
            except ValueError:
                return None
    return None


def quota_exhausted(exc: BaseException, max_wait_s: float) -> str | None:
    """Суточная квота (16.10.2026: free tier 3.5-flash — 20 запросов/сутки) или ожидание
    дольше max_wait_s — не временная ошибка: ретраи только жгут время слота."""
    if not isinstance(exc, errors.APIError) or exc.code != 429:
        return None
    for detail in _error_details(exc):
        if detail.get("@type", "").endswith("QuotaFailure"):
            for violation in detail.get("violations", []):
                quota = str(violation.get("quotaId", ""))
                if "PerDay" in quota:
                    return f"{quota} = {violation.get('quotaValue')}"
    delay = retry_delay_seconds(exc)
    if delay is not None and delay > max_wait_s:
        return f"повтор не раньше чем через {delay / 3600:.1f} ч"
    return None


def key_rejected(exc: errors.APIError) -> bool:
    if exc.code in KEY_STATUS:
        return True
    return exc.code == 400 and any(
        detail.get("@type", "").endswith("ErrorInfo")
        and str(detail.get("reason", "")).startswith("API_KEY")
        for detail in _error_details(exc)
    )


def is_retryable(exc: BaseException, max_wait_s: float) -> bool:
    if isinstance(exc, errors.APIError):
        return exc.code in RETRYABLE_STATUS and quota_exhausted(exc, max_wait_s) is None
    return isinstance(exc, httpx.TransportError | TimeoutError)


class GeminiClassifier:
    def __init__(
        self,
        api_keys: Sequence[str],
        model: str,
        cfg: AIConfig,
        categories: list[str],
        system_prompt: str,
        voice_prompt: str = "",
        client_factory: Callable[[str], Any] | None = None,
        retry_wait: Any = None,
    ) -> None:
        if not api_keys:
            raise ValueError("нужен хотя бы один ключ Gemini")
        self._model = model
        self._retry_wait = retry_wait or wait_exponential_jitter(
            initial=cfg.retry_wait_initial_seconds, max=cfg.retry_wait_max_seconds
        )
        self._cfg = cfg
        self._categories = categories
        self._client_factory = client_factory or (
            lambda key: genai.Client(
                api_key=key,
                http_options=types.HttpOptions(timeout=int(cfg.timeout_seconds * 1000)),
            )
        )
        self._api_keys = list(api_keys)
        self._key_index = 0
        self._client = self._client_factory(self._api_keys[0])
        self._config = types.GenerateContentConfig(
            system_instruction=system_prompt,
            temperature=0,
            response_mime_type="application/json",
            response_json_schema=response_schema(categories),
            thinking_config=types.ThinkingConfig(thinking_budget=cfg.thinking_budget),
        )
        self._voice_config = types.GenerateContentConfig(
            system_instruction=voice_prompt,
            temperature=0,
            response_mime_type="application/json",
            response_json_schema=voice_schema(),
            thinking_config=types.ThinkingConfig(thinking_budget=cfg.thinking_budget),
        )

    @property
    def model(self) -> str:
        return self._model

    async def _generate(self, contents: types.Content, config: types.GenerateContentConfig) -> Any:
        max_wait = self._cfg.retry_wait_max_seconds

        def wait(state: RetryCallState) -> float:
            # 429 с RetryInfo: ждём ровно столько, сколько просит сервер (уже ≤ max_wait)
            outcome = state.outcome.exception() if state.outcome else None
            delay = retry_delay_seconds(outcome) if outcome else None
            return delay if delay is not None else float(self._retry_wait(state))

        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(self._cfg.api_retries),
            wait=wait,
            retry=retry_if_exception(lambda exc: is_retryable(exc, max_wait)),
            reraise=True,
        ):
            with attempt:
                state = attempt.retry_state
                if state.attempt_number > 1 and state.outcome is not None:
                    previous = state.outcome.exception()
                    log.warning(
                        "ai_retry",
                        attempt=state.attempt_number,
                        code=getattr(previous, "code", None),
                        error=type(previous).__name__,
                    )
                return await self._client.aio.models.generate_content(
                    model=self._model, contents=contents, config=config
                )
        raise AssertionError("unreachable: AsyncRetrying с reraise=True")

    async def _generate_any_key(
        self, contents: types.Content, config: types.GenerateContentConfig
    ) -> Any:
        """Суточная квота или отказ ключа — тот же запрос со следующим ключом (квота считается
        на проект Google Cloud). Назад не переключаемся; ключи
        кончились — AIUnavailable."""
        while True:
            try:
                return await self._generate(contents, config)
            except errors.APIError as exc:
                if key_rejected(exc):
                    reason = f"Gemini {exc.code}: {exc.status}"
                elif exc.code in FATAL_STATUS:
                    raise AIUnavailable(f"Gemini {exc.code}: {exc.status}") from exc
                elif quota := quota_exhausted(exc, self._cfg.retry_wait_max_seconds):
                    reason = f"квота Gemini исчерпана: {quota}"
                else:
                    raise
                if self._key_index + 1 >= len(self._api_keys):
                    suffix = f" (ключей: {len(self._api_keys)})" if len(self._api_keys) > 1 else ""
                    raise AIUnavailable(reason + suffix) from exc
                self._key_index += 1
                self._client = self._client_factory(self._api_keys[self._key_index])
                log.warning(
                    "ai_key_switched",
                    key=self._key_index + 1,
                    keys=len(self._api_keys),
                    reason=reason,
                )

    async def classify(self, data: ClassifyInput) -> ClassifyResult:
        contents = types.Content(
            role="user",
            parts=[
                types.Part.from_bytes(data=data.image_jpeg, mime_type="image/jpeg"),
                types.Part.from_text(text=describe(data)),
            ],
        )
        raw: str | None = None
        error = "нет ответа"
        usage = Usage()
        for _ in range(self._cfg.invalid_answer_retries + 1):
            response = await self._generate_any_key(contents, self._config)
            usage = _usage(response, usage)
            raw = response.text
            try:
                result = parse_answer(raw or "", self._categories)
            except InvalidAnswer as exc:
                error = str(exc)
                log.warning("ai_invalid_answer", username=data.username, error=error)
                continue
            result.prices = [normalize(p) for p in result.prices]
            log.info(
                "ai_classified",
                username=data.username,
                is_selling=result.is_selling,
                confidence=result.confidence,
                category=result.category,
                prices=len(result.prices),
                key=self._key_index + 1,
                prompt_tokens=usage.prompt_tokens,
                output_tokens=usage.output_tokens,
            )
            return ClassifyResult(result, self._model, raw, None, usage)
        return ClassifyResult(None, self._model, raw, error, usage)

    async def listen(self, data: VoiceInput) -> VoiceResult:
        contents = types.Content(
            role="user",
            parts=[
                types.Part.from_bytes(data=data.audio_mp3, mime_type="audio/mp3"),
                types.Part.from_text(text=describe_voice(data)),
            ],
        )
        raw: str | None = None
        error = "нет ответа"
        usage = Usage()
        for _ in range(self._cfg.invalid_answer_retries + 1):
            response = await self._generate_any_key(contents, self._voice_config)
            usage = _usage(response, usage)
            raw = response.text
            try:
                answer = parse_voice(raw or "")
            except InvalidAnswer as exc:
                error = str(exc)
                log.warning("ai_invalid_answer", username=data.username, error=error, voice=True)
                continue
            prices = [normalize(p.to_price()) for p in answer.prices]
            log.info(
                "ai_listened",
                username=data.username,
                prices=len(prices),
                speech=answer.speech,
                key=self._key_index + 1,
                prompt_tokens=usage.prompt_tokens,
                output_tokens=usage.output_tokens,
            )
            return VoiceResult(prices, answer.speech, raw, None, usage)
        return VoiceResult([], None, raw, error, usage)


def _usage(response: Any, previous: Usage) -> Usage:
    meta = getattr(response, "usage_metadata", None)
    if meta is None:
        return previous
    return Usage(
        prompt_tokens=previous.prompt_tokens + (meta.prompt_token_count or 0),
        output_tokens=previous.output_tokens + (meta.candidates_token_count or 0),
        thinking_tokens=previous.thinking_tokens + (meta.thoughts_token_count or 0),
    )
