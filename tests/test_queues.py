import asyncio
from contextlib import suppress
from time import monotonic
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from prometheus_client import REGISTRY

from penumbra import metrics
from penumbra.queues import AsyncMessageClient

# Outer guard on the bounded-publish tests, so a regression hangs the test rather
# than the suite. It has to stay well above `publish_timeout` and well below the
# elapsed-time assertions, which are what actually prove whose deadline fired --
# `wait_for` raises the same TimeoutError the client does.
GUARD_TIMEOUT = 5.0


def connection_mock(channel_side_effect=None) -> MagicMock:
    """
    A broker connection whose channels declare everything successfully.

    `channel_side_effect` replaces `connection.channel`, so a test can make the
    setup that follows the connect hang or fail.
    """
    connection = MagicMock()
    connection.is_closed = False
    connection.close = AsyncMock()

    def new_channel(*args, **kwargs):
        channel = MagicMock()
        channel.set_qos = AsyncMock()
        channel.declare_queue = AsyncMock(return_value=MagicMock(bind=AsyncMock()))
        channel.declare_exchange = AsyncMock()
        return channel

    if channel_side_effect is None:
        connection.channel = AsyncMock(side_effect=new_channel)
    else:
        connection.channel = AsyncMock(side_effect=channel_side_effect)
    return connection


@pytest.mark.asyncio
async def test_connect_declares_queue_and_exchange_on_separate_channels():
    """
    A publish-side failure on a shared channel takes the consumer down with it, so
    consuming and publishing each get their own channel -- and the consume channel
    gets an explicit prefetch, without which RabbitMQ pushes the whole queue into
    an unbounded in-process buffer.
    """
    channels = []

    def new_channel(*args, **kwargs):
        channel = MagicMock()
        channel.set_qos = AsyncMock()
        channel.declare_queue = AsyncMock(return_value=MagicMock(bind=AsyncMock()))
        channel.declare_exchange = AsyncMock()
        channels.append(channel)
        return channel

    connection = connection_mock(channel_side_effect=new_channel)
    client = AsyncMessageClient(prefetch_count=7, connect_timeout=11.0)
    connect_robust = AsyncMock(return_value=connection)

    with patch("aio_pika.connect_robust", connect_robust):
        queue, exchange = await client.connect()

    connect_robust.assert_awaited_once_with(client.amqp_url, timeout=11.0)

    consume_channel, publish_channel = channels
    assert len(channels) == 2
    consume_channel.set_qos.assert_awaited_once_with(prefetch_count=7)
    consume_channel.declare_queue.assert_awaited_once()
    publish_channel.declare_exchange.assert_awaited_once()
    # The queue is declared on the consume channel, the exchange on the publish one.
    assert consume_channel.declare_exchange.await_count == 0
    assert publish_channel.declare_queue.await_count == 0
    queue.bind.assert_awaited_once_with(exchange, routing_key=client.routing_key)


@pytest.mark.asyncio
async def test_connect_cancelled_midway_caches_nothing_and_closes_the_connection():
    """
    Callers publish under a deadline, so being cancelled part-way through setup is
    reachable. Caching the connection before the declares finished left it open
    with no exchange -- and because the connection then looked healthy, `connect`
    never rebuilt it and every later publish failed for the life of the process.
    """
    hanging = True

    async def channel(*args, **kwargs):
        if hanging:
            await asyncio.sleep(60)
        result = MagicMock()
        result.set_qos = AsyncMock()
        result.declare_queue = AsyncMock(return_value=MagicMock(bind=AsyncMock()))
        result.declare_exchange = AsyncMock()
        return result

    connection = connection_mock(channel_side_effect=channel)
    client = AsyncMessageClient()

    with patch("aio_pika.connect_robust", AsyncMock(return_value=connection)):
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.1):
                await client.connect()

        # Nothing half-built is cached, and the connection is not leaked.
        assert client.connection is None
        assert client.queue is None
        assert client.exchange is None
        connection.close.assert_awaited_once()

        # A later attempt rebuilds from scratch and works.
        hanging = False
        queue, exchange = await client.connect()

    assert queue is not None and exchange is not None
    assert client.connection is connection


@pytest.mark.asyncio
async def test_connect_bounds_a_broker_that_accepts_then_goes_quiet():
    """
    `connect_robust` fails fast on a *refused* connection, but a broker that
    accepts TCP and then never completes the handshake -- a hung node, a load
    balancer holding the socket open -- blocked here forever without an explicit
    timeout. With /metrics already served by then, the process looked healthy
    while consuming nothing.
    """
    handshakes = 0

    async def accept_and_say_nothing(reader, writer):
        nonlocal handshakes
        handshakes += 1
        with suppress(asyncio.CancelledError):
            await asyncio.sleep(GUARD_TIMEOUT * 2)

    server = await asyncio.start_server(accept_and_say_nothing, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    client = AsyncMessageClient(
        amqp_url=f"amqp://guest:guest@127.0.0.1:{port}/%2f", connect_timeout=0.3
    )
    # As at startup, before anything has connected.
    metrics.penumbra_broker_connected.set(0)

    try:
        started = monotonic()
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(client.connect(), timeout=GUARD_TIMEOUT)
        # The client's own 0.3s deadline, not the outer guard.
        assert monotonic() - started < 1

        # Nothing cached, and the gauge shows we are not consuming.
        assert client.connection is None
        assert client.exchange is None
        assert REGISTRY.get_sample_value("penumbra_broker_connected") == 0

        # The failure is reported rather than retried behind our back: a factory
        # task still looping here would hold a connection nobody closes.
        await asyncio.sleep(0.2)
        assert handshakes == 1
        assert not [
            task
            for task in asyncio.all_tasks()
            if "connection_factory" in str(task.get_coro())
        ]
    finally:
        server.close()


@pytest.mark.asyncio
async def test_broker_connected_gauge_follows_the_connection():
    """
    The gauge is the only thing that distinguishes "up but not consuming" from an
    idle queue: /metrics is scrapeable either way, and a pages-crawled counter
    that is not moving looks identical.
    """
    connection = connection_mock()
    client = AsyncMessageClient()
    metrics.penumbra_broker_connected.set(0)

    with patch("aio_pika.connect_robust", AsyncMock(return_value=connection)):
        await client.connect()

    # Only set once the topology is declared, so it means "usable".
    assert REGISTRY.get_sample_value("penumbra_broker_connected") == 1

    (on_disconnect,), _ = connection.close_callbacks.add.call_args
    (on_reconnect,), _ = connection.reconnect_callbacks.add.call_args

    on_disconnect(connection, None)
    assert REGISTRY.get_sample_value("penumbra_broker_connected") == 0

    on_reconnect(connection)
    assert REGISTRY.get_sample_value("penumbra_broker_connected") == 1


@pytest.mark.asyncio
async def test_discard_does_not_hang_on_an_unreachable_broker(monkeypatch):
    """
    The cleanup close runs while holding `_connect_lock`, usually because the
    broker is unreachable. If it blocked there, every other publisher would queue
    up behind it.
    """
    monkeypatch.setattr("penumbra.queues.DISCARD_CLOSE_TIMEOUT", 0.1)

    async def hang():
        await asyncio.sleep(60)

    connection = MagicMock()
    connection.close = AsyncMock(side_effect=hang)

    await asyncio.wait_for(AsyncMessageClient._discard(connection), timeout=5)


@pytest.mark.asyncio
async def test_concurrent_connect_opens_a_single_connection():
    """
    Every in-flight publish awaits `connect`, so the check-then-create has to be
    serialised or a page's worth of racing publishes opens a connection each.
    """
    connection = connection_mock()
    connect_robust = AsyncMock(return_value=connection)
    client = AsyncMessageClient()

    with patch("aio_pika.connect_robust", connect_robust):
        results = await asyncio.gather(*(client.connect() for _ in range(10)))

    assert connect_robust.await_count == 1
    # Every caller sees the same finished topology.
    assert {id(queue) for queue, _ in results} == {id(client.queue)}
    assert {id(exchange) for _, exchange in results} == {id(client.exchange)}


@pytest.mark.asyncio
async def test_publish_timeout_covers_the_connect_as_well_as_the_publish():
    """
    Reconnecting is part of the cost of a publish, and `connect_robust` retries a
    reachable-but-unhealthy broker without a deadline of its own. With the connect
    outside the timeout, `publish_timeout` bounded nothing on the path that
    actually blocks.
    """

    async def hang(*args, **kwargs):
        await asyncio.sleep(60)

    client = AsyncMessageClient(publish_timeout=0.1)
    response = MagicMock(client_id="urls")
    response.asdict.return_value = {"url": "https://example.com"}

    started = monotonic()
    with patch("aio_pika.connect_robust", AsyncMock(side_effect=hang)):
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(
                client.publish_message(response), timeout=GUARD_TIMEOUT
            )
    # It was the client's own 0.1s deadline that fired, not the outer guard.
    assert monotonic() - started < 1


@pytest.mark.asyncio
async def test_publish_message_is_bounded_by_publish_timeout():
    """A broker under a resource alarm blocks publishers rather than refusing."""

    async def hang(*args, **kwargs):
        await asyncio.sleep(60)

    connection = connection_mock()
    client = AsyncMessageClient(publish_timeout=0.1)
    response = MagicMock(client_id="urls")
    response.asdict.return_value = {"url": "https://example.com"}

    with patch("aio_pika.connect_robust", AsyncMock(return_value=connection)):
        await client.connect()
        client.exchange.publish = AsyncMock(side_effect=hang)
        started = monotonic()
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(
                client.publish_message(response), timeout=GUARD_TIMEOUT
            )
    assert monotonic() - started < 1
