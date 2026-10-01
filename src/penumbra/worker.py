import asyncio
import json
import logging
import re
import signal
from contextlib import suppress
from itertools import cycle

from time import monotonic
from urllib.parse import urlsplit, urlunsplit

import aio_pika
from playwright.async_api import Browser, Request, Response, Route, async_playwright
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from prometheus_async.aio import time, track_inprogress

from penumbra import metrics
from penumbra.models import Settings, UmbraMessage, UmbraResponse
from penumbra.queues import AsyncMessageClient

logger = logging.getLogger(__name__)
# Add a global variable to manage shutdown signal
shutdown_event = asyncio.Event()
settings = Settings()


class InstanceStalled(Exception):
    """This instance stopped making progress and will not recover without a restart."""


class SilentBoundedSemaphore(asyncio.BoundedSemaphore):
    """
    Swallow the ValueError `BoundedSemaphore` raises when a release would push the
    counter above the bound, but say so.

    Not an expected event, but better to log than lose a slot in the worker pool
    """

    def release(self):
        try:
            super().release()
        except ValueError:
            logger.error(
                "Semaphore over-released: more releases than acquires, so the "
                "count of free page slots is no longer trustworthy.",
                stack_info=True,
            )


async def publish_with_retry(
    client: AsyncMessageClient, umbra_response: UmbraResponse
) -> bool:
    """
    Publish one `UmbraResponse`, retrying with exponential backoff.

    Never raises: a single unpublishable URL must not cancel its sibling
    publishes via the enclosing `TaskGroup`, nor fail the whole page. Once the
    attempts are exhausted the URL is dropped loudly.
    Returns True if the response was published.
    """
    delay = settings.publish_retry_base_delay_seconds
    for attempt in range(1, settings.publish_max_attempts + 1):
        try:
            await client.publish_message(umbra_response)
            return True
        # CancelledError is a BaseException, so an outer deadline still aborts
        # the retry loop rather than being retried through.
        except Exception as e:
            metrics.penumbra_amqp_publish_exceptions.inc(1)
            if attempt == settings.publish_max_attempts:
                logger.error(
                    "Dropping URL after %d failed publish attempts: %s (parent %s)",
                    attempt,
                    umbra_response.url,
                    umbra_response.parent_url,
                    exc_info=e,
                )
                metrics.penumbra_urls_dropped_publish_failed.inc(1)
                return False
            logger.warning(
                "Publish attempt %d/%d failed for %s, retrying in %.1fs",
                attempt,
                settings.publish_max_attempts,
                umbra_response.url,
                delay,
                exc_info=e,
            )
            metrics.penumbra_amqp_publish_retries.inc(1)
            await asyncio.sleep(delay)
            delay *= 2
    return False


@time(metrics.penumbra_url_publishing_duration_seconds)
async def publish_umbra_response(
    client: AsyncMessageClient, parent_message: UmbraMessage, urls: set[str]
) -> None:
    """
    Return found links to Heritrix (via RabbitMQ) for crawling.
    The number of links found in page processing is *unbounded*.
    There doesn't appear to be a batch publish method for RabbitMQ.
    To save time, `publish_umbra_response` publishes each response asynchronously.
    """
    async with asyncio.TaskGroup() as tg:
        publishes = []
        for url in urls:
            if len(url) > settings.max_url_length:
                logger.info(
                    "Dropping over-length URL (%d chars): %.200s...", len(url), url
                )
                metrics.penumbra_urls_dropped_too_long.inc(1)
                continue
            umbra_response = UmbraResponse(
                url=url,
                method="GET",
                headers={},
                parent_message=parent_message,
            )
            publishes.append(tg.create_task(publish_with_retry(client, umbra_response)))

    if not publishes:
        return

    published = sum(task.result() for task in publishes)
    if published == len(publishes):
        logger.info("Published %d links from %s", published, parent_message.url)
    else:
        logger.warning(
            "Published %d of %d links from %s; the rest were dropped after every "
            "attempt failed",
            published,
            len(publishes),
            parent_message.url,
        )


def canonical_url(url: str) -> str:
    """
    Canonicalise just enough to recognise a page among its own requests.

    Chromium asks for a normalised form of what it was given: a bare host gains
    a trailing slash, the host is lowercased, and the fragment is never sent. So
    comparing raw strings misses the very URL we are trying to match.
    """
    parts = urlsplit(url)
    return urlunsplit(
        (parts.scheme.lower(), parts.netloc.lower(), parts.path or "/", parts.query, "")
    )


def outlinks_from(page_requests: set[str], page_url: str) -> set[str]:
    """
    The links discovered on a page: everything it requested except itself.
    """
    page = canonical_url(page_url)
    return {url for url in page_requests if canonical_url(url) != page}


def update_metrics(urls: set[str]) -> None:
    """
    Record one crawled page and whatever links it found.

    Called for every message we take off the queue and attempt, however it went.
    A site that was offline, served a bad certificate or timed out is still a
    page that was crawled.
    """
    metrics.penumbra_last_page_crawled_time.set_to_current_time()
    metrics.penumbra_pages_crawled.inc(1)
    metrics.penumbra_urls_found.inc(len(urls))


async def watch_broker(client: AsyncMessageClient) -> str | None:
    """
    Give up and exit when the broker goes away and never comes back.

    Returns why it gave up, or None if it was stopped by an ordinary shutdown.

    A broker restart or a brief partition should resolve well inside
    `broker_unhealthy_exit_seconds`, and the cost of a false positive is
    restarting a working instance.

    Page tasks are not watched here. Each one is already bounded by
    `task_timeout_seconds` in `run_page_task`.
    """
    unhealthy_since: float | None = None
    while True:
        # Wake on the interval, or return early if we are shutting down for some
        # other reason -- an operator's SIGTERM must not wait out the interval.
        try:
            async with asyncio.timeout(settings.watchdog_interval_seconds):
                await shutdown_event.wait()
            return None
        except TimeoutError:
            pass

        if client.is_usable():
            if unhealthy_since is not None:
                logger.info(
                    "AMQP topology for queue %s is usable again after %.0fs.",
                    settings.amqp_queue_name,
                    monotonic() - unhealthy_since,
                )
                unhealthy_since = None
            continue

        if unhealthy_since is None:
            unhealthy_since = monotonic()
            logger.warning(
                "AMQP topology for queue %s is not usable. Allowing %.0fs for it "
                "to recover before restarting.",
                settings.amqp_queue_name,
                settings.broker_unhealthy_exit_seconds,
            )
            continue

        unhealthy_for = monotonic() - unhealthy_since
        if unhealthy_for >= settings.broker_unhealthy_exit_seconds:
            logger.error(
                "AMQP topology for queue %s has been unusable for %.0fs and is not "
                "recovering on its own. Exiting so the supervisor restarts us: "
                "staying up would mean consuming nothing while looking healthy.",
                settings.amqp_queue_name,
                unhealthy_for,
            )
            shutdown_event.set()
            return (
                f"AMQP topology for queue {settings.amqp_queue_name} unusable for "
                f"{unhealthy_for:.0f}s"
            )


async def robust_context_close(context) -> None:
    """
    Playwright sometimes throws exceptions if you try to close a closed context,
    and a wedged browser can make `close()` block forever. Bound it: abandoning
    a stuck close leaks one context, but letting it hang would pin this task's
    semaphore permit and in-progress gauge for the life of the process.
    """
    try:
        async with asyncio.timeout(settings.context_close_timeout_seconds):
            await context.close()
    except Exception as e:
        logger.warning("Failed to close browser context", exc_info=e)


# Chromium reports navigation failures as `net::ERR_...` inside the Playwright
# error message; there is no structured field to read it from.
NET_ERROR_PATTERN = re.compile(r"net::(ERR_[A-Z0-9_]+)")

# Playwright failures that carry no `net::` code, matched as lowercase fragments
# of the message because that is the only place Playwright reports them.
PLAYWRIGHT_ERRORS: tuple[tuple[str, str], ...] = (
    # fragment, metric slug
    ("download is starting", "download_started"),
    ("is interrupted by another navigation", "navigation_interrupted"),
    ("frame was detached", "frame_detached"),
    # Covers "Target page, context or browser has been closed" too.
    ("browser has been closed", "target_closed"),
    ("target crashed", "target_crashed"),
    ("connection closed", "connection_closed"),
    ("protocol error", "protocol_error"),
)
UNKNOWN_PLAYWRIGHT_ERROR = "playwright_error"
BROWSER_STUCK_ERROR = "browser_stuck"


def classify_playwright_message(text: str) -> str:
    """Map a Playwright error message to its metric slug, first match wins."""
    lowered = text.lower()
    for fragment, slug in PLAYWRIGHT_ERRORS:
        if fragment in lowered:
            return slug
    return UNKNOWN_PLAYWRIGHT_ERROR


class PageLoadTimeout(Exception):
    """
    The document committed but its subresources never finished.
    """


def failure_reason(e: Exception, deadline_expired: bool) -> str:
    """
    Name a page failure, for `penumbra_pages_failed` and the log.
    """
    # Phase 2: committed, but the subresources never finished. Spending the
    # whole budget on a page is a result, not a failure.
    if isinstance(e, PageLoadTimeout):
        return "page_timeout"
    # Phase 1, checked before PlaywrightError which it subclasses: the server
    # never answered.
    if isinstance(e, PlaywrightTimeoutError):
        return "navigation_timeout"
    if isinstance(e, PlaywrightError):
        match = NET_ERROR_PATTERN.search(str(e))
        if match:
            return match.group(1)
        # Everything else Playwright reports only in the message text.
        return classify_playwright_message(str(e))
    # The outer backstop. Both phases are bounded on their own, so reaching this
    # means a wedged browser, not a slow site.
    #
    # Only the deadline actually expiring counts: the builtin TimeoutError is an
    # OSError subclass that aio-pika also raises from a socket operation.
    if isinstance(e, TimeoutError) and deadline_expired:
        return BROWSER_STUCK_ERROR
    # Nothing recognised it. The exception's class name is the most specific
    # label available, and `log_page_failure` keys the traceback off it.
    return type(e).__name__


async def publish_outlinks(
    client: AsyncMessageClient, message: UmbraMessage, page_requests: set[str]
) -> bool:
    """
    Return a page's links to Heritrix. Returns True if they all went out.

    Never raises: failing to deliver the links must still let the message be
    settled and the slot freed, and `publish_with_retry` has already counted
    whatever it dropped.
    """
    try:
        await publish_umbra_response(client, message, page_requests)
        return True
    except Exception as e:
        logger.error(
            "Failed to publish %d URLs from %s",
            len(page_requests),
            message.url,
            exc_info=e,
        )
        return False


def log_page_failure(
    reason: str, message: UmbraMessage, outlinks: set[str], e: Exception
) -> None:
    """
    Report a page that did not load cleanly, at a level matching how bad it is.
    """
    if reason == BROWSER_STUCK_ERROR:
        logger.warning(
            "Browser deadline expired on %s before the crawl phases could even "
            "run; giving up on it. That is the browser rather than the site.",
            message.url,
        )
    elif reason == type(e).__name__:
        # `failure_reason` fell through to the exception's class name, so
        # nothing recognised this. A traceback is the only thing that will
        # identify it, and an unrecognised failure is rare enough to afford one.
        logger.warning("Exception while processing page: %s", message.url, exc_info=e)
    elif outlinks:
        logger.info(
            "Gave up on %s (%s); keeping the %d links it found and treating the "
            "page as done",
            message.url,
            reason,
            len(outlinks),
        )
    else:
        logger.info("Unreachable page (%s): %s", reason, message.url)


async def robust_ack(raw_message: aio_pika.IncomingMessage, url: str) -> None:
    """
    Settle a message we are done with, so it leaves the queue.

    Every page ends here, whether it loaded, ran out of time or was never
    reachable: a page is only ever attempted once. Bounded and never raising
    because the broker is one of the things that can be broken at this point, so
    the ack cannot be trusted to return and a page task must not be held up by
    it either way. A lost ack costs one redelivery.
    """
    try:
        async with asyncio.timeout(settings.amqp_ack_timeout_seconds):
            await raw_message.ack()
    except Exception as e:
        logger.error("Failed to ack %s", url, exc_info=e)


async def handle_route(route: Route, request: Request) -> None:
    metrics.penumbra_resources_requested.labels(request.resource_type).inc(1)
    if request.resource_type in settings.skip_resource_types:
        await route.abort()
    else:
        await route.continue_()


async def handle_request_finished(request: Request) -> None:
    response: Response = await request.response()
    if response:
        metrics.penumbra_resources_fetched.labels(
            request.resource_type, response.status
        ).inc(1)

        content_length = int(response.headers.get("content-length", 0))
        metrics.penumbra_resources_size_bytes.labels(request.resource_type).inc(
            content_length
        )

        timing = request.timing
        fetch_time = timing["responseEnd"] - timing["requestStart"]
        metrics.penumbra_resources_fetch_time.labels(request.resource_type).inc(
            fetch_time
        )


@time(metrics.penumbra_page_processing_duration_seconds)
@track_inprogress(metrics.penumbra_in_progress_pages)
async def process_page(
    client: AsyncMessageClient,
    browser: Browser,
    raw_message: aio_pika.IncomingMessage,
):
    """
    Crawl one page and return the URLs it requested to Heritrix.

    The crawl runs in two phases, each with its own timeout: navigate until the
    server answers and the document commits, then wait for its subresources to
    finish. Splitting them separates an unreachable site from a site that never
    finishes loading.

    The browser deadline wraps both as a backstop, because `new_context`,
    `new_page` and `route` are untimed Playwright protocol calls that block
    forever against a wedged browser.

    Publishing and settling the message happen afterward on a separate deadline.
    """
    message = UmbraMessage(json.loads(raw_message.body))
    context = None
    # The `request` handler mutates this in place, so whatever the page reached
    # before it stopped is still here afterwards.
    page_requests: set[str] = set()
    reason: str | None = None
    failure_exc: Exception | None = None
    deadline = asyncio.timeout(settings.browser_deadline_seconds)
    try:
        async with deadline:
            context = await browser.new_context(
                accept_downloads=False,
                user_agent=message.metadata.user_agent,
            )
            page = await context.new_page()
            await page.route("**/*", handle_route)
            page.on("request", lambda request: page_requests.add(request.url))
            page.on("requestfinished", handle_request_finished)
            # Phase 1. `commit` returns as soon as the server has answered and
            # the navigation is committed.
            await page.goto(
                message.url,
                wait_until="commit",
                timeout=settings.navigation_timeout_seconds * 1000,
            )
            # Phase 2, on its own budget. Re-raised as `PageLoadTimeout` so it
            # is not mistaken for the navigation timing out: Playwright raises
            # the same exception type for both.
            try:
                await page.wait_for_load_state(
                    "load", timeout=settings.page_timeout_seconds * 1000
                )
            except PlaywrightTimeoutError as e:
                raise PageLoadTimeout(message.url) from e
    except Exception as e:
        reason = failure_reason(e, deadline.expired())
        failure_exc = e
    finally:
        # Closed before publishing, so a browser context is not held open across
        # what can be a slow AMQP round trip.
        if context is not None:
            await robust_context_close(context)

    outlinks = outlinks_from(page_requests, message.url)

    if reason is not None:
        metrics.penumbra_pages_failed.labels(reason).inc(1)
        log_page_failure(reason, message, outlinks, failure_exc)

    if outlinks:
        await publish_outlinks(client, message, outlinks)

    await robust_ack(raw_message, message.url)

    update_metrics(outlinks)


# Returned by `or_shutdown` when the shutdown signal won the race. A sentinel
# rather than None, because `semaphore.acquire()` legitimately returns None.
SHUTDOWN = object()


def _swallow(task: asyncio.Future) -> None:
    """Read a finished task's outcome, so asyncio does not report it unretrieved."""
    if not task.cancelled():
        task.exception()


def _abandon(task: asyncio.Future) -> None:
    """
    Let go of a task whose outcome no longer matters, quietly.

    Cancelled if it is still running, read if it already finished. An unread
    exception on a discarded task resurfaces later as "Task exception was never
    retrieved", which reads like a fault and is not one.
    """
    if task.done():
        _swallow(task)
        return
    task.cancel()
    task.add_done_callback(_swallow)


def report_watchdog_death(task: asyncio.Task) -> None:
    """
    Bring the process down if the watchdog itself dies.

    Setting the event ends the loop, and `main` re-raises the exception from
    `result()` on the way out.
    """
    if task.cancelled():
        return
    error = task.exception()
    if error is None:
        return
    logger.error(
        "The watchdog died, so nothing is watching this instance for stalls any "
        "more. Shutting down: consuming without it is the failure mode it was "
        "added to catch.",
        exc_info=error,
    )
    shutdown_event.set()


async def or_shutdown(awaitable):
    """
    Await `awaitable`, or return `SHUTDOWN` if a shutdown signal arrives first.

    Allows us to respond to a shutdown signal while waiting for something that may be stalled
    """
    task = asyncio.ensure_future(awaitable)
    waiter = asyncio.ensure_future(shutdown_event.wait())
    try:
        await asyncio.wait((task, waiter), return_when=asyncio.FIRST_COMPLETED)
        # The event is asked directly rather than `waiter.done()`, which is also
        # true when the wait itself failed -- and would then report a shutdown
        # nobody requested.
        if shutdown_event.is_set():
            return SHUTDOWN
        if task.done():
            return task.result()
        # Only reachable if the waiter finished without the event being set. Wait
        # out the real work rather than inventing a shutdown.
        return await task
    finally:
        _abandon(waiter)
        _abandon(task)


def ensure_playwright_installed():
    """Installs playwright's browser and all dependencies (if needed) at runtime."""
    import subprocess

    subprocess.check_call(["playwright", "install", "--with-deps", "chromium"])


async def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    logger.setLevel(logging.DEBUG)
    logging.getLogger("aiormq").setLevel(logging.WARNING)

    # Setup metrics
    if settings.metrics_enabled:
        metrics.register_prom_metrics(settings.metrics_port)

    # Setup RabbitMQ client. Prefetch is capped at the number of page tasks we
    # can actually run, so undelivered work stays queued in the broker where it
    # can be inspected and purged, rather than buffered in this process.
    max_concurrency = settings.max_concurrency
    client = AsyncMessageClient(
        amqp_url=settings.amqp_url,
        queue_name=settings.amqp_queue_name,
        routing_key=settings.amqp_routing_key,
        exchange_name=settings.amqp_exchange_name,
        prefetch_count=max_concurrency,
        publish_timeout=settings.publish_timeout_seconds,
        connect_timeout=settings.amqp_connect_timeout_seconds,
        recovery_timeout=settings.amqp_recovery_timeout_seconds,
    )

    # Setup Playwright
    if settings.install_playwright:
        ensure_playwright_installed()
    browser_pool = []
    for i in range(settings.browser_pool_size):
        pw = await async_playwright().start()
        browser = await pw.chromium.launch()
        browser_pool.append({"playwright": pw, "browser": browser})

    # Divide page crawl tasks round-robin to browsers
    browser_pool = cycle(browser_pool)

    # Setup semaphore for concurrent browser tasks.
    semaphore = SilentBoundedSemaphore(max_concurrency)

    # The page tasks currently in flight, which is what the shutdown drain waits
    # on. Membership is all anyone needs: each task carries its own deadline, so
    # nothing has to age them from outside.
    page_tasks: set[asyncio.Task] = set()

    async def run_page_task(browser: Browser, raw_message: aio_pika.IncomingMessage):
        """
        Own a semaphore permit for the lifetime of one page task and return it in
        a `finally`, so the permit comes back even when the task is cancelled or
        raises.

        The `wait_for` is a backstop around the deadlines inside `process_page`:
        if any await there ever escapes its own timeout, this still frees the
        permit instead of leaking it.
        """
        try:
            await asyncio.wait_for(
                process_page(client, browser, raw_message),
                timeout=settings.task_timeout_seconds,
            )
        except TimeoutError:
            logger.error(
                "Page task exceeded its %ss backstop deadline and was abandoned; "
                "an inner timeout failed to fire",
                settings.task_timeout_seconds,
            )
            metrics.penumbra_page_task_deadline_exceeded.inc(1)
        except Exception as e:
            logger.error("Unhandled exception in page task", exc_info=e)
        finally:
            semaphore.release()

    def done_callback(task: asyncio.Task):
        """`done_callback` is called when page tasks complete."""
        page_tasks.discard(task)

    def shutdown_signal_handler():
        logger.info("Shutdown signal received. Shutting down gracefully...")

        shutdown_event.set()

    loop = asyncio.get_running_loop()
    for signal_number in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signal_number, shutdown_signal_handler)

    watchdog_task = asyncio.create_task(watch_broker(client))
    watchdog_task.add_done_callback(report_watchdog_death)

    # Main worker loop
    try:
        async with client.iterator() as messages:
            while True:
                # Wait for free slot, or shutdown
                if await or_shutdown(semaphore.acquire()) is SHUTDOWN:
                    logger.info("Shutdown requested; stopping consumption.")
                    break

                try:
                    raw_message = await or_shutdown(
                        anext(messages)
                    )  # message, OR shutdown
                except StopAsyncIteration:
                    semaphore.release()
                    logger.info("Queue iterator closed; stopping consumption.")
                    break

                if raw_message is SHUTDOWN:
                    semaphore.release()
                    logger.info("Shutdown requested; stopping consumption.")
                    break

                browser = next(browser_pool)
                logger.info("Got message from queue")
                task = asyncio.create_task(
                    run_page_task(browser["browser"], raw_message)
                )
                task.add_done_callback(done_callback)
                page_tasks.add(task)

        # Wait for in-progress tasks before shutdown, but only for so long.
        draining = set(page_tasks)
        if draining:
            logger.info(
                "Waiting up to %ss for %d in-progress page task(s) to complete...",
                settings.shutdown_drain_timeout_seconds,
                len(draining),
            )
            _, pending = await asyncio.wait(
                draining, timeout=settings.shutdown_drain_timeout_seconds
            )
            if pending:
                logger.warning(
                    "Abandoning %d page task(s) still running after %ss; their "
                    "messages were never acked and will be redelivered.",
                    len(pending),
                    settings.shutdown_drain_timeout_seconds,
                )
                for task in pending:
                    _abandon(task)

        # Raised here rather than from the watchdog itself, so the in-flight pages
        # still get their chance to finish and ack first.
        if watchdog_task.done() and not watchdog_task.cancelled():
            if reason := watchdog_task.result():
                raise InstanceStalled(reason)

    except asyncio.CancelledError:
        pass
    # Either the initial connect never reached a usable broker, or the broker
    # closed the consume channel. Exit non-zero.
    except (TimeoutError, aio_pika.exceptions.AMQPError) as e:
        logger.error(
            "AMQP failure on queue %s, so this instance is not consuming. "
            "Exiting so we are restarted rather than sitting here idle.",
            settings.amqp_queue_name,
            exc_info=e,
        )
        raise
    finally:
        watchdog_task.cancel()
        # Awaited, not just cancelled, so the loop is not torn down with it still
        # pending -- which asyncio reports as a "Task was destroyed" error.
        with suppress(asyncio.CancelledError):
            await watchdog_task

        for _ in range(settings.browser_pool_size):
            browser = next(browser_pool)
            await browser["browser"].close()
            await browser["playwright"].stop()
        await client.close_connection()


def run() -> object:
    if settings.event_loop == "asyncio":
        asyncio.run(main())
    elif settings.event_loop == "uvloop":
        import uvloop

        uvloop.run(main())


if __name__ == "__main__":
    run()
