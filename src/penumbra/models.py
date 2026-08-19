from dataclasses import asdict, dataclass
from functools import cached_property
from typing import Literal

from pydantic import Field, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    event_loop: Literal["asyncio", "uvloop"] = Field(default="asyncio")
    browser_pool_size: int = Field(default=1, ge=1)
    contexts_per_browser: int = Field(default=1, ge=1)
    metrics_enabled: bool = Field(default=True)
    metrics_port: int = Field(default=8888, ge=1)
    amqp_url: str = Field(default="amqp://guest:guest@localhost:5672/%2f")
    amqp_queue_name: str = Field(default="urls")
    amqp_routing_key: str = Field(default="urls")
    amqp_exchange_name: str = Field(default="umbra")
    amqp_connect_timeout_seconds: float = Field(default=60.0, gt=0)
    # How long to let aio-pika restore a channel the broker closed before giving
    # up on it and rebuilding the connection. A robust channel normally reopens
    # itself, so this is a grace period rather than a limit -- but the recovery is
    # not guaranteed, and without an end to the waiting a dead channel under a
    # live connection stops the instance consuming for the life of the process.
    # Kept below `publish_timeout_seconds`, which encloses it on the publish path.
    amqp_recovery_timeout_seconds: float = Field(default=10.0, gt=0)
    # Grace period for in-progress page tasks at shutdown. Deliberately well under
    # systemd's default TimeoutStopSec (90s): a page task's own backstop is
    # `task_timeout_seconds`, so an unbounded drain would earn a SIGKILL whenever
    # pages are in flight. Abandoned tasks were never acked, so their messages are
    # redelivered.
    shutdown_drain_timeout_seconds: float = Field(default=30.0, gt=0)
    # How often the watchdog checks that this instance is still making progress.
    watchdog_interval_seconds: float = Field(default=30.0, gt=0)
    # How long the AMQP topology may stay unusable before the process gives up and
    # exits for the supervisor to restart it. Comfortably longer than
    # `amqp_recovery_timeout_seconds` and than a broker restart, so an outage that
    # resolves itself never costs a restart -- but far short of the ten hours the
    # August outages spent sitting idle. See `watch_broker`.
    broker_unhealthy_exit_seconds: float = Field(default=300.0, gt=0)
    # Heritrix rejects URLs longer than its UURI limit (2083 chars), so drop
    # over-length URLs before enqueueing rather than publishing dead links.
    max_url_length: int = Field(default=2083, ge=1)
    # Put a failed message back on the queue for another attempt.
    #
    # EXPERIMENTAL, and off by default: this is the setting most likely to take
    # an instance down. A requeued message goes back to the head of the queue and
    # is redelivered immediately, so a URL that fails the same way every time
    # loops as fast as it can fail. With only
    # `browser_pool_size * contexts_per_browser` slots, a handful of those
    # occupy every one of them while the real backlog waits, and the process
    # stays up and healthy-looking throughout. Every redelivery loop seen in
    # production so far has started this way.
    #
    # Off, a retryable failure loses that page's links outright. That is the
    # cheaper mistake: Heritrix fetches the URL itself regardless of what
    # penumbra reports, so what is lost is the links from one page rather than
    # an instance's throughput. Turn it on only while watching
    # `penumbra_pages_failed`.
    enable_page_retries: bool = Field(default=False)
    # Time to wait for a server to respond to the initial request
    navigation_timeout_seconds: float = Field(default=30.0, gt=0)
    # Time to wait for a page to be considered finished requestion resources. After this,
    # outlinks a send regardless of current page status
    page_timeout_seconds: float = Field(default=120.0, gt=0)
    context_close_timeout_seconds: float = Field(default=30.0, gt=0)
    # Returning a page's links to Heritrix. Deliberately outside the browser
    # deadline, so a slow broker is not charged to the page and misreported as a
    # page timeout -- which means it needs a bound of its own. Publishes run
    # concurrently and each is already bounded by `publish_timeout_seconds` with
    # `publish_max_attempts` retries, so the natural worst case is around 92s;
    # this sits above that as a backstop rather than a limiter.
    outlink_publish_timeout_seconds: float = Field(default=120.0, gt=0)
    amqp_ack_timeout_seconds: float = Field(default=30.0, gt=0)
    publish_timeout_seconds: float = Field(default=30.0, gt=0)
    publish_max_attempts: int = Field(default=3, ge=1)
    publish_retry_base_delay_seconds: float = Field(default=0.5, gt=0)
    install_playwright: bool = Field(default=True)
    skip_resource_document: bool = Field(default=False)
    skip_resource_stylesheet: bool = Field(default=False)
    skip_resource_image: bool = Field(default=False)
    skip_resource_media: bool = Field(default=False)
    skip_resource_font: bool = Field(default=False)
    skip_resource_script: bool = Field(default=False)
    skip_resource_texttrack: bool = Field(default=False)
    skip_resource_xhr: bool = Field(default=False)
    skip_resource_fetch: bool = Field(default=False)
    skip_resource_eventsource: bool = Field(default=False)
    skip_resource_websocket: bool = Field(default=False)
    skip_resource_manifest: bool = Field(default=False)
    skip_resource_other: bool = Field(default=False)

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", env_prefix="penumbra_"
    )

    @computed_field
    @cached_property
    def max_concurrency(self) -> int:
        """Concurrent page tasks, and therefore the AMQP prefetch count."""
        return self.browser_pool_size * self.contexts_per_browser

    @computed_field
    @cached_property
    def browser_deadline_seconds(self) -> float:
        """
        Backstop over the whole browser interaction.

        Both crawl phases carry their own timeout, so reaching this means one of
        the *untimed* Playwright protocol calls -- `new_context`, `new_page`,
        `route` -- hung against a wedged browser. Derived rather than configured
        precisely so it cannot be set below the phases it contains: that was the
        one way the two page timeouts used to be able to cancel each other out.
        The margin covers those setup calls.
        """
        return self.navigation_timeout_seconds + self.page_timeout_seconds + 30.0

    @computed_field
    @cached_property
    def task_timeout_seconds(self) -> float:
        """
        Backstop deadline for a whole page task, covering the browser deadline
        plus the bounded publish and cleanup that run after it. Only reached if
        one of the inner deadlines fails to do its job.
        """
        return (
            self.browser_deadline_seconds
            + self.outlink_publish_timeout_seconds
            + self.context_close_timeout_seconds
            + self.amqp_ack_timeout_seconds
            + 30.0
        )

    @computed_field
    @cached_property
    def page_task_stall_seconds(self) -> float:
        """
        How long a single page task may run before the watchdog calls it stuck.

        Derived rather than configured, so it cannot be set below the backstop it
        is checking. `run_page_task` wraps every page in `task_timeout_seconds`,
        so no task should ever reach this: getting here means the `wait_for`
        itself failed to fire -- a cancellation the page never honoured -- and the
        semaphore permit it holds is gone for the life of the process.

        The margin is generous because the cost of a false positive is a restart
        of a working instance, while the cost of a slow true positive is one slot
        out of `max_concurrency` for a few more minutes.
        """
        return self.task_timeout_seconds + 300.0

    @computed_field
    @cached_property
    def skip_resource_types(self) -> set[str]:
        return {
            resource_type
            for resource_type in {
                "document" if self.skip_resource_document else None,
                "stylesheet" if self.skip_resource_stylesheet else None,
                "image" if self.skip_resource_image else None,
                "media" if self.skip_resource_media else None,
                "font" if self.skip_resource_font else None,
                "script" if self.skip_resource_script else None,
                "texttrack" if self.skip_resource_texttrack else None,
                "xhr" if self.skip_resource_xhr else None,
                "fetch" if self.skip_resource_fetch else None,
                "eventsource" if self.skip_resource_eventsource else None,
                "websocket" if self.skip_resource_websocket else None,
                "manifest" if self.skip_resource_manifest else None,
                "other" if self.skip_resource_other else None,
            }
            if resource_type
        }


@dataclass
class HeritableData:
    source: str | None
    heritable: list[str]

    def __str__(self) -> str:
        return f"source:{self.source} heritable:{self.heritable}"

    def asdict(self) -> dict:
        return asdict(self)


@dataclass
class UmbraMetadata:
    path_from_seed: str
    heritable_data: HeritableData

    def __str__(self) -> str:
        return (
            f"path_from_seed:{self.path_from_seed} heritable_data:{self.heritable_data}"
        )

    def asdict(self) -> dict:
        return {
            "pathFromSeed": self.path_from_seed,
            "heritableData": self.heritable_data.asdict(),
        }


@dataclass(init=False)
class UmbraMessage:
    """
    Example:

    {
       "metadata":{
          "heritableData":{
             "source":"https://example.com/",
             "heritable":[
                "source",
                "heritable"
             ]
          },
          "pathFromSeed":"LL"
       },
       "clientId":"example_crawl",
       "url":"https://example.com/sub/page"
    }
    """

    metadata: UmbraMetadata
    client_id: str
    url: str

    def __init__(self, json_message: dict):
        self.client_id = json_message.get("clientId")
        self.url = json_message.get("url")
        heritable_data = HeritableData(
            source=json_message["metadata"]["heritableData"].get("source"),
            heritable=json_message["metadata"]["heritableData"].get("heritable", []),
        )
        self.metadata = UmbraMetadata(
            path_from_seed=json_message.get("metadata").get("pathFromSeed"),
            heritable_data=heritable_data,
        )

    def __str__(self) -> str:
        return f"client_id: {self.client_id} url:{self.url} metadata:{self.metadata}"


@dataclass(init=False)
class UmbraResponse:
    """
     Example

     {
    "url":"https://www.senate.gov/resources/fonts/css/font-awesome.min.css",
    "headers":{

    },
    "parentUrl":"https://www.senate.gov/about/historic-buildings-spaces/meeting-places.htm",
    "parentUrlMetadata":{
       "heritableData":{
          "source":"https://www.senate.gov",
          "heritable":[
             "source",
             "heritable"
          ]
       },
       "pathFromSeed":"L"
    },
    "method":"GET"
    """

    url: str
    method: str
    headers: dict
    parent_url: str
    parent_url_metadata: UmbraMetadata

    def __init__(
        self, url: str, method: str, headers: dict, parent_message: UmbraMessage
    ):
        self.url = url
        self.method = method
        self.headers = headers
        self.parent_url = parent_message.url
        self.parent_url_metadata = parent_message.metadata
        self.client_id = parent_message.client_id

    def __str__(self) -> str:
        return f"url:{self.url} method:{self.method} headers:{self.headers} parent_url:{self.parent_url} parent_url_metadata:{self.parent_url_metadata}"

    def asdict(self) -> dict:
        return {
            "url": self.url,
            "method": self.method,
            "headers": self.headers,
            "parentUrl": self.parent_url,
            "parentUrlMetadata": self.parent_url_metadata.asdict(),
        }
