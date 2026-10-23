import sys
from pathlib import Path
from typing import Any

import pytest

from ttmon.collector import browser
from ttmon.collector import selectors as sel
from ttmon.settings import AppConfig, load_config, project_path


def _config(chrome_path: Path, login_args: list[str] | None = None) -> AppConfig:
    base = load_config()
    update: dict[str, Any] = {"chrome_path": chrome_path}
    if login_args is not None:
        update["login_args"] = login_args
    return base.model_copy(update={"browser": base.browser.model_copy(update=update)})


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


def test_login_args_before_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    chrome = tmp_path / "chrome"
    chrome.write_bytes(b"")
    calls: list[list[str]] = []
    monkeypatch.setattr(browser.subprocess, "run", lambda args, **_kw: calls.append(args))

    browser.interactive_login(_config(chrome, ["--no-sandbox"]))

    assert calls[0][-2:] == ["--no-sandbox", sel.LOGIN_URL]


def test_missing_chrome_is_launch_error(tmp_path: Path) -> None:
    with pytest.raises(browser.BrowserLaunchError, match="chrome_path"):
        browser.interactive_login(_config(tmp_path / "nope.exe"))


@pytest.mark.skipif(
    sys.platform == "win32", reason="на Windows Chrome держит lockfile, не симлинки"
)
def test_stale_singleton_removed(tmp_path: Path) -> None:
    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / "SingletonLock").symlink_to("other-host-123")
    (profile / "Preferences").write_text("{}")

    browser.prepare_profile(profile)

    assert not (profile / "SingletonLock").is_symlink()
    assert (profile / "Preferences").exists()


def test_prepare_profile_creates_dir(tmp_path: Path) -> None:
    assert browser.prepare_profile(tmp_path / "new").is_dir()
