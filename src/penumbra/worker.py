import asyncio
import json
import logging
import signal
from contextlib import suppress
from itertools import cycle

# `time` below is prometheus_async's decorator, not the stdlib module, so import
# the clock we need by name.
from time import monotonic

import aio_pika
from playwright.async_api import Browser, Request, Response, Route, async_playwright
from prometheus_async.aio import time, track_inprogress

from penumbra import metrics
from penumbra.models import Settings, UmbraMessage, UmbraResponse
from penumbra.queues import AsyncMessageClient

logger = logging.getLogger(__name__)
# Add a global variable to manage shutdown signal
shutdown_event = asyncio.Event()
settings = Settings()


# Monotonic timestamp of the last completed page, so a clock step cannot invent
# or hide a stall. Seeded at startup rather than left unset until the first page
# completes: an instance that fills every slot and wedges before finishing a
# single page is the stall most worth hearing about, and there would be nothing
# to measure it from.
last_completion: float = monotonic()


class SilentBoundedSemaphore(asyncio.BoundedSemaphore):
    """
    Swallow the ValueError thrown by BoundedSemaphore when a call to release would
    push the internal counter above the bound value.

    For Penumbra, the occasional bounded "over-release" is preferable to running out of
    slots in our Semaphore for fear of releasing to defend against an edge case. If we
    over-release there could temporarily be too many concurrent crawl tasks but should
    return to equilibrium at the bound value.
    """

    def release(self):
        try:
            super().release()
        except ValueError:
            pass


async def publish_with_retry(
    client: AsyncMessageClient, umbra_response: UmbraResponse
) -> bool:
    """
    Publish one `UmbraResponse`, retrying with exponential backoff.

    Never raises: a single unpublishable URL must not cancel its sibling
    publishes via the enclosing `TaskGroup`, nor fail the whole page. Once the
    attempts are exhausted the URL is dropped loudly -- that link is lost, so it
    is an error, not a warning. Returns True if the response was published.
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
        for url in urls:
            if len(url) > settings.max_url_length:
                # Truncated: these are over-length by definition, and a page full
                # of them would otherwise dominate the log.
                logger.info(
                    "Dropping over-length URL (%d chars): %.200s...", len(url), url
                )
                metrics.penumbra_urls_dropped_too_long.inc(1)
                continue
            logger.info("Publishing URL %s", url)
            umbra_response = UmbraResponse(
                url=url,
                method="GET",
                headers={},
                parent_message=parent_message,
            )
            tg.create_task(publish_with_retry(client, umbra_response))


def update_metrics(urls: set[str]) -> None:
    """Update `process_page` Prometheus metrics."""
    global last_completion
    metrics.penumbra_last_page_crawled_time.set_to_current_time()
    metrics.penumbra_pages_crawled.inc(1)
    metrics.penumbra_urls_found.inc(len(urls))
    last_completion = monotonic()


async def warn_if_stalled(tasks: set[asyncio.Task], max_concurrency: int) -> None:
    """
    Periodically warn when page tasks are in flight but none are completing.

    A separate task rather than a check in the main loop on purpose: a stalled
    instance is parked on `semaphore.acquire()` and never iterates the loop
    again, so a check there would never run in the case it exists to catch.
    """
    while True:
        try:
            async with asyncio.timeout(settings.stall_check_interval_seconds):
                await shutdown_event.wait()
            return
        except TimeoutError:
            pass

        # No tasks in flight means an idle cluster, which is not a stall.
        if not tasks:
            continue

        idle_for = monotonic() - last_completion
        if idle_for > settings.stall_warning_seconds:
            logger.warning(
                "%d/%d page slots in use but no page has completed in %.0fs. "
                "This instance may have stalled and stopped consuming from %s; "
                "a restart will clear it.",
                len(tasks),
                max_concurrency,
                idle_for,
                settings.amqp_queue_name,
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


async def robust_nack(raw_message: aio_pika.IncomingMessage, url: str) -> None:
    """
    Return a message to the queue for redelivery.

    Only safe to call when no ack has been attempted for this delivery tag; see
    `ack_attempted` in `process_page`.

    Bounded separately from the page deadline: a blocked AMQP connection is one
    of the reasons we end up here, so the nack itself cannot be trusted to
    return. Losing the nack costs a redelivery, which the broker will do anyway
    once the consumer goes away.
    """
    try:
        async with asyncio.timeout(settings.amqp_ack_timeout_seconds):
            await raw_message.nack(requeue=True)
    except Exception as e:
        logger.error("Failed to nack message for %s", url, exc_info=e)


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
    `process_page` interacts with a page in a browser and publishes any URLs it finds
    back to Heritrix for potential crawling.

    The whole body runs under a deadline. Only `page.goto()` has a timeout of its
    own; `new_context`, `new_page` and `route` are untimed Playwright protocol
    calls, and the publish/ack are untimed AMQP round trips. Any of them can
    block forever against a wedged browser or a blocked broker connection, which
    is how a task -- and one of only
    `browser_pool_size * contexts_per_browser` semaphore permits -- gets stuck
    for the life of the process.
    """
    message = UmbraMessage(json.loads(raw_message.body))
    context = None
    # Set before the ack, not after: `ack()` hands the Basic.Ack frame to the
    # socket and only marks the message processed once the drain returns, so a
    # deadline landing on that drain leaves `raw_message.processed` False with the
    # ack already on the wire. Nacking that delivery tag earns a
    # PRECONDITION_FAILED that closes the consume channel and stops this instance
    # consuming at all, so once this is set the nack has to be skipped.
    ack_attempted = False
    deadline = asyncio.timeout(settings.page_timeout_seconds)
    try:
        async with deadline:
            context = await browser.new_context()
            page_requests = set()
            page = await context.new_page()
            await page.route("**/*", handle_route)
            page.on("request", lambda request: page_requests.add(request.url))
            page.on("requestfinished", handle_request_finished)
            await page.goto(message.url)
            await publish_umbra_response(client, message, page_requests)
            ack_attempted = True
            await raw_message.ack()
            update_metrics(page_requests)
    except Exception as e:
        # Playwright's TimeoutError is its own class, but the builtin one that
        # `asyncio.timeout` raises is an OSError subclass that aio-pika can also
        # raise from a socket operation. `deadline.expired()` is what actually
        # distinguishes our deadline from an unrelated timeout, so a slow socket
        # is not miscounted as a page that outran its budget.
        if isinstance(e, TimeoutError) and deadline.expired():
            logger.error(
                "Timed out after %ss while processing page: %s",
                settings.page_timeout_seconds,
                message.url,
            )
            metrics.penumbra_page_timeouts.inc(1)
        else:
            logger.warning(
                "Exception while processing page: %s", message.url, exc_info=e
            )
        if ack_attempted:
            logger.warning(
                "Not nacking %s: its ack may already be on the wire. The broker "
                "will redeliver if it did not land.",
                message.url,
            )
        else:
            await robust_nack(raw_message, message.url)
    finally:
        if context is not None:
            await robust_context_close(context)


def ensure_playwright_installed():
    """Installs playwright's browser and all dependencies (if needed) at runtime."""
    import subprocess

    subprocess.check_call(["playwright", "install", "--with-deps", "chromium"])


async def main():
    # Setup logging
    logger.setLevel(logging.DEBUG)
    ch = logging.StreamHandler()
    ch.setLevel(logging.DEBUG)
    formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )
    ch.setFormatter(formatter)
    logger.addHandler(ch)

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

    tasks = set()

    async def run_page_task(browser: Browser, raw_message: aio_pika.IncomingMessage):
        """
        Own a semaphore permit for the lifetime of one page task and return it in
        a `finally`, so the permit comes back even when the task is cancelled or
        raises.

        The `wait_for` is a backstop around the deadlines inside `process_page`:
        if any await there ever escapes its own timeout, this still frees the
        permit instead of leaking it. Leaking `max_concurrency` permits stops
        this instance consuming entirely, without exiting -- so systemd sees a
        healthy unit and `Restart=always` never fires.
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
        tasks.discard(task)

    # Function to handle shutdown signals
    def shutdown_signal_handler():
        logger.info("Shutdown signal received. Shutting down gracefully...")

        shutdown_event.set()

    # Register the signal handlers
    signal.signal(signal.SIGTERM, lambda s, f: shutdown_signal_handler())
    signal.signal(signal.SIGINT, lambda s, f: shutdown_signal_handler())

    stall_warning_task = asyncio.create_task(warn_if_stalled(tasks, max_concurrency))

    # Main worker loop
    try:
        async with client.iterator() as messages:
            async for raw_message in messages:
                if shutdown_event.is_set():
                    break
                await semaphore.acquire()
                browser = next(browser_pool)
                logger.info("Got message from queue")
                task = asyncio.create_task(
                    run_page_task(browser["browser"], raw_message)
                )
                task.add_done_callback(done_callback)
                tasks.add(task)

        # Wait for in-progress tasks to complete before shutdown
        logger.info("Waiting for in-progress tasks to complete...")
        await asyncio.gather(*tasks)

    except asyncio.CancelledError:
        pass
    # Startup could not reach a usable broker. Exit non-zero and let the
    # supervisor retry with backoff: a crash-looping unit is visible and
    # alertable, whereas staying up without consuming is not -- /metrics is
    # already served by this point, and `warn_if_stalled` stays quiet because no
    # page task ever started.
    except (TimeoutError, aio_pika.exceptions.AMQPError) as e:
        logger.error(
            "Could not establish a usable AMQP connection for queue %s within %ss. "
            "Exiting so we are restarted rather than sitting here not consuming.",
            settings.amqp_queue_name,
            settings.amqp_connect_timeout_seconds,
            exc_info=e,
        )
        raise
    finally:
        stall_warning_task.cancel()
        # Awaited, not just cancelled, so the loop is not torn down with it still
        # pending -- which asyncio reports as a "Task was destroyed" error.
        with suppress(asyncio.CancelledError):
            await stall_warning_task

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
