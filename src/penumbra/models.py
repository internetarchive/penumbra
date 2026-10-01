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
    # How long to let aio-pika attempt to restore a channel the broker closed
    amqp_recovery_timeout_seconds: float = Field(default=10.0, gt=0)
    # Grace period for in-progress page tasks at shutdown.
    shutdown_drain_timeout_seconds: float = Field(default=30.0, gt=0)
    # How often the watchdog checks that this instance is still making progress.
    watchdog_interval_seconds: float = Field(default=30.0, gt=0)
    # How long the AMQP topology may stay unusable before the process gives up and
    # exits for the supervisor to restart it. Comfortably longer than
    # `amqp_recovery_timeout_seconds` and than a broker restart, so an outage that
    # resolves itself never costs a restart.
    broker_unhealthy_exit_seconds: float = Field(default=300.0, gt=0)
    # Heritrix rejects URLs longer than its UURI limit (2083 chars), so drop
    # over-length URLs before enqueueing rather than publishing dead links.
    max_url_length: int = Field(default=2083, ge=1)
    # Time to wait for a server to respond to the initial request
    navigation_timeout_seconds: float = Field(default=30.0, gt=0)
    # Time to wait for a page to be considered finished requestion resources. After this,
    # outlinks a send regardless of current page status
    page_timeout_seconds: float = Field(default=120.0, gt=0)
    context_close_timeout_seconds: float = Field(default=30.0, gt=0)
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
        precisely so it cannot be set below the phases it contains.
        The margin covers those setup calls.
        """
        return self.navigation_timeout_seconds + self.page_timeout_seconds + 30.0

    @computed_field
    @cached_property
    def outlink_publish_bound_seconds(self) -> float:
        """
        How long returning one page's links can take, at worst.
        """
        backoff = self.publish_retry_base_delay_seconds * (
            2 ** (self.publish_max_attempts - 1) - 1
        )
        return self.publish_max_attempts * self.publish_timeout_seconds + backoff

    @computed_field
    @cached_property
    def task_timeout_seconds(self) -> float:
        """
        Backstop deadline for a whole page task, covering the browser deadline
        plus the publish and cleanup that run after it. Only reached if one of
        the inner deadlines fails to do its job.
        """
        return (
            self.browser_deadline_seconds
            + self.outlink_publish_bound_seconds
            + self.context_close_timeout_seconds
            + self.amqp_ack_timeout_seconds
            + 30.0
        )

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
        # Omit an absent source rather than emitting `"source": null`.
        data = asdict(self)
        if self.source is None:
            del data["source"]
        return data


def clean_user_agent(value: object) -> str | None:
    """
    Heritrix sends `metadata.userAgent` only when it has one; absent, null and
    blank all mean "no preference", which Playwright spells as None.
    """
    if not isinstance(value, str):
        return None
    return value.strip() or None


@dataclass
class UmbraMetadata:
    path_from_seed: str
    heritable_data: HeritableData
    # Heritrix's own UA. None means Playwright's default. Not in `asdict`: an
    # instruction to us, not part of the reply Heritrix reads back.
    user_agent: str | None = None

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
          "pathFromSeed":"LL",
          "userAgent":"Mozilla/5.0 (compatible; heritrix/3.4.0 +https://example.com/crawl-info)"
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
            user_agent=clean_user_agent(
                json_message["metadata"].get("userAgent"),
            ),
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
