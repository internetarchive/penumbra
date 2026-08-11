import asyncio
import json
import logging
from time import monotonic
from unittest.mock import AsyncMock, MagicMock, patch

import aio_pika
import pytest
from playwright.async_api import async_playwright
from prometheus_client import REGISTRY

from penumbra import worker
from penumbra.models import UmbraMessage, UmbraResponse
from penumbra.queues import AsyncMessageClient
from penumbra.worker import Settings, process_page, publish_umbra_response


@pytest.fixture
def fresh_shutdown_event():
    """
    `worker.shutdown_event` is created at import and binds to the first event loop
    that awaits it. That is fine in production, where there is one loop for the
    life of the process, but each test gets its own -- so hand every test that
    touches it a fresh Event to bind to its own loop.
    """
    event = asyncio.Event()
    with patch.object(worker, "shutdown_event", event):
        yield event


def message_maker(url: str) -> MagicMock:
    message = MagicMock(spec=aio_pika.IncomingMessage)
    message.body = json.dumps(
        {
            "url": url,
            "metadata": {
                "heritableData": {
                    "source": "test",
                    "heritable": ["source", "heritable"],
                }
            },
            "clientId": "urls",
        }
    ).encode()
    return message


@pytest.mark.asyncio
async def test_process_page(monkeypatch):
    async with async_playwright() as playwright:
        # Launch a headless browser (you can set headless=False to see the browser in action)
        browser = await playwright.chromium.launch(headless=True)

        # Use a real URL for testing
        test_url = "https://example.com"
        message = message_maker(test_url)

        # Mock the publish_message method of the AsyncMessageClient class
        with patch.object(
            AsyncMessageClient, "publish_message", new_callable=AsyncMock
        ) as mock_publish_message:
            # Call the process_page function with the real browser instance and test URL
            client = AsyncMessageClient()
            await process_page(client, browser, message)

            # Assert that publish_message was called with the expected arguments
            assert mock_publish_message.called
            requested_docs_pre = REGISTRY.get_sample_value(
                "penumbra_resources_requested_total", {"resource_type": "document"}
            )
            fetched_docs_pre = REGISTRY.get_sample_value(
                "penumbra_resources_fetched_total", {"resource_type": "document"}
            )
            monkeypatch.setenv("penumbra_skip_resource_document", "1")
            settings = Settings()
            assert settings.skip_resource_document
            await process_page(client, browser, message)
            requested_docs_post = REGISTRY.get_sample_value(
                "penumbra_resources_requested_total", {"resource_type": "document"}
            )
            fetched_docs_post = REGISTRY.get_sample_value(
                "penumbra_resources_fetched_total", {"resource_type": "document"}
            )
            assert requested_docs_pre < requested_docs_post
            assert fetched_docs_pre == fetched_docs_post
        # Close the browser at the end of the test
        await browser.close()


@pytest.mark.asyncio
async def test_publish_umbra_response_drops_over_length_urls():
    """URLs longer than settings.max_url_length are dropped, not published."""
    parent_message = UmbraMessage(json.loads(message_maker("https://example.com").body))

    short_url = "https://example.com/ok"
    long_url = "https://example.com/" + "a" * 3000  # > 2083 default limit

    dropped_pre = REGISTRY.get_sample_value("penumbra_urls_dropped_too_long_total") or 0

    client = MagicMock(spec=AsyncMessageClient)
    client.publish_message = AsyncMock()

    await publish_umbra_response(client, parent_message, {short_url, long_url})

    # Only the short URL is published.
    assert client.publish_message.await_count == 1
    published = client.publish_message.await_args.args[0]
    assert published.url == short_url

    dropped_post = (
        REGISTRY.get_sample_value("penumbra_urls_dropped_too_long_total") or 0
    )
    assert dropped_post == dropped_pre + 1


@pytest.mark.asyncio
async def test_publish_with_retry_succeeds_after_transient_failure(monkeypatch):
    """A publish that fails once is retried rather than dropped."""
    monkeypatch.setattr(worker.settings, "publish_retry_base_delay_seconds", 0)

    parent_message = UmbraMessage(json.loads(message_maker("https://example.com").body))
    response = UmbraResponse(
        url="https://example.com/x",
        method="GET",
        headers={},
        parent_message=parent_message,
    )

    client = MagicMock(spec=AsyncMessageClient)
    client.publish_message = AsyncMock(side_effect=[ConnectionError("boom"), None])

    assert await worker.publish_with_retry(client, response) is True
    assert client.publish_message.await_count == 2


@pytest.mark.asyncio
async def test_publish_with_retry_drops_after_exhausting_attempts(monkeypatch):
    """Once attempts run out the URL is dropped and counted, without raising."""
    monkeypatch.setattr(worker.settings, "publish_retry_base_delay_seconds", 0)
    monkeypatch.setattr(worker.settings, "publish_max_attempts", 3)

    parent_message = UmbraMessage(json.loads(message_maker("https://example.com").body))
    response = UmbraResponse(
        url="https://example.com/x",
        method="GET",
        headers={},
        parent_message=parent_message,
    )

    client = MagicMock(spec=AsyncMessageClient)
    client.publish_message = AsyncMock(side_effect=ConnectionError("boom"))

    dropped_pre = (
        REGISTRY.get_sample_value("penumbra_urls_dropped_publish_failed_total") or 0
    )

    assert await worker.publish_with_retry(client, response) is False
    assert client.publish_message.await_count == 3

    dropped_post = (
        REGISTRY.get_sample_value("penumbra_urls_dropped_publish_failed_total") or 0
    )
    assert dropped_post == dropped_pre + 1


@pytest.mark.asyncio
async def test_publish_umbra_response_survives_one_bad_url(monkeypatch):
    """One unpublishable URL must not cancel its siblings via the TaskGroup."""
    monkeypatch.setattr(worker.settings, "publish_retry_base_delay_seconds", 0)
    monkeypatch.setattr(worker.settings, "publish_max_attempts", 1)

    parent_message = UmbraMessage(json.loads(message_maker("https://example.com").body))
    bad_url = "https://example.com/bad"

    async def publish(umbra_response):
        if umbra_response.url == bad_url:
            raise ConnectionError("boom")

    client = MagicMock(spec=AsyncMessageClient)
    client.publish_message = AsyncMock(side_effect=publish)

    urls = {bad_url, "https://example.com/a", "https://example.com/b"}
    # Does not raise, and every URL is attempted.
    await publish_umbra_response(client, parent_message, urls)
    assert client.publish_message.await_count == 3


@pytest.mark.asyncio
async def test_process_page_timeout_nacks_and_closes_context(monkeypatch):
    """
    A page that outruns the deadline is nacked and its context closed, so the
    task returns and its semaphore permit and in-progress gauge are released.
    """
    monkeypatch.setattr(worker.settings, "page_timeout_seconds", 0.1)

    message = message_maker("https://example.com")
    message.ack = AsyncMock()
    message.nack = AsyncMock()

    async def hang(*args, **kwargs):
        await asyncio.sleep(60)

    browser = MagicMock()
    # Hang on the first untimed Playwright call inside the deadline.
    browser.new_context = AsyncMock(side_effect=hang)

    timeouts_pre = REGISTRY.get_sample_value("penumbra_page_timeouts_total") or 0
    in_progress_pre = REGISTRY.get_sample_value("penumbra_in_progress_pages") or 0

    client = MagicMock(spec=AsyncMessageClient)
    await asyncio.wait_for(process_page(client, browser, message), timeout=5)

    message.nack.assert_awaited_once_with(requeue=True)
    message.ack.assert_not_awaited()
    assert REGISTRY.get_sample_value("penumbra_page_timeouts_total") == timeouts_pre + 1
    # The gauge is back where it started rather than stuck above it.
    assert REGISTRY.get_sample_value("penumbra_in_progress_pages") == in_progress_pre


def page_browser_mock() -> MagicMock:
    """A browser whose context/page calls all succeed instantly."""
    page = MagicMock()
    page.route = AsyncMock()
    page.goto = AsyncMock()
    context = MagicMock()
    context.new_page = AsyncMock(return_value=page)
    context.close = AsyncMock()
    browser = MagicMock()
    browser.new_context = AsyncMock(return_value=context)
    return browser


@pytest.mark.asyncio
async def test_process_page_does_not_nack_once_the_ack_is_on_the_wire(monkeypatch):
    """
    `ack()` queues the Basic.Ack frame and only then awaits the drain, marking the
    message processed afterwards. A deadline landing on that drain must not nack:
    the broker answers PRECONDITION_FAILED for an already-acked delivery tag and
    closes the consume channel, which stops this instance consuming entirely.
    """
    monkeypatch.setattr(worker.settings, "page_timeout_seconds", 0.1)

    message = message_maker("https://example.com")
    message.nack = AsyncMock()

    async def hang():
        await asyncio.sleep(60)

    # The frame is already gone; only the drain is outstanding.
    message.ack = AsyncMock(side_effect=hang)
    message.processed = False

    client = MagicMock(spec=AsyncMessageClient)
    client.publish_message = AsyncMock()

    await asyncio.wait_for(
        process_page(client, page_browser_mock(), message), timeout=5
    )

    message.ack.assert_awaited_once()
    message.nack.assert_not_awaited()


@pytest.mark.asyncio
async def test_process_page_still_nacks_when_the_page_fails_before_the_ack():
    """The ack guard must not suppress the nack a genuine page failure needs."""
    message = message_maker("https://example.com")
    message.ack = AsyncMock()
    message.nack = AsyncMock()

    browser = page_browser_mock()
    context = await browser.new_context()
    context.new_page.return_value.goto = AsyncMock(
        side_effect=RuntimeError("navigation failed")
    )

    client = MagicMock(spec=AsyncMessageClient)
    await process_page(client, browser, message)

    message.ack.assert_not_awaited()
    message.nack.assert_awaited_once_with(requeue=True)


@pytest.mark.asyncio
async def test_process_page_does_not_count_an_unrelated_timeout_as_a_page_timeout():
    """
    The builtin TimeoutError is an OSError subclass that aio-pika can raise from a
    socket operation. Only the page deadline actually expiring may increment
    `penumbra_page_timeouts`, or the metric stops meaning what it says.
    """
    message = message_maker("https://example.com")
    message.ack = AsyncMock()
    message.nack = AsyncMock()

    browser = page_browser_mock()
    context = await browser.new_context()
    # Raised well inside a page deadline that has not expired.
    context.new_page.return_value.goto = AsyncMock(
        side_effect=TimeoutError("socket timed out")
    )

    timeouts_pre = REGISTRY.get_sample_value("penumbra_page_timeouts_total") or 0

    client = MagicMock(spec=AsyncMessageClient)
    await process_page(client, browser, message)

    assert REGISTRY.get_sample_value("penumbra_page_timeouts_total") == timeouts_pre
    # Still treated as a failure: the message goes back to the queue.
    message.nack.assert_awaited_once_with(requeue=True)


@pytest.mark.asyncio
async def test_robust_context_close_bounds_a_hanging_close(monkeypatch):
    """A context whose close() never returns is abandoned, not awaited forever."""
    monkeypatch.setattr(worker.settings, "context_close_timeout_seconds", 0.1)

    context = MagicMock()

    async def hang():
        await asyncio.sleep(60)

    context.close = AsyncMock(side_effect=hang)

    await asyncio.wait_for(worker.robust_context_close(context), timeout=5)


@pytest.mark.asyncio
async def test_warn_if_stalled_warns_when_nothing_completes(
    monkeypatch, caplog, fresh_shutdown_event
):
    """Slots in use with no completion inside the window logs one warning."""
    monkeypatch.setattr(worker.settings, "stall_check_interval_seconds", 0.05)
    monkeypatch.setattr(worker.settings, "stall_warning_seconds", 0.0)
    monkeypatch.setattr(worker, "last_completion", monotonic())

    tasks = {MagicMock()}

    with caplog.at_level(logging.WARNING, logger="penumbra.worker"):
        task = asyncio.create_task(worker.warn_if_stalled(tasks, 10))
        await asyncio.sleep(0.12)
        task.cancel()

    assert "1/10 page slots in use" in caplog.text
    assert "stopped consuming" in caplog.text


@pytest.mark.asyncio
async def test_warn_if_stalled_warns_before_any_page_has_completed(
    monkeypatch, caplog, fresh_shutdown_event
):
    """
    An instance that fills every slot and wedges before finishing a single page is
    the stall most worth hearing about. `last_completion` is therefore seeded at
    startup rather than left None until the first completion: while it was None,
    `warn_if_stalled` skipped its check and never warned at all.
    """
    monkeypatch.setattr(worker.settings, "stall_check_interval_seconds", 0.05)
    monkeypatch.setattr(worker.settings, "stall_warning_seconds", 0.0)
    # The invariant the warning rests on: there is always a time to measure from.
    assert worker.last_completion is not None

    with caplog.at_level(logging.WARNING, logger="penumbra.worker"):
        task = asyncio.create_task(worker.warn_if_stalled({MagicMock()}, 1))
        await asyncio.sleep(0.12)
        task.cancel()

    assert "1/1 page slots in use" in caplog.text


@pytest.mark.asyncio
async def test_warn_if_stalled_silent_when_idle_or_healthy(
    monkeypatch, caplog, fresh_shutdown_event
):
    """
    An idle cluster is not a stall, and neither is recent progress. Both must stay
    quiet or the warning becomes noise nobody reads.
    """
    monkeypatch.setattr(worker.settings, "stall_check_interval_seconds", 0.05)

    with caplog.at_level(logging.WARNING, logger="penumbra.worker"):
        # Idle: nothing in flight, however stale the last completion is.
        monkeypatch.setattr(worker.settings, "stall_warning_seconds", 0.0)
        monkeypatch.setattr(worker, "last_completion", monotonic() - 10_000)
        task = asyncio.create_task(worker.warn_if_stalled(set(), 10))
        await asyncio.sleep(0.12)
        task.cancel()

        # Busy, but completing well inside the window.
        monkeypatch.setattr(worker.settings, "stall_warning_seconds", 3600.0)
        monkeypatch.setattr(worker, "last_completion", monotonic())
        task = asyncio.create_task(worker.warn_if_stalled({MagicMock()}, 10))
        await asyncio.sleep(0.12)
        task.cancel()

    assert caplog.records == []


@pytest.mark.asyncio
async def test_warn_if_stalled_exits_on_shutdown(monkeypatch, fresh_shutdown_event):
    """Returns promptly on shutdown rather than blocking it for a full interval."""
    monkeypatch.setattr(worker.settings, "stall_check_interval_seconds", 60.0)

    task = asyncio.create_task(worker.warn_if_stalled(set(), 1))
    await asyncio.sleep(0)
    fresh_shutdown_event.set()
    await asyncio.wait_for(task, timeout=5)
