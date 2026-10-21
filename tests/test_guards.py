from pathlib import Path

import pytest

from ttmon.collector.guards import (
    Action,
    GuardPolicy,
    PageState,
    Probe,
    StopSlot,
    classify,
)
from ttmon.settings import GuardsConfig, load_config

CFG: GuardsConfig = load_config().guards


def _probe(
    status: int | None = 200,
    forbidden_text: bool = False,
    captcha: bool = False,
    login_button: bool = False,
) -> Probe:
    return Probe(
        status=status, forbidden_text=forbidden_text, captcha=captcha, login_button=login_button
    )


@pytest.mark.parametrize(
    ("probe", "state"),
    [
        (_probe(), PageState.OK),
        (_probe(status=None), PageState.OK),
        (_probe(status=404), PageState.ENDED),
        (_probe(status=403), PageState.FORBIDDEN),
        (_probe(forbidden_text=True), PageState.FORBIDDEN),
        (_probe(login_button=True), PageState.LOGGED_OUT),
        (_probe(status=404, login_button=True), PageState.LOGGED_OUT),
        (_probe(captcha=True), PageState.CAPTCHA),
        (_probe(captcha=True, status=403, login_button=True), PageState.CAPTCHA),
        (_probe(status=403, login_button=True), PageState.FORBIDDEN),
    ],
)
def test_classify(probe: Probe, state: PageState) -> None:
    assert classify(probe) == state


class FakeSleep:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


async def test_forbidden_retry_then_stop() -> None:
    sleep = FakeSleep()
    policy = GuardPolicy(CFG, sleep)

    assert await policy.handle(PageState.FORBIDDEN) == Action.RETRY
    assert await policy.handle(PageState.FORBIDDEN) == Action.RETRY
    with pytest.raises(StopSlot) as exc:
        await policy.handle(PageState.FORBIDDEN)

    assert exc.value.state == PageState.FORBIDDEN
    assert sleep.calls == [CFG.forbidden_pause_minutes * 60] * 2


async def test_ok_resets_forbidden_counter() -> None:
    policy = GuardPolicy(CFG, FakeSleep())
    for _ in range(2):
        await policy.handle(PageState.FORBIDDEN)
    assert await policy.handle(PageState.OK) == Action.PROCEED
    for _ in range(2):
        assert await policy.handle(PageState.FORBIDDEN) == Action.RETRY


async def test_timeout_does_not_break_forbidden_series() -> None:
    policy = GuardPolicy(CFG, FakeSleep())
    await policy.handle(PageState.FORBIDDEN)
    await policy.handle(PageState.TIMEOUT)
    await policy.handle(PageState.FORBIDDEN)
    await policy.handle(PageState.TIMEOUT)
    with pytest.raises(StopSlot):
        await policy.handle(PageState.FORBIDDEN)


async def test_ended_skips_without_touching_counters() -> None:
    sleep = FakeSleep()
    policy = GuardPolicy(CFG, sleep)
    await policy.handle(PageState.FORBIDDEN)
    assert await policy.handle(PageState.ENDED) == Action.SKIP
    assert policy.forbidden_in_row == 1


@pytest.mark.parametrize("state", [PageState.CAPTCHA, PageState.LOGGED_OUT])
async def test_immediate_stop(state: PageState) -> None:
    sleep = FakeSleep()
    with pytest.raises(StopSlot):
        await GuardPolicy(CFG, sleep).handle(state)
    assert sleep.calls == []


async def test_timeouts_pause_after_max_in_row() -> None:
    sleep = FakeSleep()
    policy = GuardPolicy(CFG, sleep)

    results = [await policy.handle(PageState.TIMEOUT) for _ in range(CFG.timeouts_max_in_row)]

    assert results == [Action.SKIP] * CFG.timeouts_max_in_row
    assert sleep.calls == [CFG.timeouts_pause_minutes * 60]
    assert policy.timeouts_in_row == 0


def test_screenshot_name_cannot_escape_errors_dir(tmp_path: Path) -> None:
    for state in PageState:
        assert "/" not in state.value and "\\" not in state.value and ".." not in state.value
