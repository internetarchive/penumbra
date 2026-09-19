import asyncio
import logging
from contextlib import suppress
from unittest.mock import MagicMock

import pytest

from penumbra import worker

# Outer guard, so a regression fails the test rather than hanging the suite. The
# awaits under test are the ones that block indefinitely in production, so an
# unbounded wait here is exactly the bug being tested for.
GUARD_TIMEOUT = 5.0


@pytest.fixture(autouse=True)
def fresh_shutdown_event(monkeypatch):
    """
    Replace `shutdown_event`, rather than just clearing it.

    It is module state, and an `asyncio.Event` binds itself to the first loop that
    awaits it. pytest-asyncio hands each test its own loop, so a shared event
    raises "bound to a different event loop" from the second test onwards.
    """
    monkeypatch.setattr(worker, "shutdown_event", asyncio.Event())


@pytest.fixture
def fast_watchdog(monkeypatch):
    """Collapse the watchdog's timers so a test runs in milliseconds, not minutes."""
    monkeypatch.setattr(
        worker.settings, "watchdog_interval_seconds", 0.01, raising=False
    )
    monkeypatch.setattr(
        worker.settings, "broker_unhealthy_exit_seconds", 0.05, raising=False
    )


def client_stub(usable: bool) -> MagicMock:
    client = MagicMock()
    client.is_usable = MagicMock(return_value=usable)
    return client


async def never():
    """Stands in for the two awaits that block forever: `__anext__` and `acquire`."""
    await asyncio.sleep(GUARD_TIMEOUT * 10)


@pytest.mark.asyncio
async def test_or_shutdown_gives_up_when_the_signal_arrives_first():
    """
    The regression this replaces. The consume loop tested `shutdown_event` in its
    body, so the test was only reached once a delivery arrived -- and both awaits
    ahead of it block indefinitely whenever the broker has nothing to push or every
    page slot is busy. On an idle or wedged instance SIGTERM therefore did nothing,
    systemd waited out TimeoutStopSec and sent SIGKILL, and the browser-pool
    cleanup in `main`'s `finally` never ran.
    """

    async def signal_soon():
        await asyncio.sleep(0.01)
        worker.shutdown_event.set()

    signaller = asyncio.create_task(signal_soon())
    try:
        result = await asyncio.wait_for(
            worker.or_shutdown(never()), timeout=GUARD_TIMEOUT
        )
    finally:
        await signaller

    assert result is worker.SHUTDOWN


@pytest.mark.asyncio
async def test_or_shutdown_returns_the_value_when_the_await_wins():
    """The ordinary path: no signal pending, so the result passes straight through."""

    async def deliver():
        return "message"

    assert await worker.or_shutdown(deliver()) == "message"


@pytest.mark.asyncio
async def test_or_shutdown_returns_immediately_if_already_shutting_down():
    """
    Shutdown is latched, so once set every later call short-circuits. Without this
    the loop would consume one more message per iteration on the way out.
    """
    worker.shutdown_event.set()
    result = await asyncio.wait_for(worker.or_shutdown(never()), timeout=GUARD_TIMEOUT)
    assert result is worker.SHUTDOWN


@pytest.mark.asyncio
async def test_or_shutdown_prefers_shutdown_over_a_simultaneous_delivery():
    """
    A delivery that lands in the same moment as the signal is left unacked for the
    broker to redeliver, rather than started with no time left to finish it.
    """
    worker.shutdown_event.set()

    async def deliver():
        return "message"

    assert await worker.or_shutdown(deliver()) is worker.SHUTDOWN


@pytest.mark.asyncio
async def test_or_shutdown_cancels_the_loser_and_leaves_no_pending_task():
    """
    The abandoned await must not outlive the race. A surviving `__anext__` would go
    on holding a delivery nobody is going to process.
    """
    started = asyncio.Event()

    async def blocks():
        started.set()
        await asyncio.sleep(GUARD_TIMEOUT * 10)

    before = asyncio.all_tasks()
    race = asyncio.create_task(worker.or_shutdown(blocks()))
    await started.wait()
    worker.shutdown_event.set()
    assert await asyncio.wait_for(race, timeout=GUARD_TIMEOUT) is worker.SHUTDOWN

    # Let the cancellation settle, then confirm nothing of ours is still running.
    await asyncio.sleep(0.05)
    leaked = asyncio.all_tasks() - before - {asyncio.current_task()}
    assert not [task for task in leaked if not task.done()]


@pytest.mark.asyncio
async def test_or_shutdown_propagates_the_awaitables_exception():
    """
    A real failure from the await -- `StopAsyncIteration` when the iterator closes,
    an AMQP error -- has to reach the caller, which decides whether it ends the
    loop. Swallowing it here would turn a closed iterator into a silent idle spin.
    """

    async def stop():
        raise StopAsyncIteration

    with pytest.raises(StopAsyncIteration):
        await worker.or_shutdown(stop())


@pytest.mark.asyncio
async def test_watchdog_exits_when_the_topology_stays_unusable(fast_watchdog, caplog):
    """
    The August outages, and the reason this exists. The connection drops,
    aio-pika's reconnect loop is never signalled, and the process sits alive with
    no socket and nothing in the log -- ten hours, in the worst case seen. Nothing
    else notices: `connect` is only reached from a publish, and an instance that
    consumes nothing publishes nothing.
    """
    with caplog.at_level(logging.ERROR, logger="penumbra.worker"):
        reason = await asyncio.wait_for(
            worker.watch_broker(client_stub(usable=False)),
            timeout=GUARD_TIMEOUT,
        )

    # Returned rather than raised or recorded in module state: `main` turns it
    # into the exception it exits on, so the last line before the restart names
    # the reason. There is no metric -- a counter incremented on the way out of a
    # process is almost never scraped before the process is gone.
    assert "unusable for" in reason
    # Shutdown too, so the consume loop unparks and the drain runs.
    assert worker.shutdown_event.is_set()
    assert "is not recovering on its own" in caplog.text


@pytest.mark.asyncio
async def test_watchdog_leaves_a_healthy_instance_alone(fast_watchdog):
    """It must not restart a working instance, however long it runs."""
    watchdog = asyncio.create_task(worker.watch_broker(client_stub(usable=True)))
    await asyncio.sleep(0.2)  # many intervals at the collapsed timings

    assert not watchdog.done()

    # An ordinary shutdown returns no reason, so `main` exits zero.
    worker.shutdown_event.set()
    assert await asyncio.wait_for(watchdog, timeout=GUARD_TIMEOUT) is None


@pytest.mark.asyncio
async def test_watchdog_forgives_an_outage_that_recovers(fast_watchdog):
    """
    A broker restart or a brief partition must not cost a restart. The grace period
    is the whole point of not acting on the first failed check.
    """
    client = client_stub(usable=False)
    watchdog = asyncio.create_task(worker.watch_broker(client))

    # Unhealthy for less than the threshold, then back.
    await asyncio.sleep(0.03)
    client.is_usable.return_value = True
    await asyncio.sleep(0.15)

    assert not watchdog.done()

    # An ordinary shutdown returns no reason, so `main` exits zero.
    worker.shutdown_event.set()
    assert await asyncio.wait_for(watchdog, timeout=GUARD_TIMEOUT) is None


@pytest.mark.asyncio
async def test_watchdog_stops_promptly_on_shutdown(fast_watchdog):
    """
    It must not hold up a SIGTERM. The interval is the wait, so it has to be
    interruptible rather than slept through.
    """
    watchdog = asyncio.create_task(worker.watch_broker(client_stub(usable=True)))
    await asyncio.sleep(0)
    worker.shutdown_event.set()

    assert await asyncio.wait_for(watchdog, timeout=GUARD_TIMEOUT) is None


@pytest.mark.asyncio
async def test_a_watchdog_that_dies_brings_the_process_down(caplog):
    """
    `main` only reads the watchdog's outcome once the consume loop has stopped,
    and the watchdog is what stops it -- so without this the loop would go on
    consuming with nothing left watching for a stall, silently, which is the
    exact state the watchdog was added to make impossible.
    """

    async def boom():
        raise AttributeError("aio-pika changed shape under is_usable")

    watchdog = asyncio.ensure_future(boom())
    await asyncio.sleep(0)

    with caplog.at_level(logging.ERROR, logger="penumbra.worker"):
        worker.report_watchdog_death(watchdog)

    assert worker.shutdown_event.is_set()
    assert "nothing is watching this instance for stalls" in caplog.text
    # The cause is carried, so the restart is diagnosable.
    assert "aio-pika changed shape" in caplog.text


@pytest.mark.asyncio
async def test_a_watchdog_that_returns_or_is_cancelled_is_not_a_death(caplog):
    """
    The two ordinary endings. Returning a reason is the watchdog doing its job --
    it has already set the event and `main` raises `InstanceStalled` from the
    returned reason -- and cancellation is `main`'s own `finally` tearing it
    down. Neither may be reported as the watchdog having failed.
    """

    async def clean():
        return "AMQP topology unusable for 300s"

    finished = asyncio.ensure_future(clean())
    await asyncio.sleep(0)

    async def forever():
        await asyncio.sleep(GUARD_TIMEOUT * 10)

    cancelled = asyncio.ensure_future(forever())
    cancelled.cancel()
    with suppress(asyncio.CancelledError):
        await cancelled

    with caplog.at_level(logging.ERROR, logger="penumbra.worker"):
        worker.report_watchdog_death(finished)
        worker.report_watchdog_death(cancelled)

    assert not worker.shutdown_event.is_set()
    assert caplog.text == ""


@pytest.mark.asyncio
async def test_abandon_swallows_a_failed_tasks_exception(caplog):
    """
    A discarded task whose exception is never read resurfaces as "Task exception
    was never retrieved" once the loop collects it -- noise that reads like a fault
    and is not one, at exactly the moment the log matters most.
    """

    async def boom():
        raise RuntimeError("discarded")

    task = asyncio.ensure_future(boom())
    await asyncio.sleep(0)  # let it run and fail
    assert task.done()

    worker._abandon(task)
    del task
    await asyncio.sleep(0.05)

    assert "never retrieved" not in caplog.text
