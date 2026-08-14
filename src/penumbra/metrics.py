from prometheus_client import Counter, Gauge, Histogram, start_http_server

# fmt: off
penumbra_pages_crawled = Counter("penumbra_pages_crawled", "pages penumbra has taken off the queue and attempted, however they turned out; a site that was offline or timed out is still a crawl result, and penumbra_pages_failed carries the breakdown")
penumbra_urls_found = Counter("penumbra_urls_found", "number of URLs extracted by penumbra")
penumbra_urls_dropped_too_long = Counter("penumbra_urls_dropped_too_long", "number of URLs dropped for exceeding the max URL length before publishing")
penumbra_page_processing_duration_seconds = Histogram("penumbra_page_processing_duration_seconds", "time spent processing a page in penumbra")
penumbra_last_page_crawled_time = Gauge("penumbra_last_page_crawled_time", "time of last page visit")
penumbra_in_progress_pages = Gauge("penumbra_in_progress_pages", "number of pages currently processing with penumbra")
penumbra_broker_connected = Gauge("penumbra_broker_connected", "1 while a usable AMQP connection is established, 0 otherwise; 0 with the process up means penumbra is not consuming")
penumbra_url_publishing_duration_seconds = Histogram("penumbra_url_publishing_duration_seconds", "time spent publishing URLs to RabbitMQ in penumbra")
penumbra_amqp_publish_exceptions = Counter("penumbra_amqp_publish_exceptions", "count of exceptions thrown while publishing umbra responses")
penumbra_amqp_publish_retries = Counter("penumbra_amqp_publish_retries", "count of publish attempts retried after a failure")
penumbra_urls_dropped_publish_failed = Counter("penumbra_urls_dropped_publish_failed", "number of URLs dropped after every publish attempt failed")
penumbra_page_timeouts = Counter("penumbra_page_timeouts", "number of pages that exceeded the page processing deadline; the links they reached first are still published")
penumbra_pages_failed = Counter("penumbra_pages_failed", "pages that did not load cleanly, by reason; reason is the Chromium net:: error code where there is one, otherwise a fixed string or an exception class name", labelnames=["reason"])
penumbra_page_task_deadline_exceeded = Counter("penumbra_page_task_deadline_exceeded", "number of page tasks killed by the backstop deadline; nonzero means an inner timeout failed to fire")
penumbra_resources_requested = Counter("penumbra_resources_requested", "number of resources requested", labelnames=["resource_type"])
penumbra_resources_fetched = Counter("penumbra_resources_fetched", "number of resources fetched", labelnames=["resource_type", "status_code"])
penumbra_resources_size_bytes = Counter("penumbra_resources_size_bytes", "total size of resources fetched", labelnames=["resource_type"])
penumbra_resources_fetch_time = Counter("penumbra_resources_fetch_time", "time spent fetching resources", labelnames=["resource_type"])
# fmt: on


def register_prom_metrics(metrics_port: int = 8888):
    start_http_server(metrics_port)
