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

from penumbra import worker
from penumbra.models import UmbraMessage, UmbraResponse
from penumbra.queues import AsyncMessageClient
from penumbra.worker import (
    PageLoadTimeout,
    Settings,
    process_page,
    publish_umbra_response,
)


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


@pytest.fixture
def page_retries_enabled(monkeypatch):
    """
    Retries are off by default, so the requeue paths are unreachable without
    this. Kept as a fixture so every test that needs it says so in its signature.
    """
    monkeypatch.setattr(worker.settings, "enable_page_retries", True)


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

            # Whatever a real browser reports, the page's own URL is never handed
            # back to Heritrix -- asserted against the normalised form Chromium
            # actually requests, which is what the raw test_url is not.
            published = [
                call.args[0].url for call in mock_publish_message.await_args_list
            ]
            assert test_url not in published
            assert "https://example.com/" not in published
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
async def test_browser_deadline_catches_a_hang_in_the_untimed_setup_calls(
    monkeypatch, caplog
):
    """
    Both crawl phases carry their own timeout, so the outer deadline can only
    fire on the untimed protocol calls -- a wedged browser. Terminal, with
    nothing published and nothing retried, so the log line and the counter are
    the only signs the pool has gone bad.

    The context is closed either way, so the task returns and its semaphore
    permit and in-progress gauge are released.
    """
    # Derived and cached, so its inputs cannot shrink it below the fixed margin.
    # `cached_property` reads instance __dict__, so seed it directly.
    monkeypatch.setitem(worker.settings.__dict__, "browser_deadline_seconds", 0.1)

    message = message_maker("https://example.com")
    message.ack = AsyncMock()
    message.nack = AsyncMock()

    async def hang(*args, **kwargs):
        await asyncio.sleep(60)

    browser = MagicMock()
    # Hang on the first untimed Playwright call inside the deadline.
    browser.new_context = AsyncMock(side_effect=hang)

    in_progress_pre = REGISTRY.get_sample_value("penumbra_in_progress_pages") or 0
    stuck_pre = failed_pages_total("browser_stuck")

    client = MagicMock(spec=AsyncMessageClient)
    with caplog.at_level(logging.WARNING, logger="penumbra.worker"):
        await asyncio.wait_for(process_page(client, browser, message), timeout=10)

    message.ack.assert_awaited_once()
    message.nack.assert_not_awaited()
    # Nothing was published: there was nothing to publish.
    client.publish_message.assert_not_called()
    assert failed_pages_total("browser_stuck") == stuck_pre + 1
    # The loss is not silent -- it is the only signal that the browser is bad.
    assert "That is the browser rather than the site" in caplog.text
    # The gauge is back where it started rather than stuck above it.
    assert REGISTRY.get_sample_value("penumbra_in_progress_pages") == in_progress_pre


def page_browser_mock() -> MagicMock:
    """A browser whose context/page calls all succeed instantly."""
    page = MagicMock()
    page.route = AsyncMock()
    page.goto = AsyncMock()
    page.wait_for_load_state = AsyncMock()
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
async def test_process_page_still_nacks_when_the_page_fails_before_the_ack(
    page_retries_enabled,
):
    """An unrecognised failure is retryable, so with retries on it is requeued."""
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
async def test_process_page_does_not_count_an_unrelated_timeout_as_a_page_timeout(
    page_retries_enabled,
):
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
async def test_process_page_requeues_a_net_error_that_is_our_end_of_the_wire(
    page_retries_enabled,
):
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
async def test_process_page_requeues_only_browser_side_playwright_errors(
    page_retries_enabled,
):
    """
    Playwright reports these only in the message text, and requeueing is opt-in:
    a dead browser is worth another attempt, anything else is not.
    """
    message = await run_failing_page(
        PlaywrightError("Target page, context or browser has been closed")
    )
    message.nack.assert_awaited_once_with(requeue=True)
    message.ack.assert_not_awaited()
    assert failed_pages_total("target_closed") > 0


@pytest.mark.asyncio
async def test_nothing_is_requeued_by_default(caplog):
    """
    Retries are off unless asked for, so out of the box every message is settled
    exactly once -- even the failures a retry could plausibly get past. The page
    is still counted under its own reason and reported, not silently dropped.
    """
    assert worker.settings.enable_page_retries is False

    closed_pre = failed_pages_total("target_closed")

    with caplog.at_level(logging.WARNING, logger="penumbra.worker"):
        message = await run_failing_page(
            PlaywrightError("Target page, context or browser has been closed")
        )

    message.ack.assert_awaited_once()
    message.nack.assert_not_awaited()
    assert "Exception while processing page" in caplog.text
    # Still visible as the browser-side failure it is, not relabelled.
    assert failed_pages_total("target_closed") == closed_pre + 1


def test_retryable_failures_stay_classified_when_retries_are_off():
    """
    The switch is policy, not classification: `retryable` keeps saying what the
    failure is, so turning retries on later needs no reclassification and the
    metric reason is the same either way.
    """
    assert worker.classify_page_failure(PlaywrightError("Target crashed"), False) == (
        worker.PageFailure("target_crashed", retryable=True, publish=False)
    )


@pytest.mark.asyncio
async def test_process_page_drops_a_url_that_serves_a_download():
    """
    `Page.goto: Download is starting` carries no `net::` code, so the old
    catch-all called it a browser problem and requeued it -- a hot loop, because
    a URL with Content-Disposition: attachment does this on every redelivery.
    There is nothing to extract from the bytes either way.
    """
    dropped_pre = failed_pages_total("download_started")

    message = await run_failing_page(PlaywrightError("Page.goto: Download is starting"))

    message.ack.assert_awaited_once()
    message.nack.assert_not_awaited()
    assert failed_pages_total("download_started") == dropped_pre + 1


@pytest.mark.asyncio
async def test_process_page_does_not_requeue_an_unrecognised_playwright_error():
    """
    Unknown is not retryable. Guessing "retry" is what put the cert errors into a
    hot loop, and the costs are lopsided: a wrong ack loses one page's links,
    which Heritrix crawls itself anyway, while a wrong requeue starves the queue.
    """
    dropped_pre = failed_pages_total("playwright_error")

    message = await run_failing_page(PlaywrightError("Page.goto: something novel"))

    message.ack.assert_awaited_once()
    message.nack.assert_not_awaited()
    assert failed_pages_total("playwright_error") == dropped_pre + 1


def test_outlinks_exclude_the_page_itself():
    """
    Heritrix gave us this URL, so returning it is a no-op its frontier has to
    dedupe away. Chromium asks for a normalised form of what it was given, so
    the match cannot be a raw string comparison.
    """
    page = "https://EXAMPLE.com/a?q=1#frag"
    requests = {
        # The same page, in the shape Chromium actually requested it.
        "https://example.com/a?q=1",
        # Genuine discoveries, including a redirect target and a path that
        # differs only by case -- paths are case-sensitive, so it stays.
        "https://example.com/a?q=2",
        "https://example.com/A?q=1",
        "https://cdn.example.com/s.css",
    }
    assert worker.outlinks_from(requests, page) == {
        "https://example.com/a?q=2",
        "https://example.com/A?q=1",
        "https://cdn.example.com/s.css",
    }

    # A bare host gains a trailing slash on the wire.
    assert (
        worker.outlinks_from({"https://example.com/"}, "https://example.com") == set()
    )


@pytest.mark.asyncio
async def test_process_page_publishes_nothing_for_a_page_it_only_requested():
    """
    An unreachable server still fires the document request, so without the self
    filter every failed navigation published one useless URL back to Heritrix.
    """
    browser = page_browser_mock()
    context = await browser.new_context()
    page = context.new_page.return_value

    async def fail_after_requesting(*args, **kwargs):
        page.on.call_args_list[0].args[1](MagicMock(url="https://example.com/"))
        raise PlaywrightTimeoutError("Page.goto: Timeout 30000ms exceeded.")

    page.goto = AsyncMock(side_effect=fail_after_requesting)

    message = message_maker("https://example.com/")
    message.ack = AsyncMock()
    client = MagicMock(spec=AsyncMessageClient)
    client.publish_message = AsyncMock()
    crawled_pre = REGISTRY.get_sample_value("penumbra_pages_crawled_total") or 0

    await process_page(client, browser, message)

    client.publish_message.assert_not_called()
    message.ack.assert_awaited_once()
    # Still a crawl: the URL was attempted, and "the server was unreachable" is
    # the state of that site at archive time. The reason is in pages_failed.
    assert REGISTRY.get_sample_value("penumbra_pages_crawled_total") == crawled_pre + 1


@pytest.mark.asyncio
async def test_a_dead_end_page_still_counts_as_crawled():
    """
    A document with no outgoing links is a page we crawled. Skipping it would
    undercount real work purely because the site was a dead end -- and after the
    self-filter, every link-free page looks like this.
    """
    browser = page_browser_mock()
    context = await browser.new_context()
    page = context.new_page.return_value

    async def requests_only_itself(*args, **kwargs):
        page.on.call_args_list[0].args[1](MagicMock(url="https://example.com/"))

    page.goto = AsyncMock(side_effect=requests_only_itself)

    message = message_maker("https://example.com/")
    message.ack = AsyncMock()
    client = MagicMock(spec=AsyncMessageClient)
    client.publish_message = AsyncMock()

    crawled_pre = REGISTRY.get_sample_value("penumbra_pages_crawled_total") or 0
    found_pre = REGISTRY.get_sample_value("penumbra_urls_found_total") or 0

    await process_page(client, browser, message)

    # Nothing to publish, but the page is counted and no links are invented.
    client.publish_message.assert_not_called()
    assert REGISTRY.get_sample_value("penumbra_pages_crawled_total") == crawled_pre + 1
    assert REGISTRY.get_sample_value("penumbra_urls_found_total") == found_pre


@pytest.mark.asyncio
async def test_every_finished_page_task_counts_and_refreshes_the_stall_clock(
    monkeypatch,
):
    """
    `warn_if_stalled` reads `last_completion`, so what refreshes it decides which
    instances look stalled. A failed page is still a finished task, so it counts:
    the warning is for an instance that has stopped getting through work at all,
    not one working through a bad run of URLs.
    """
    client = MagicMock(spec=AsyncMessageClient)
    client.publish_message = AsyncMock()

    # A URL we could not reach.
    monkeypatch.setattr(worker, "last_completion", 1.0)
    crawled_pre = REGISTRY.get_sample_value("penumbra_pages_crawled_total") or 0
    browser = page_browser_mock()
    browser.new_context.return_value.new_page.return_value.goto = AsyncMock(
        side_effect=PlaywrightTimeoutError("Page.goto: Timeout")
    )
    unreachable = message_maker("https://example.com")
    unreachable.ack = AsyncMock()
    await process_page(client, browser, unreachable)
    assert worker.last_completion != 1.0

    # A browser that hung before the crawl phases could run. Still a finished
    # task -- the deadline fired, the slot came back -- so it counts too. A
    # genuine stall is tasks that never finish, which is what leaves the clock
    # untouched.
    monkeypatch.setitem(worker.settings.__dict__, "browser_deadline_seconds", 0.1)
    monkeypatch.setattr(worker, "last_completion", 1.0)

    async def hang(*args, **kwargs):
        await asyncio.sleep(60)

    stuck_browser = MagicMock()
    stuck_browser.new_context = AsyncMock(side_effect=hang)
    stuck = message_maker("https://example.com")
    stuck.ack = AsyncMock()
    await asyncio.wait_for(process_page(client, stuck_browser, stuck), timeout=5)
    assert worker.last_completion != 1.0

    assert REGISTRY.get_sample_value("penumbra_pages_crawled_total") == crawled_pre + 2


def test_playwright_error_slugs_stay_bounded():
    """
    The slug is a metric label. Playwright messages embed URLs, so mapping them
    through a table is what keeps the label's cardinality finite.
    """
    noisy = (
        'Page.goto: Navigation to "https://example.com/a?x=1" is interrupted by '
        'another navigation to "https://example.com/b?y=2"'
    )
    assert worker.classify_playwright_message(noisy) == (
        "navigation_interrupted",
        False,
    )
    assert worker.classify_playwright_message("Page.goto: Download is starting") == (
        "download_started",
        False,
    )
    # Case-insensitive, and the "Target page, context or browser has been
    # closed" wording is covered by the same fragment.
    assert worker.classify_playwright_message("TARGET CRASHED") == (
        "target_crashed",
        True,
    )
    assert worker.classify_playwright_message("anything else") == (
        "playwright_error",
        False,
    )


@pytest.mark.asyncio
async def test_process_page_refuses_downloads():
    """
    Accepting a download writes roughly twice the file size into the browser's
    temp dir until the context closes. TMPDIR is /dev/shm in production, so that
    is RAM, times `browser_pool_size` concurrent pages -- for bytes we cannot
    extract links from.
    """
    message = message_maker("https://example.com")
    message.ack = AsyncMock()
    browser = page_browser_mock()

    client = MagicMock(spec=AsyncMessageClient)
    client.publish_message = AsyncMock()
    await process_page(client, browser, message)

    assert browser.new_context.await_args.kwargs["accept_downloads"] is False


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
        "navigation_timeout", retryable=False, publish=True
    )

    # Phase 2 raises the same Playwright type, so it is re-raised as
    # PageLoadTimeout to stay distinguishable. Checked first for that reason.
    load = worker.classify_page_failure(PageLoadTimeout("https://example.com"), False)
    assert load == worker.PageFailure("page_timeout", retryable=False, publish=True)

    # The outer backstop, which now means a wedged browser rather than a slow
    # page: both phases are bounded on their own.
    stuck = worker.classify_page_failure(TimeoutError("deadline"), True)
    assert stuck == worker.PageFailure("browser_stuck", retryable=False, publish=True)

    # An unrelated socket timeout, with our deadline still unexpired. Nothing
    # here says anything about the page, so it is retried and nothing published.
    assert worker.classify_page_failure(TimeoutError("socket"), False) == (
        worker.PageFailure("TimeoutError", retryable=True, publish=False)
    )


@pytest.mark.asyncio
async def test_process_page_publishes_the_links_a_slow_page_reached():
    """
    The document committed but its subresources never finished. Everything it
    did fetch is a real link, so it is published and the page acked as done
    rather than discarded and crawled again.
    """
    message = message_maker("https://example.com")
    message.ack = AsyncMock()
    message.nack = AsyncMock()

    # The document request comes first, as it does on a real page, and is the
    # page's own URL -- so it is dropped rather than handed back to Heritrix.
    requested = [
        "https://example.com/",
        "https://example.com/a.css",
        "https://example.com/b.js",
    ]
    expected = ["https://example.com/a.css", "https://example.com/b.js"]

    browser = page_browser_mock()
    context = await browser.new_context()
    page = context.new_page.return_value

    async def commit_then_never_load(*args, **kwargs):
        # The requests the real handler would see between commit and the load
        # event that never arrives.
        for url in requested:
            page.on.call_args_list[0].args[1](MagicMock(url=url))
        raise PlaywrightTimeoutError("Timeout 120000ms exceeded.")

    page.wait_for_load_state = AsyncMock(side_effect=commit_then_never_load)

    client = MagicMock(spec=AsyncMessageClient)
    client.publish_message = AsyncMock()

    failed_pre = failed_pages_total("page_timeout")
    timeouts_pre = REGISTRY.get_sample_value("penumbra_page_timeouts_total") or 0
    crawled_pre = REGISTRY.get_sample_value("penumbra_pages_crawled_total") or 0

    await asyncio.wait_for(process_page(client, browser, message), timeout=5)

    assert (
        sorted(call.args[0].url for call in client.publish_message.await_args_list)
        == expected
    )
    message.ack.assert_awaited_once()
    message.nack.assert_not_awaited()
    assert failed_pages_total("page_timeout") == failed_pre + 1
    # The counter now means "pages too slow to finish loading", not "wedged".
    assert REGISTRY.get_sample_value("penumbra_page_timeouts_total") == timeouts_pre + 1
    # Counted as a completion, so `warn_if_stalled` sees progress.
    assert REGISTRY.get_sample_value("penumbra_pages_crawled_total") == crawled_pre + 1


@pytest.mark.asyncio
async def test_process_page_separates_the_two_crawl_phases():
    """
    Playwright raises the same TimeoutError for both phases. A navigation that
    never commits and a page that never finishes loading are different problems
    -- unreachable server versus slow render -- so they must not share a reason.
    """
    browser = page_browser_mock()
    context = await browser.new_context()
    page = context.new_page.return_value
    page.goto = AsyncMock(side_effect=PlaywrightTimeoutError("Page.goto: Timeout"))

    nav_pre = failed_pages_total("navigation_timeout")
    load_pre = failed_pages_total("page_timeout")

    message = message_maker("https://example.com")
    message.ack = AsyncMock()
    client = MagicMock(spec=AsyncMessageClient)
    client.publish_message = AsyncMock()
    await process_page(client, browser, message)

    assert failed_pages_total("navigation_timeout") == nav_pre + 1
    # The load phase never ran, so it is not blamed for the navigation.
    assert failed_pages_total("page_timeout") == load_pre


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


@pytest.mark.asyncio
async def test_each_crawl_phase_gets_its_own_timeout(monkeypatch):
    """
    Playwright takes milliseconds; the settings are in seconds like their
    siblings. `commit` is what makes the split work -- with the default `load`,
    `goto` would wait out the whole page load on the navigation budget and the
    second phase would have nothing left to do.
    """
    monkeypatch.setattr(worker.settings, "navigation_timeout_seconds", 20.0)
    monkeypatch.setattr(worker.settings, "page_timeout_seconds", 90.0)

    message = message_maker("https://example.com")
    message.ack = AsyncMock()
    browser = page_browser_mock()
    context = await browser.new_context()

    client = MagicMock(spec=AsyncMessageClient)
    client.publish_message = AsyncMock()
    await process_page(client, browser, message)

    page = context.new_page.return_value
    assert page.goto.await_args.kwargs["wait_until"] == "commit"
    assert page.goto.await_args.kwargs["timeout"] == 20_000
    assert page.wait_for_load_state.await_args.args[0] == "load"
    assert page.wait_for_load_state.await_args.kwargs["timeout"] == 90_000


def test_browser_deadline_contains_both_phases(monkeypatch):
    """
    Derived, not configured: the outer backstop cannot be set below the phases it
    wraps, which is how the two page timeouts used to be able to cancel out.
    """
    monkeypatch.setenv("penumbra_navigation_timeout_seconds", "30")
    monkeypatch.setenv("penumbra_page_timeout_seconds", "120")

    settings = Settings()
    assert settings.browser_deadline_seconds > (30 + 120)
    assert settings.task_timeout_seconds > settings.browser_deadline_seconds


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
