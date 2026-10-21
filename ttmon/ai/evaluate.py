"""Eval классификатора: размеченные скриншоты → accuracy продающий/нет,
accuracy категории, доля найденных цен."""

import contextlib
import json
import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog
import yaml
from pydantic import BaseModel

from ttmon.ai.base import ChatLine, Classifier, ClassifyInput, ClassifyResult
from ttmon.ai.gemini import AIUnavailable
from ttmon.logging import redact
from ttmon.media.layout import ai_input_path
from ttmon.media.resize import fit_width
from ttmon.settings import PROJECT_ROOT, AppConfig, EvaluationConfig

log = structlog.get_logger(__name__)

LABELS_FILE = "labels.yaml"
IMAGES_DIR = "images"
RESULTS_FILE = "results.jsonl"


class Label(BaseModel):
    is_selling: bool | None = None  # None = ещё не размечено
    category: str | None = None
    prices: list[int] = []  # цены, видимые на скриншоте (в тенге)


class EvalItem(BaseModel):
    file: str
    username: str
    nickname: str = ""
    title: str = ""
    viewers: int = 0
    chat: list[dict[str, str | None]] = []
    label: Label = Label()


def load_items(eval_dir: Path) -> list[EvalItem]:
    path = eval_dir / LABELS_FILE
    if not path.exists():
        return []
    data: Any = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    return [EvalItem.model_validate(item) for item in data]


def save_items(eval_dir: Path, items: list[EvalItem]) -> None:
    eval_dir.mkdir(parents=True, exist_ok=True)
    data = [item.model_dump() for item in items]
    text = yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=100)
    (eval_dir / LABELS_FILE).write_text(text, encoding="utf-8")


def prepare(eval_dir: Path, db_files: list[Path], count: int) -> int:
    """Добавить до count новых скриншотов из собранных эфиров; уже размеченные не трогает."""
    items = load_items(eval_dir)
    known = {item.file for item in items}
    images = eval_dir / IMAGES_DIR
    images.mkdir(parents=True, exist_ok=True)
    added = 0
    for db_file in db_files:
        if not db_file.exists():
            continue
        # with по соединению sqlite3 только коммитит, но не закрывает его
        with contextlib.closing(sqlite3.connect(db_file)) as conn:
            rows = conn.execute(
                "select username, nickname, title, viewers, chat_json, screenshot_path, date"
                " from streams order by id"
            ).fetchall()
        for username, nickname, title, viewers, chat_json, shot, day in rows:
            if added >= count:
                break
            stamped = Path(shot) if Path(shot).is_absolute() else PROJECT_ROOT / shot
            unstamped = ai_input_path(stamped)
            source = unstamped if unstamped is not None and unstamped.exists() else stamped
            name = f"{day}_{stamped.name}"
            if name in known or not source.exists():
                continue
            shutil.copy2(source, images / name)
            items.append(
                EvalItem(
                    file=name,
                    username=username,
                    nickname=nickname,
                    title=title,
                    viewers=viewers,
                    chat=json.loads(chat_json),
                )
            )
            known.add(name)
            added += 1
    save_items(eval_dir, items)
    return added


@dataclass
class Metrics:
    selling_total: int = 0
    selling_correct: int = 0
    category_total: int = 0
    category_correct: int = 0
    prices_total: int = 0
    prices_found: int = 0
    invalid_answers: int = 0
    prompt_tokens: int = 0
    output_tokens: int = 0

    @staticmethod
    def _ratio(part: int, total: int) -> float | None:
        return part / total if total else None

    @property
    def selling_accuracy(self) -> float | None:
        return self._ratio(self.selling_correct, self.selling_total)

    @property
    def category_accuracy(self) -> float | None:
        return self._ratio(self.category_correct, self.category_total)

    @property
    def price_recall(self) -> float | None:
        return self._ratio(self.prices_found, self.prices_total)

    def passed(self, thresholds: EvaluationConfig) -> bool:
        checks = [
            (self.selling_accuracy, thresholds.selling_accuracy),
            (self.category_accuracy, thresholds.category_accuracy),
            (self.price_recall, thresholds.price_recall),
        ]
        return all(value is not None and value >= limit for value, limit in checks)


async def run_eval(
    classifier: Classifier, cfg: AppConfig, eval_dir: Path
) -> tuple[Metrics, list[dict[str, Any]]]:
    metrics = Metrics()
    results: list[dict[str, Any]] = []
    for item in load_items(eval_dir):
        label = item.label
        if label.is_selling is None:
            continue
        image = fit_width(
            (eval_dir / IMAGES_DIR / item.file).read_bytes(),
            cfg.ai.max_image_width,
            cfg.media.jpeg_quality,
        )
        data = ClassifyInput(
            image_jpeg=image,
            username=item.username,
            nickname=item.nickname,
            title=item.title,
            viewers=item.viewers,
            chat=[ChatLine(m.get("author"), m.get("text") or "") for m in item.chat],
        )
        try:
            answer = await classifier.classify(data)
        except AIUnavailable:
            raise
        except Exception as exc:
            # один сбой API не должен терять весь прогон: считается невалидным ответом
            error = redact(str(exc).splitlines()[0])
            log.warning("eval_ai_failed", file=item.file, error=error)
            answer = ClassifyResult(None, classifier.model, None, error)
        metrics.prompt_tokens += answer.usage.prompt_tokens
        metrics.output_tokens += answer.usage.output_tokens
        c = answer.classification
        metrics.selling_total += 1
        if c is None:
            metrics.invalid_answers += 1
        predicted_selling = (
            c is not None and c.is_selling and c.confidence >= cfg.ai.selling_threshold
        )
        metrics.selling_correct += predicted_selling == label.is_selling
        if label.is_selling and label.category:
            metrics.category_total += 1
            metrics.category_correct += c is not None and c.category == label.category
        found = {p.amount for p in c.prices} if c else set()
        metrics.prices_total += len(label.prices)
        metrics.prices_found += sum(amount in found for amount in label.prices)
        results.append(
            {
                "file": item.file,
                "label": label.model_dump(),
                "predicted": c.model_dump() if c else None,
                "error": answer.error,
                "raw": answer.raw,
            }
        )
    lines = "\n".join(json.dumps(r, ensure_ascii=False) for r in results)
    (eval_dir / RESULTS_FILE).write_text(lines + "\n" if lines else "", encoding="utf-8")
    return metrics, results
