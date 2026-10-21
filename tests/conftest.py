from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from playwright.async_api import BrowserContext, BrowserType

from ttmon.settings import load_config, project_path

REAL_PROFILE = project_path(load_config().browser.profile_dir).resolve()
_original_launch = BrowserType.launch_persistent_context


def is_real_profile(user_data_dir: Any) -> bool:
    return Path(user_data_dir).resolve() == REAL_PROFILE


@pytest.fixture(autouse=True, scope="session")
def forbid_real_chrome_profile() -> Iterator[None]:
    """Ни один тест не открывает рабочий профиль: там живая сессия TikTok."""

    async def guarded(
        self: BrowserType, user_data_dir: Any, *args: Any, **kwargs: Any
    ) -> BrowserContext:
        if is_real_profile(user_data_dir):
            pytest.fail(f"тест открывает рабочий профиль Chrome: {REAL_PROFILE}")
        return await _original_launch(self, user_data_dir, *args, **kwargs)

    BrowserType.launch_persistent_context = guarded  # type: ignore[method-assign]
    yield
    BrowserType.launch_persistent_context = _original_launch  # type: ignore[method-assign]
