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
