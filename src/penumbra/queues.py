import asyncio
from contextlib import asynccontextmanager

import aio_pika

from penumbra import metrics, models

# Bound on closing a connection whose setup did not finish. Not configurable: it
# only runs on an error path, where the alternative is hanging on `_connect_lock`
# and blocking every other publisher behind it.
DISCARD_CLOSE_TIMEOUT = 10.0


class AsyncMessageClient:
    def __init__(
        self,
        amqp_url="amqp://guest:guest@localhost:5672/%2f",
        queue_name="urls",
        exchange_name="umbra",
        routing_key="urls",
        prefetch_count=1,
        publish_timeout=30.0,
        connect_timeout=60.0,
    ):
        """
        AsyncMessageClient

        :param amqp_url:
        :param queue_name:
        :param exchange_name:
        :param routing_key:
        :param prefetch_count: unacked messages RabbitMQ may push at us at once
        :param publish_timeout: seconds to wait for a publish confirm
        :param connect_timeout: seconds to wait for a connection to become usable
        """
        self.amqp_url = amqp_url
        self.queue_name = queue_name
        self.exchange_name = exchange_name
        self.routing_key = routing_key
        self.prefetch_count = prefetch_count
        self.publish_timeout = publish_timeout
        self.connect_timeout = connect_timeout
        self.connection = None
        self.exchange = None
        self.queue = None
        # `connect` is awaited concurrently by every in-flight publish, so the
        # check-then-create below has to be serialised or we open one connection
        # per racing caller.
        self._connect_lock = asyncio.Lock()

    async def connect(self):
        async with self._connect_lock:
            if self.connection is None or self.connection.is_closed:
                # `timeout` bounds the handshake, and `connect_robust` reuses it
                # for each later reconnect attempt. Without it a broker that
                # accepts TCP and then goes quiet blocks here forever.
                connection = await aio_pika.connect_robust(
                    self.amqp_url, timeout=self.connect_timeout
                )
                try:
                    # Attached before anything is declared, so there is no window
                    # in which the connection can drop unnoticed.
                    connection.close_callbacks.add(self._on_disconnected)
                    connection.reconnect_callbacks.add(self._on_reconnected)

                    # Consuming and publishing get their own channels. On a
                    # shared channel a publish-side failure (or a broker resource
                    # alarm blocking the publisher) takes the consumer down with
                    # it.
                    consume_channel = await connection.channel()
                    # Without an explicit QoS, RabbitMQ pushes the entire queue at
                    # this consumer and aio-pika buffers it in an unbounded
                    # asyncio.Queue: unbounded memory, and a `purge` leaves the
                    # already-delivered backlog to be worked through anyway.
                    await consume_channel.set_qos(prefetch_count=self.prefetch_count)
                    queue = await consume_channel.declare_queue(
                        self.queue_name, durable=True
                    )

                    publish_channel = await connection.channel()
                    exchange = await publish_channel.declare_exchange(
                        self.exchange_name,
                        type=aio_pika.ExchangeType.DIRECT,
                        durable=True,
                    )
                    await queue.bind(exchange, routing_key=self.routing_key)
                # BaseException, so this covers cancellation: callers publish
                # under a deadline, which makes being cancelled at any of the
                # awaits above reachable in normal operation.
                except BaseException:
                    await self._discard(connection)
                    raise

                # Everything is declared, so publish it to `self` in one step.
                # Assigning `self.connection` up front instead would let a
                # cancelled `connect` cache an open connection with no exchange:
                # the check above would then skip rebuilding it, and every later
                # publish would fail for the life of the process.
                self.connection = connection
                self.queue = queue
                self.exchange = exchange
                # Set here rather than straight after `connect_robust`, so the
                # gauge means "usable", not just "socket open".
                metrics.penumbra_broker_connected.set(1)

        return self.queue, self.exchange

    @staticmethod
    def _on_disconnected(*_args) -> None:
        """
        Connection-close callback. Takes the gauge down so that "process up but not
        consuming" is visible: nothing else distinguishes it from an idle queue,
        because a scrapeable /metrics and a pages-crawled counter that is not
        moving look exactly the same in both cases.
        """
        metrics.penumbra_broker_connected.set(0)

    @staticmethod
    def _on_reconnected(*_args) -> None:
        """Reconnect callback. Robust channels re-declare the topology, so by the
        time this fires the connection is usable again."""
        metrics.penumbra_broker_connected.set(1)

    @staticmethod
    async def _discard(connection) -> None:
        """
        Close a connection whose setup did not finish, so it is not leaked.

        Bounded and never raises: the reason setup failed is often that the broker
        is unreachable, and this runs while still holding `_connect_lock` on the
        way out of an already-failing `connect`.
        """
        try:
            async with asyncio.timeout(DISCARD_CLOSE_TIMEOUT):
                await connection.close()
        except Exception:
            pass

    @asynccontextmanager
    async def iterator(self):
        """
        Async context manager that yields an iterator of incoming messages.
        Registers a persistent consumer with RabbitMQ (basic.consume), giving
        push-based delivery rather than polling.
        """
        queue, _ = await self.connect()
        async with queue.iterator() as it:
            yield it

    async def publish_message(self, umbra_response: models.UmbraResponse) -> None:
        """
        Publish an UmbraResponse message to the specified exchange.

        Bounded by `publish_timeout`: publisher confirms are on by default, and
        a broker under a memory or disk alarm blocks publishers indefinitely
        rather than refusing them.

        The deadline covers `connect` as well as the publish itself. Reconnecting
        is part of the cost of a publish, and `connect_robust` retries a
        reachable-but-unhealthy broker without a deadline of its own.
        """
        routing_key = umbra_response.client_id
        async with asyncio.timeout(self.publish_timeout):
            # Get the cached connection, opening one if this is the first publish
            # or the last connection dropped.
            queue, exchange = await self.connect()
            await exchange.publish(
                aio_pika.Message(body=str(umbra_response.asdict()).encode()),
                routing_key=routing_key,
            )

    async def close_connection(self) -> None:
        if self.connection and not self.connection.is_closed:
            await self.connection.close()
