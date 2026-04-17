from prometheus_client import Counter, Gauge, Histogram, start_http_server

# fmt: off
penumbra_pages_crawled = Counter("penumbra_pages_crawled", "number of pages visited by penumbra")
penumbra_urls_found = Counter("penumbra_urls_found", "number of URLs extracted by penumbra")
penumbra_page_processing_duration_seconds = Histogram("penumbra_page_processing_duration_seconds", "time spent processing a page in penumbra")
penumbra_last_page_crawled_time = Gauge("penumbra_last_page_crawled_time", "time of last page visit")
penumbra_in_progress_pages = Gauge("penumbra_in_progress_pages", "number of pages currently processing with penumbra")
penumbra_url_publishing_duration_seconds = Histogram("penumbra_url_publishing_duration_seconds", "time spent publishing URLs to RabbitMQ in penumbra")
penumbra_ampq_publish_exceptions = Counter("penumbra_ampq_publish_exceptions", "count of exceptions thrown while publishing umbra responses")
penumbra_resources_requested = Counter("penumbra_resources_requested", "number of resources requested", labelnames=["resource_type"])
penumbra_resources_fetched = Counter("penumbra_resources_fetched", "number of resources fetched", labelnames=["resource_type", "status_code"])
penumbra_resources_size_total = Counter("penumbra_resources_size_total", "total size of resources fetched", labelnames=["resource_type"])
penumbra_resources_fetch_time = Counter("penumbra_resources_fetch_time", "time spent fetching resources", labelnames=["resource_type"])
# fmt: on


def register_prom_metrics(metrics_port: int = 8888):
    start_http_server(metrics_port)
