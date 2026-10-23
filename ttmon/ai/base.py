from dataclasses import dataclass, field
from typing import Protocol

from ttmon.ai.schema import Classification, Price

NO_CHAT = "(чат пуст или комментарии выключены)"


@dataclass(frozen=True)
class ChatLine:
    author: str | None
    text: str


@dataclass(frozen=True)
class ClassifyInput:
    image_jpeg: bytes
    username: str
    nickname: str
    title: str
    viewers: int
    chat: list[ChatLine] = field(default_factory=list)


@dataclass(frozen=True)
class VoiceInput:
    audio_mp3: bytes
    username: str
    nickname: str
    title: str
    products: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Usage:
    prompt_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0


@dataclass(frozen=True)
class ClassifyResult:
    """classification None — модель так и не дала валидный ответ: эфир не идёт в отчёт;
    raw/error сохраняются для разбора."""

    classification: Classification | None
    model: str
    raw: str | None
    error: str | None = None
    usage: Usage = Usage()


@dataclass(frozen=True)
class VoiceResult:
    """prices уже с source=voice и нормализованы; при error — пустые."""

    prices: list[Price]
    speech: str | None
    raw: str | None
    error: str | None = None
    usage: Usage = Usage()


class Classifier(Protocol):
    @property
    def model(self) -> str: ...

    async def classify(self, data: ClassifyInput) -> ClassifyResult: ...

    async def listen(self, data: VoiceInput) -> VoiceResult: ...


def describe(data: ClassifyInput) -> str:
    """Тексты эфира — данные, а не инструкции: обрамлены маркерами, промпт об этом знает."""
    chat = "\n".join(f"- {m.author or '?'}: {m.text}" for m in data.chat) or NO_CHAT
    return (
        "<данные_эфира>\n"
        f"Ник: {data.nickname} (@{data.username})\n"
        f"Название эфира: {data.title or '—'}\n"
        f"Зрителей сейчас: {data.viewers}\n"
        f"Чат (последние сообщения):\n{chat}\n"
        "</данные_эфира>"
    )


def describe_voice(data: VoiceInput) -> str:
    products = ", ".join(data.products) or "—"
    return (
        "<данные_эфира>\n"
        f"Ник: {data.nickname} (@{data.username})\n"
        f"Название эфира: {data.title or '—'}\n"
        f"Товары на экране: {products}\n"
        "</данные_эфира>"
    )
