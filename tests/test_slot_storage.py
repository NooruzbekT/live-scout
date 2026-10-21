from pathlib import Path

import pytest

from ttmon.collector.run import TEST_SLOT, _slot_storage, final_status
from ttmon.settings import PROJECT_ROOT, AppConfig, load_config


def _config(data_dir: Path, test_dir: Path) -> AppConfig:
    base = load_config()
    paths = base.paths.model_copy(update={"data_dir": data_dir, "test_data_dir": test_dir})
    return base.model_copy(update={"paths": paths})


def test_test_slot_starts_clean(tmp_path: Path) -> None:
    test_dir = tmp_path / "data" / "test"
    (test_dir / "screenshots").mkdir(parents=True)
    (test_dir / "old.txt").write_text("x")

    db_file, screenshots = _slot_storage(_config(tmp_path / "data", test_dir), TEST_SLOT)

    assert not test_dir.exists()
    assert db_file == test_dir.resolve() / "ttmon.db"
    assert screenshots == test_dir.resolve() / "screenshots"


@pytest.mark.parametrize(
    ("data_dir", "test_dir"),
    [
        (PROJECT_ROOT, PROJECT_ROOT / "test"),
        (Path("data"), Path("data")),
        (Path("data"), Path("other")),
    ],
)
def test_unsafe_test_dir_rejected(data_dir: Path, test_dir: Path) -> None:
    with pytest.raises(ValueError, match="test_data_dir"):
        _slot_storage(_config(data_dir, test_dir), TEST_SLOT)


@pytest.mark.parametrize(
    ("saved", "failed", "selling", "status"),
    [
        (25, 0, 20, "ok"),
        (25, 3, 21, "ok"),
        (25, 0, 19, "partial"),
        (25, 0, 0, "failed"),
        (3, 0, None, "ok"),
        (3, 1, None, "partial"),
        (0, 2, None, "failed"),
    ],
)
def test_final_status(saved: int, failed: int, selling: int | None, status: str) -> None:
    assert final_status(saved, failed, selling, min_selling=20) == status
