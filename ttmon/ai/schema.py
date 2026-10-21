import json
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

# manual — правка в панели; в схему ответа AI не входит (там свой enum)
PriceSource = Literal["screen", "chat", "voice", "caption", "manual"]
Currency = Literal["KZT", "RUB", "USD", "OTHER"]


class Price(BaseModel):
    item: str | None = None
    raw: str | None = None  # как цена написана/сказана: страховка для prices.normalize
    amount: int = Field(gt=0)
    currency: Currency = "KZT"
    source: PriceSource


class Classification(BaseModel):
    is_selling: bool
    confidence: float = Field(ge=0, le=1)
    category: str
    products: list[str] = []
    prices: list[Price] = []
    reason: str


class InvalidAnswer(ValueError):
    pass


def response_schema(categories: list[str]) -> dict[str, Any]:
    """JSON Schema для structured output. category — enum из config.yaml."""
    price = {
        "type": "object",
        "properties": {
            "item": {"type": ["string", "null"]},
            "raw": {"type": ["string", "null"]},
            "amount": {"type": "integer", "minimum": 1},
            "currency": {"type": "string", "enum": ["KZT", "RUB", "USD", "OTHER"]},
            "source": {"type": "string", "enum": ["screen", "chat", "voice", "caption"]},
        },
        "required": ["amount", "currency", "source"],
    }
    return {
        "type": "object",
        "properties": {
            "is_selling": {"type": "boolean"},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "category": {"type": "string", "enum": categories},
            "products": {"type": "array", "items": {"type": "string"}},
            "prices": {"type": "array", "items": price},
            "reason": {"type": "string"},
        },
        "required": ["is_selling", "confidence", "category", "products", "prices", "reason"],
    }


def parse_answer(raw: str, categories: list[str]) -> Classification:
    try:
        result = Classification.model_validate(json.loads(raw))
    except (ValueError, ValidationError) as exc:
        raise InvalidAnswer(f"ответ не по схеме: {exc}".splitlines()[0]) from exc
    if result.category not in categories:
        raise InvalidAnswer(f"категория не из списка: {result.category!r}")
    return result
