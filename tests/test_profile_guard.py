import pytest

from ttmon.collector.browser import open_browser
from ttmon.settings import load_config


async def test_real_profile_is_blocked_in_tests() -> None:
    with pytest.raises(pytest.fail.Exception, match="рабочий профиль"):
        async with open_browser(load_config()):
            pass
