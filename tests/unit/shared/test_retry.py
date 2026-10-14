import pytest

from candy_store.shared.application.retry import retry_on_concurrency
from candy_store.shared.application.unit_of_work import ConcurrencyError


async def test_retries_until_success() -> None:
    calls = 0

    @retry_on_concurrency(attempts=3, base_delay_seconds=0)
    async def flaky() -> str:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise ConcurrencyError
        return "ok"

    assert await flaky() == "ok"
    assert calls == 3


async def test_gives_up_after_max_attempts() -> None:
    calls = 0

    @retry_on_concurrency(attempts=2, base_delay_seconds=0)
    async def always_conflicts() -> None:
        nonlocal calls
        calls += 1
        raise ConcurrencyError

    with pytest.raises(ConcurrencyError):
        await always_conflicts()
    assert calls == 2


async def test_other_errors_are_not_retried() -> None:
    calls = 0

    @retry_on_concurrency(base_delay_seconds=0)
    async def broken() -> None:
        nonlocal calls
        calls += 1
        raise ValueError

    with pytest.raises(ValueError):  # noqa: PT011
        await broken()
    assert calls == 1
