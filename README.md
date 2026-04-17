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
