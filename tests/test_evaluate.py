import contextlib
import io
import sqlite3
from pathlib import Path

import pytest
from PIL import Image

from ttmon.ai import evaluate
from ttmon.ai.base import ClassifyInput, ClassifyResult, VoiceInput, VoiceResult
from ttmon.ai.schema import Classification, Price
from ttmon.db.migrate import upgrade_to_head
from ttmon.settings import load_config

CFG = load_config()


def _jpeg() -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (64, 128), (10, 20, 30)).save(out, "JPEG")
    return out.getvalue()


def _db_with_streams(tmp_path: Path, usernames: list[str]) -> Path:
    db_file = tmp_path / "ttmon.db"
    upgrade_to_head(db_file)
    shots = tmp_path / "shots"
    shots.mkdir()
    with contextlib.closing(sqlite3.connect(db_file)) as conn, conn:
        conn.execute(
            "insert into runs (date, slot, status, started_at, rooms_opened, rooms_failed,"
            " selling_found) values (?, 'test', 'ok', ?, 0, 0, 0)",
            ("2026-10-15", "2026-10-15 00:00:00.000000"),
        )
        for user in usernames:
            shot = shots / f"{user}_2215.jpg"
            shot.write_bytes(_jpeg())
            conn.execute(
                "insert into streams (run_id, date, slot, username, nickname, room_id, title,"
                " viewers, followers, captured_at, url, source, screenshot_path, chat_json,"
                " manual_override, excluded) values (1, ?, 'test', ?, 'Ник', ?, '', 10, 0, ?,"
                " '', 'search:x', ?, '[]', 0, 0)",
                ("2026-10-15", user, user, "2026-10-15 17:15:00.000000", str(shot)),
            )
    return db_file


def test_prepare_is_incremental(tmp_path: Path) -> None:
    db_file = _db_with_streams(tmp_path, ["a", "b", "c"])
    eval_dir = tmp_path / "eval"

    assert evaluate.prepare(eval_dir, [db_file, tmp_path / "missing.db"], 2) == 2
    items = evaluate.load_items(eval_dir)
    items[0].label.is_selling = True
    evaluate.save_items(eval_dir, items)
    assert evaluate.prepare(eval_dir, [db_file], 10) == 1

    items = evaluate.load_items(eval_dir)
    assert [i.username for i in items] == ["a", "b", "c"]
    assert items[0].label.is_selling is True
    assert (eval_dir / evaluate.IMAGES_DIR / items[2].file).is_file()


class ScriptedClassifier:
    model = "fake"

    def __init__(self, answers: dict[str, Classification | None]) -> None:
        self._answers = answers

    async def classify(self, data: ClassifyInput) -> ClassifyResult:
        return ClassifyResult(self._answers[data.username], "fake", "{}")

    async def listen(self, data: VoiceInput) -> VoiceResult:
        raise NotImplementedError


def _answer(selling: bool, category: str, amounts: list[int]) -> Classification:
    return Classification(
        is_selling=selling,
        confidence=0.9 if selling else 0.1,
        category=category,
        prices=[Price(amount=a, source="screen") for a in amounts],
        reason="",
    )


@pytest.fixture
def four_streams(tmp_path: Path) -> Path:
    return _db_with_streams(tmp_path, ["a", "b", "c", "d"])


async def test_run_eval_metrics(tmp_path: Path, four_streams: Path) -> None:
    db_file = four_streams
    eval_dir = tmp_path / "eval"
    evaluate.prepare(eval_dir, [db_file], 10)
    items = evaluate.load_items(eval_dir)
    labels = {
        "a": evaluate.Label(is_selling=True, category="Обувь", prices=[3500, 1500]),
        "b": evaluate.Label(is_selling=False),
        "c": evaluate.Label(is_selling=True, category="Сумки и аксессуары", prices=[9000]),
    }
    for item in items:
        item.label = labels.get(item.username, evaluate.Label())
    evaluate.save_items(eval_dir, items)
    classifier = ScriptedClassifier(
        {
            "a": _answer(True, "Обувь", [3500]),
            "b": _answer(True, "Другое", []),
            "c": None,
        }
    )

    metrics, results = await evaluate.run_eval(classifier, CFG, eval_dir)

    assert (metrics.selling_total, metrics.selling_correct) == (3, 1)
    assert (metrics.category_total, metrics.category_correct) == (2, 1)
    assert (metrics.prices_total, metrics.prices_found) == (3, 1)
    assert metrics.invalid_answers == 1
    assert len(results) == 3
    assert not metrics.passed(CFG.evaluation)
    assert (eval_dir / evaluate.RESULTS_FILE).read_text(encoding="utf-8").count("\n") == 3


def test_passed_requires_every_metric() -> None:
    perfect = evaluate.Metrics(10, 10, 5, 5, 4, 4)
    assert perfect.passed(CFG.evaluation)
    no_prices = evaluate.Metrics(10, 10, 5, 5, 0, 0)
    assert not no_prices.passed(CFG.evaluation)


@pytest.mark.parametrize("missing", ["labels"])
def test_load_items_empty_dir(tmp_path: Path, missing: str) -> None:
    assert evaluate.load_items(tmp_path / missing) == []


def test_prepare_prefers_unstamped_ai_input(tmp_path: Path) -> None:
    root = tmp_path / "data"
    shot = root / "screenshots" / "2026-10-15" / "test" / "u_2215.jpg"
    unstamped = root / "ai_inputs" / "2026-10-15" / "test" / "u_2215.jpg"
    for path, color in ((shot, (255, 0, 0)), (unstamped, (0, 0, 255))):
        path.parent.mkdir(parents=True)
        out = io.BytesIO()
        Image.new("RGB", (8, 8), color).save(out, "JPEG")
        path.write_bytes(out.getvalue())
    db_file = _db_with_streams(tmp_path, [])
    with contextlib.closing(sqlite3.connect(db_file)) as conn, conn:
        conn.execute(
            "insert into streams (run_id, date, slot, username, nickname, room_id, title,"
            " viewers, followers, captured_at, url, source, screenshot_path, chat_json,"
            " manual_override, excluded) values (1, '2026-10-15', 'test', 'u', '', '1', '',"
            " 0, 0, '2026-10-15 17:15:00.000000', '', 'search:x', ?, '[]', 0, 0)",
            (str(shot),),
        )

    evaluate.prepare(tmp_path / "eval", [db_file], 5)

    [item] = evaluate.load_items(tmp_path / "eval")
    copied = Image.open(tmp_path / "eval" / evaluate.IMAGES_DIR / item.file).convert("RGB")
    red, _, blue = copied.getpixel((4, 4))  # type: ignore[misc]
    assert blue > red


class BrokenAI:
    model = "broken"

    async def classify(self, data: ClassifyInput) -> ClassifyResult:
        raise ConnectionError("503")

    async def listen(self, data: VoiceInput) -> VoiceResult:
        raise NotImplementedError


async def test_transient_ai_error_counts_as_invalid(tmp_path: Path, four_streams: Path) -> None:
    eval_dir = tmp_path / "eval"
    evaluate.prepare(eval_dir, [four_streams], 2)
    items = evaluate.load_items(eval_dir)
    for item in items:
        item.label = evaluate.Label(is_selling=False)
    evaluate.save_items(eval_dir, items)

    metrics, _ = await evaluate.run_eval(BrokenAI(), CFG, eval_dir)

    assert (metrics.selling_total, metrics.invalid_answers) == (2, 2)
