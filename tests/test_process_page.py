import asyncio
import json
import logging
from time import monotonic
from unittest.mock import AsyncMock, MagicMock, patch

import aio_pika
import pytest
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright
from prometheus_client import REGISTRY
from pydantic import ValidationError

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
async def test_process_page_timeout_with_no_links_is_still_terminal(
    monkeypatch, caplog
):
    """
    Outrunning the page deadline is terminal even when the page reached nothing:
    having spent the budget once is reason enough not to spend it again on a
    redelivery. The loss is logged and counted rather than retried.

    The context is closed either way, so the task returns and its semaphore
    permit and in-progress gauge are released.
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
    dropped_pre = failed_pages_total("page_timeout")

    client = MagicMock(spec=AsyncMessageClient)
    with caplog.at_level(logging.WARNING, logger="penumbra.worker"):
        await asyncio.wait_for(process_page(client, browser, message), timeout=5)

    message.ack.assert_awaited_once()
    message.nack.assert_not_awaited()
    # Nothing was published: there was nothing to publish.
    client.publish_message.assert_not_called()
    assert failed_pages_total("page_timeout") == dropped_pre + 1
    # The loss is not silent -- it is the only signal that the browser is bad.
    assert "before any request was made" in caplog.text
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
async def test_process_page_never_nacks_a_message_it_has_acked(monkeypatch):
    """
    `ack()` queues the Basic.Ack frame and only then awaits the drain. An ack
    that stalls on that drain must not be followed by a nack: the broker answers
    PRECONDITION_FAILED for an already-acked delivery tag and closes the consume
    channel, which stops this instance consuming entirely.

    The settle step now sits outside the page deadline and picks exactly one of
    ack/nack, so this is structural rather than guarded by a flag. The test
    stays because it is the invariant, not the implementation.
    """
    monkeypatch.setattr(worker.settings, "amqp_ack_timeout_seconds", 0.1)

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


def failed_pages_total(reason: str) -> float:
    return (
        REGISTRY.get_sample_value("penumbra_pages_failed_total", {"reason": reason})
        or 0
    )


async def run_failing_page(error: Exception) -> MagicMock:
    """Drive one page whose `goto` raises, and hand back the message."""
    message = message_maker("https://example.com")
    message.ack = AsyncMock()
    message.nack = AsyncMock()

    browser = page_browser_mock()
    context = await browser.new_context()
    context.new_page.return_value.goto = AsyncMock(side_effect=error)

    await process_page(MagicMock(spec=AsyncMessageClient), browser, message)
    return message


@pytest.mark.parametrize(
    "message_text,reason",
    [
        (
            "Page.goto: net::ERR_CERT_COMMON_NAME_INVALID at https://example.com/",
            "ERR_CERT_COMMON_NAME_INVALID",
        ),
        (
            "Page.goto: net::ERR_NAME_NOT_RESOLVED at https://example.com/",
            "ERR_NAME_NOT_RESOLVED",
        ),
    ],
)
@pytest.mark.asyncio
async def test_process_page_drops_permanently_unreachable_pages(message_text, reason):
    """
    A site whose certificate or DNS is broken fails the same way on every
    redelivery. Requeueing it spun those URLs through the prefetch slots in a
    tight loop and starved the real backlog, so the message is acked away.
    """
    dropped_pre = failed_pages_total(reason)

    message = await run_failing_page(PlaywrightError(message_text))

    message.ack.assert_awaited_once()
    message.nack.assert_not_awaited()
    assert failed_pages_total(reason) == dropped_pre + 1


@pytest.mark.asyncio
async def test_process_page_requeues_a_net_error_that_is_our_end_of_the_wire():
    """
    Not every `net::` error is the site's fault. Losing our own connectivity says
    nothing about the URL, so those stay retryable.
    """
    requeued_pre = failed_pages_total("ERR_INTERNET_DISCONNECTED")

    message = await run_failing_page(
        PlaywrightError("Page.goto: net::ERR_INTERNET_DISCONNECTED at https://x/")
    )

    message.nack.assert_awaited_once_with(requeue=True)
    message.ack.assert_not_awaited()
    assert failed_pages_total("ERR_INTERNET_DISCONNECTED") == requeued_pre + 1


@pytest.mark.asyncio
async def test_process_page_drops_a_page_that_never_finished_navigating():
    """
    Playwright's TimeoutError means `goto` had its full navigation budget and got
    nothing. Distinct from the builtin TimeoutError our own deadline raises,
    which stays retryable because a wedged browser looks the same.
    """
    dropped_pre = failed_pages_total("navigation_timeout")

    message = await run_failing_page(
        PlaywrightTimeoutError("Page.goto: Timeout 30000ms exceeded.")
    )

    message.ack.assert_awaited_once()
    message.nack.assert_not_awaited()
    assert failed_pages_total("navigation_timeout") == dropped_pre + 1


@pytest.mark.asyncio
async def test_process_page_requeues_a_browser_side_playwright_error():
    """
    A Playwright error carrying no `net::` code is about our browser rather than
    the page -- a closed context or a dead target -- and is ours to retry.
    """
    message = await run_failing_page(
        PlaywrightError("Target page, context or browser has been closed")
    )

    message.nack.assert_awaited_once_with(requeue=True)
    message.ack.assert_not_awaited()


def test_classify_page_failure_separates_the_two_timeout_classes():
    """
    Playwright's TimeoutError is not a builtin TimeoutError subclass, which is
    what lets a navigation timeout be told apart from our page deadline. If that
    ever changes, the ordering in `classify_page_failure` silently inverts.
    """
    assert not issubclass(PlaywrightTimeoutError, TimeoutError)

    navigation = worker.classify_page_failure(
        PlaywrightTimeoutError("Page.goto: Timeout 30000ms exceeded."), False
    )
    assert navigation == worker.PageFailure(
        "navigation_timeout", requeue=False, publish=True
    )

    # Both timeouts keep their links and neither goes back on the queue: having
    # spent the budget once is reason enough not to spend it again.
    page = worker.classify_page_failure(TimeoutError("deadline"), True)
    assert page == worker.PageFailure("page_timeout", requeue=False, publish=True)

    # An unrelated socket timeout, with our deadline still unexpired. Nothing
    # here says anything about the page, so it is retried and nothing published.
    assert worker.classify_page_failure(TimeoutError("socket"), False) == (
        worker.PageFailure("TimeoutError", requeue=True, publish=False)
    )


@pytest.mark.asyncio
async def test_process_page_publishes_the_links_a_slow_page_reached(monkeypatch):
    """
    Spending the whole budget on a page is a result, not a failure. The requests
    it fired before the deadline are real links, so they are published and the
    page is acked as done rather than discarded and crawled again.
    """
    monkeypatch.setattr(worker.settings, "page_timeout_seconds", 0.2)

    message = message_maker("https://example.com")
    message.ack = AsyncMock()
    message.nack = AsyncMock()

    found = ["https://example.com/", "https://example.com/a.css"]

    browser = page_browser_mock()
    context = await browser.new_context()
    page = context.new_page.return_value

    async def slow_goto(*args, **kwargs):
        # Fire the request events the real handler would, then outlive the
        # deadline the way a page that never finishes loading does.
        for url in found:
            page.on.call_args_list[0].args[1](MagicMock(url=url))
        await asyncio.sleep(60)

    page.goto = AsyncMock(side_effect=slow_goto)

    client = MagicMock(spec=AsyncMessageClient)
    client.publish_message = AsyncMock()

    failed_pre = failed_pages_total("page_timeout")
    crawled_pre = REGISTRY.get_sample_value("penumbra_pages_crawled_total") or 0

    await asyncio.wait_for(process_page(client, browser, message), timeout=5)

    assert sorted(
        call.args[0].url for call in client.publish_message.await_args_list
    ) == sorted(found)
    message.ack.assert_awaited_once()
    message.nack.assert_not_awaited()
    assert failed_pages_total("page_timeout") == failed_pre + 1
    # Counted as a completion, so `warn_if_stalled` sees progress.
    assert REGISTRY.get_sample_value("penumbra_pages_crawled_total") == crawled_pre + 1


@pytest.mark.asyncio
async def test_process_page_keeps_links_from_a_navigation_timeout():
    """A navigation that ran out of time still yields whatever it fetched."""
    message = message_maker("https://example.com")
    message.ack = AsyncMock()
    message.nack = AsyncMock()

    browser = page_browser_mock()
    context = await browser.new_context()
    page = context.new_page.return_value

    async def timeout_after_requests(*args, **kwargs):
        page.on.call_args_list[0].args[1](MagicMock(url="https://example.com/img.png"))
        raise PlaywrightTimeoutError("Page.goto: Timeout 120000ms exceeded.")

    page.goto = AsyncMock(side_effect=timeout_after_requests)

    client = MagicMock(spec=AsyncMessageClient)
    client.publish_message = AsyncMock()

    await process_page(client, browser, message)

    assert client.publish_message.await_count == 1
    assert (
        client.publish_message.await_args.args[0].url == "https://example.com/img.png"
    )
    message.ack.assert_awaited_once()
    message.nack.assert_not_awaited()


@pytest.mark.asyncio
async def test_process_page_settles_a_slow_page_even_if_publishing_fails():
    """
    Losing the links must not strand the message: it still leaves the queue
    rather than being retried, because the retry would spend the budget again.
    """
    message = message_maker("https://example.com")
    message.ack = AsyncMock()
    message.nack = AsyncMock()

    browser = page_browser_mock()
    context = await browser.new_context()
    page = context.new_page.return_value

    async def timeout_after_requests(*args, **kwargs):
        page.on.call_args_list[0].args[1](MagicMock(url="https://example.com/x"))
        raise PlaywrightTimeoutError("Page.goto: Timeout 120000ms exceeded.")

    page.goto = AsyncMock(side_effect=timeout_after_requests)

    client = MagicMock(spec=AsyncMessageClient)
    with patch.object(
        worker, "publish_umbra_response", AsyncMock(side_effect=RuntimeError("broker"))
    ):
        dropped_pre = failed_pages_total("navigation_timeout")
        await process_page(client, browser, message)

    message.ack.assert_awaited_once()
    assert failed_pages_total("navigation_timeout") == dropped_pre + 1


@pytest.mark.asyncio
async def test_publish_outlinks_is_bounded(monkeypatch):
    """
    Publishing sits outside the page deadline so a slow broker is not charged to
    the page, which means it needs a bound of its own rather than inheriting one.
    """
    monkeypatch.setattr(worker.settings, "outlink_publish_timeout_seconds", 0.1)

    async def hang(*args, **kwargs):
        await asyncio.sleep(60)

    parent = UmbraMessage(json.loads(message_maker("https://example.com").body))
    with patch.object(worker, "publish_umbra_response", AsyncMock(side_effect=hang)):
        published = await asyncio.wait_for(
            worker.publish_outlinks(
                MagicMock(spec=AsyncMessageClient), parent, {"https://example.com/x"}
            ),
            timeout=5,
        )
    assert published is False


def test_navigation_timeout_must_fit_inside_the_page_deadline(monkeypatch):
    """
    An equal or larger navigation timeout can never fire: the page deadline
    starts first and covers the navigation. That would silently reclassify every
    slow page from `navigation_timeout` (dropped) to `page_timeout` (requeued),
    so it has to be rejected at startup rather than discovered in the logs.
    """
    monkeypatch.setenv("penumbra_page_timeout_seconds", "120")

    monkeypatch.setenv("penumbra_navigation_timeout_seconds", "120")
    with pytest.raises(ValidationError, match="must be less than"):
        Settings()

    monkeypatch.setenv("penumbra_navigation_timeout_seconds", "180")
    with pytest.raises(ValidationError, match="must be less than"):
        Settings()

    monkeypatch.setenv("penumbra_navigation_timeout_seconds", "90")
    assert Settings().navigation_timeout_seconds == 90


@pytest.mark.asyncio
async def test_process_page_passes_the_navigation_timeout_to_goto(monkeypatch):
    """Playwright takes milliseconds; the setting is in seconds like its siblings."""
    monkeypatch.setattr(worker.settings, "navigation_timeout_seconds", 90.0)

    message = message_maker("https://example.com")
    message.ack = AsyncMock()
    browser = page_browser_mock()
    context = await browser.new_context()

    client = MagicMock(spec=AsyncMessageClient)
    client.publish_message = AsyncMock()
    await process_page(client, browser, message)

    goto = context.new_page.return_value.goto
    assert goto.await_args.kwargs["timeout"] == 90_000


@pytest.mark.asyncio
async def test_robust_ack_bounds_a_hanging_ack(monkeypatch):
    """
    The failure-path ack runs when the broker may be the thing that is broken, so
    it cannot be trusted to return. A lost ack costs one redelivery.
    """
    monkeypatch.setattr(worker.settings, "amqp_ack_timeout_seconds", 0.1)

    message = message_maker("https://example.com")

    async def hang():
        await asyncio.sleep(60)

    message.ack = AsyncMock(side_effect=hang)

    await asyncio.wait_for(worker.robust_ack(message, "https://example.com"), timeout=5)


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
