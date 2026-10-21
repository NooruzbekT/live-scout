from pathlib import Path
from typing import Any

import pytest

from ttmon.collector import browser
from ttmon.collector import selectors as sel
from ttmon.settings import AppConfig, load_config, project_path


def _config(chrome_path: Path) -> AppConfig:
    base = load_config()
    return base.model_copy(
        update={"browser": base.browser.model_copy(update={"chrome_path": chrome_path})}
    )


def test_opens_plain_chrome_on_service_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chrome = tmp_path / "chrome.exe"
    chrome.write_bytes(b"")
    calls: list[list[str]] = []

    def fake_run(args: list[str], **_kwargs: Any) -> None:
        calls.append(args)

    monkeypatch.setattr(browser.subprocess, "run", fake_run)
    cfg = _config(chrome)
    browser.interactive_login(cfg)

    [args] = calls
    assert args[0] == str(chrome)
    assert f"--user-data-dir={project_path(cfg.browser.profile_dir)}" in args
    assert args[-1] == sel.LOGIN_URL
    assert not any("remote-debugging" in a or "enable-automation" in a for a in args)


def test_missing_chrome_is_launch_error(tmp_path: Path) -> None:
    with pytest.raises(browser.BrowserLaunchError, match="chrome_path"):
        browser.interactive_login(_config(tmp_path / "nope.exe"))
