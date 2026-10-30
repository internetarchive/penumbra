# Penumbra - Playwright ENhanced Umbra

Penumbra is a reimplementation of Umbra around Playwright. It reads URLs from a queue, visits them
with a browser, interacts with the page, intercepts any requested URLs, and posts the
set of intercepted URLs to another queue. Heritrix reads from the output queue to 
potentially add URLs to its frontier.

The goal is to have a minimalist app to see how we can instrument the process in pursuit
of more Umbra-style URL harvesting scale. It's also to gain experience with a few 
interesting technologies generally: Prometheus, Playwright, asyncio, etc.

## Setup

Penumbra uses `uv` for development, ensure you have at least version 0.3.0. `uv` will
even install the right version of Python for you, how nice!

```shell
make install
make run
```

If metrics are enabled (the default), Prometheus metrics are available to scrape at
http://127.0.0.1:8888.

## Configuration

Settings are read from environment variables, or a `.env` file, prefixed `PENUMBRA_`.
Each is a field of `Settings` in `src/penumbra/models.py`.

### Browser

| Variable | Default | Description |
|---|---|---|
| `PENUMBRA_BROWSER_POOL_SIZE` | `1` | Chromium processes to launch. Pages are spread across them round-robin. |
| `PENUMBRA_CONTEXTS_PER_BROWSER` | `1` | Pages each browser crawls at once. Pool size × this is the concurrency limit, and the AMQP prefetch count. |
| `PENUMBRA_CHROMIUM_EXECUTABLE_PATH` | unset | Chromium binary to run instead of Playwright's. See [Using a different Chromium](#using-a-different-chromium). |
| `PENUMBRA_INSTALL_PLAYWRIGHT` | `true` | Run `playwright install --with-deps chromium` at startup. |
| `PENUMBRA_EVENT_LOOP` | `asyncio` | `asyncio` or `uvloop`. `uvloop` needs the `uvloop` extra installed. |

### Page timeouts

A page is crawled in two phases, each with its own timeout. Whatever a page requested
before a timeout is still published.

| Variable | Default | Description |
|---|---|---|
| `PENUMBRA_NAVIGATION_TIMEOUT_SECONDS` | `30` | Phase 1: wait for the server to answer and the document to commit. |
| `PENUMBRA_PAGE_TIMEOUT_SECONDS` | `120` | Phase 2: wait for the page's subresources to finish loading. |
| `PENUMBRA_CONTEXT_CLOSE_TIMEOUT_SECONDS` | `30` | Bound on closing a page's browser context. |

### Skipping resource types

Each of these, when `true`, aborts requests of that Playwright resource type instead of
loading them. This saves bandwidth, but the URLs are still published to Heritrix,
which fetches them itself. Anything a skipped resource would have requested in turn,
such as a script's own requests, is never seen. All default to `false`.

`PENUMBRA_SKIP_RESOURCE_DOCUMENT`, `PENUMBRA_SKIP_RESOURCE_STYLESHEET`,
`PENUMBRA_SKIP_RESOURCE_IMAGE`, `PENUMBRA_SKIP_RESOURCE_MEDIA`,
`PENUMBRA_SKIP_RESOURCE_FONT`, `PENUMBRA_SKIP_RESOURCE_SCRIPT`,
`PENUMBRA_SKIP_RESOURCE_TEXTTRACK`, `PENUMBRA_SKIP_RESOURCE_XHR`,
`PENUMBRA_SKIP_RESOURCE_FETCH`, `PENUMBRA_SKIP_RESOURCE_EVENTSOURCE`,
`PENUMBRA_SKIP_RESOURCE_WEBSOCKET`, `PENUMBRA_SKIP_RESOURCE_MANIFEST`,
`PENUMBRA_SKIP_RESOURCE_OTHER`

### AMQP

Penumbra consumes from one durable queue, bound to the exchange with the routing key
below. Each page's discovered URLs are published to the same exchange, routed by the
page's Heritrix `clientId`.

| Variable | Default | Description |
|---|---|---|
| `PENUMBRA_AMQP_URL` | `amqp://guest:guest@localhost:5672/%2f` | Broker URL. |
| `PENUMBRA_AMQP_QUEUE_NAME` | `urls` | Queue to consume pages from. |
| `PENUMBRA_AMQP_ROUTING_KEY` | `urls` | Routing key binding that queue to the exchange. |
| `PENUMBRA_AMQP_EXCHANGE_NAME` | `umbra` | Exchange for both consuming and publishing. |
| `PENUMBRA_AMQP_PERSISTENT_MESSAGES` | `false` | Publish with delivery mode 2, so queued URLs survive a broker restart. Costs a disk write per message and slows broker recovery in proportion to the backlog. |
| `PENUMBRA_AMQP_CONNECT_TIMEOUT_SECONDS` | `60` | Wait for a usable connection. |
| `PENUMBRA_AMQP_RECOVERY_TIMEOUT_SECONDS` | `10` | Time aio-pika gets to restore a channel the broker closed. |
| `PENUMBRA_AMQP_ACK_TIMEOUT_SECONDS` | `30` | Bound on acking a finished page. |

### Publishing

| Variable | Default | Description |
|---|---|---|
| `PENUMBRA_PUBLISH_TIMEOUT_SECONDS` | `30` | Bound on one publish attempt. |
| `PENUMBRA_PUBLISH_MAX_ATTEMPTS` | `3` | Attempts per URL before it is dropped and counted in `penumbra_urls_dropped_publish_failed`. |
| `PENUMBRA_PUBLISH_RETRY_BASE_DELAY_SECONDS` | `0.5` | First retry delay, doubling each attempt. |
| `PENUMBRA_MAX_URL_LENGTH` | `2083` | Longer URLs are dropped, since Heritrix rejects them. |
| `PENUMBRA_RECENTLY_SENT_MAX_SIZE` | `200000` | URLs remembered as already published, per process. `0` disables. |
| `PENUMBRA_RECENTLY_SENT_TTL_SECONDS` | `14400` | How long a published URL counts as recent, measured from when it was first sent. |
| `PENUMBRA_RECENTLY_SENT_ENFORCE` | `false` | Drop URLs already published for the same `clientId` within the TTL. Off, they are only counted in `penumbra_urls_recently_sent`. Heritrix would discard them anyway. |

### Health and shutdown

| Variable | Default | Description |
|---|---|---|
| `PENUMBRA_WATCHDOG_INTERVAL_SECONDS` | `30` | How often the broker connection is checked. |
| `PENUMBRA_BROKER_UNHEALTHY_EXIT_SECONDS` | `300` | Exit, for the supervisor to restart, once the broker has been unusable this long. |
| `PENUMBRA_SHUTDOWN_DRAIN_TIMEOUT_SECONDS` | `30` | Grace period for in-progress pages on shutdown. Unfinished pages are redelivered. |
| `PENUMBRA_METRICS_ENABLED` | `true` | Serve Prometheus metrics. |
| `PENUMBRA_METRICS_PORT` | `8888` | Metrics port. |

### Using a different Chromium

By default Penumbra runs the Chromium build pinned by its Playwright version. To run
another, such as a newer Chrome for Testing release carrying security fixes that
Playwright hasn't picked up yet, set:

```shell
PENUMBRA_CHROMIUM_EXECUTABLE_PATH=/opt/chrome-headless-shell-linux64/chrome-headless-shell
```

Unset or blank uses Playwright's build. Penumbra runs headless, so the closest match to
the default is the `chrome-headless-shell` build. Chrome for Testing publishes every
release at
`https://storage.googleapis.com/chrome-for-testing-public/<version>/linux64/chrome-headless-shell-linux64.zip`;
the current versions are listed at
https://googlechromelabs.github.io/chrome-for-testing/.

Playwright only supports the Chromium it pins, so stay on the same major version, or
test before going further ahead. The version actually launched is logged at startup:

```
Launched Chromium 156.0.8078.80 from /opt/chrome-headless-shell-linux64/chrome-headless-shell
```

## Sample crawl with Docker Compose

A complete sample crawl environment - Heritrix, Penumbra, RabbitMQ, and Prometheus -
can be started with:

```shell
docker compose up --build
```

Before starting, open `heritrix/jobs/sample-crawl/crawler-beans.cxml` and set your
operator contact URL in `simpleOverrides`:

```
metadata.operatorContactUrl=https://your-organisation.example/crawl-info
```

This URL is included in Heritrix's HTTP request headers so webmasters can identify and
contact the operator of the crawl. Heritrix requires a valid URL here before it will
run a job.

Once that is set, open the Heritrix web UI at **https://localhost:8443** (accept the
self-signed certificate, credentials `admin`/`admin`). The `sample-crawl` job will
appear; click **Build**, then **Launch**, then **Unpause** to start crawling.

| Service | URL | Credentials   |
|---|---|---------------|
| Heritrix web UI | https://localhost:8443 | admin / admin |
| RabbitMQ management | http://localhost:15672 | guest / guest |
| Penumbra metrics | http://localhost:8888 | -             |
| Prometheus | http://localhost:9090 | -             |

### How it works

Heritrix fetches the seed URLs and passes any HTML pages it encounters to Penumbra via
RabbitMQ. Penumbra loads each page in a headless Chromium browser, intercepts all
network requests the page makes (XHR, fetch, etc.), and publishes the discovered URLs
back to Heritrix. Heritrix adds those URLs to its frontier for crawling.

### Heritrix job configuration

The sample job in `heritrix/jobs/sample-crawl/crawler-beans.cxml` is the standard
Heritrix 3 default profile with the following changes:

**Seeds** (`longerOverrides`): set to `https://crawler-test.com/`, a site purpose-built
for testing web crawlers. It includes pages that make JavaScript-driven requests not
present in the static HTML, making it a clear demonstration of Penumbra's value: the
browser intercepts those requests and returns URLs that Heritrix's static extractors
would miss entirely.

**`seeds.sourceTagSeeds=true`**: each seed URL is tagged with a source identifier.
This enables source-based reporting in Heritrix and allows the `pathFromSeed` metadata
passed to Penumbra to be used for scoping decisions. Not required, but demonstrates how metadata
will be passed to Penumbra and inherited by discovered URLs.

**`scope.rules[2].maxHops=0`**: Heritrix will only fetch the seed URLs directly.
It will not follow any links it extracts, only embeds. This is to scope our example so that it
doesn't grow, unbounded. URLs that Penumbra discovers and returns via `AMQPUrlReceiver` are injected
into the frontier and fetched in turn, with scope rules applied as normal.

**Disabling Penumbra**: to compare crawl results with and without browser-based
extraction, add `amqpPublishProcessor.enabled=false` to `simpleOverrides`:

```
metadata.operatorContactUrl=https://your-organisation.example/crawl-info
amqpPublishProcessor.enabled=false
```

With `crawler-test.com` as the seed, disabling Penumbra yields ~57 discovered URLs;
enabling it yields ~497, as the browser intercepts JavaScript-driven requests invisible
to Heritrix's static extractors.

**Penumbra AMQP beans**:
- `amqpPublishProcessor` - added to the end of the fetch chain; sends each fetched
  HTML page that passes the `shouldProcessRule` filter to Penumbra via the
  `penumbra_exchange` exchange with routing key `penumbra_urls`.
- `amqpUrlWaiter` - standalone Spring bean that runs in its own thread; blocks
  Heritrix's processing of a URI until Penumbra has finished with it and returned
  its discovered URLs.
- `amqpUrlReceiver` - standalone Spring bean that runs in its own thread; listens
  on the `sample_crawl` queue (named after `clientId`) and injects URLs discovered
  by Penumbra back into Heritrix's frontier.

## Development

```shell
make format
make lint
```

You can run Prometheus in docker to watch metrics as you develop.

```shell
docker compose up prometheus
```

The included prometheus.yml file is configured to scrape metrics from the docker host's
port 8888.

The stock Prometheus interface is no Grafana, but it works in a pinch. You can access it
at http://127.0.0.1:9090.

Go to the "Graph" tab, and enter a query like:

```
rate(penumbra_pages_crawled_total[1m])
```

This gives you the rate of pages crawled per second in 1 minute windows.

## Install for deployment

```shell
uv tool install --with uvloop --python 3.12 penumbra
```
