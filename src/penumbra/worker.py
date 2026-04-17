import asyncio
import json
import logging
import signal
from itertools import cycle

import aio_pika
from playwright.async_api import Browser, Request, Response, Route, async_playwright
from prometheus_async.aio import count_exceptions, time, track_inprogress

from penumbra import metrics
from penumbra.models import Settings, UmbraMessage, UmbraResponse
from penumbra.queues import AsyncMessageClient

logger = logging.getLogger(__name__)
# Add a global variable to manage shutdown signal
shutdown_event = asyncio.Event()
settings = Settings()


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


@time(metrics.penumbra_url_publishing_duration_seconds)
@count_exceptions(metrics.penumbra_ampq_publish_exceptions)
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
            logger.info("Publishing URL %s", url)
            umbra_response = UmbraResponse(
                url=url,
                method="GET",
                headers={},
                parent_message=parent_message,
            )
            tg.create_task(client.publish_message(umbra_response))


def update_metrics(urls: set[str]) -> None:
    """Update `process_page` Prometheus metrics."""
    metrics.penumbra_last_page_crawled_time.set_to_current_time()
    metrics.penumbra_pages_crawled.inc(1)
    metrics.penumbra_urls_found.inc(len(urls))


async def robust_context_close(context) -> None:
    """Playwright sometimes throws exceptions if you try to close a closed context."""
    try:
        await context.close()
    except Exception:
        return


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
        metrics.penumbra_resources_size_total.labels(request.resource_type).inc(
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
    """
    message = UmbraMessage(json.loads(raw_message.body))
    try:
        context = await browser.new_context()
        page_requests = set()
        page = await context.new_page()
        await page.route("**/*", handle_route)
        page.on("request", lambda request: page_requests.add(request.url))
        page.on("requestfinished", handle_request_finished)
        await page.goto(message.url)
        await publish_umbra_response(client, message, page_requests)
        await raw_message.ack()
        update_metrics(page_requests)
    except Exception as e:
        logger.warning("Exception while processing page: %s", message.url, exc_info=e)
        await raw_message.nack(requeue=True)
    finally:
        if "context" not in locals():
            return
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

    # Setup RabbitMQ client
    client = AsyncMessageClient(
        amqp_url=settings.amqp_url,
        queue_name=settings.amqp_queue_name,
        routing_key=settings.amqp_routing_key,
        exchange_name=settings.amqp_exchange_name,
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
    max_concurrency = settings.browser_pool_size * settings.contexts_per_browser
    semaphore = SilentBoundedSemaphore(max_concurrency)

    # done_callback captures our semaphore in a closure
    tasks = set()

    def done_callback(task: asyncio.Task):
        """
        `done_callback` is called when `process_page` tasks complete.
        Release the semaphore to allow another task to begin.
        """
        tasks.discard(task)
        semaphore.release()

    # Function to handle shutdown signals
    def shutdown_signal_handler():
        logger.info("Shutdown signal received. Shutting down gracefully...")

        shutdown_event.set()

    # Register the signal handlers
    signal.signal(signal.SIGTERM, lambda s, f: shutdown_signal_handler())
    signal.signal(signal.SIGINT, lambda s, f: shutdown_signal_handler())

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
                    process_page(client, browser["browser"], raw_message)
                )
                task.add_done_callback(done_callback)
                tasks.add(task)

        # Wait for in-progress tasks to complete before shutdown
        logger.info("Waiting for in-progress tasks to complete...")
        await asyncio.gather(*tasks)

    except asyncio.CancelledError:
        pass
    finally:
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
