import asyncio
import json
import logging
from contextlib import asynccontextmanager

import aio_pika

from penumbra import metrics, models

logger = logging.getLogger(__name__)

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
        recovery_timeout=10.0,
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
        :param recovery_timeout: seconds to let aio-pika restore a dead channel
            before giving up on it and rebuilding from scratch
        """
        self.amqp_url = amqp_url
        self.queue_name = queue_name
        self.exchange_name = exchange_name
        self.routing_key = routing_key
        self.prefetch_count = prefetch_count
        self.publish_timeout = publish_timeout
        self.connect_timeout = connect_timeout
        self.recovery_timeout = recovery_timeout
        self.connection = None
        self.exchange = None
        self.queue = None
        # `connect` is awaited concurrently by every in-flight publish, so the
        # check-then-create below has to be serialised or we open one connection
        # per racing caller.
        self._connect_lock = asyncio.Lock()

    def is_usable(self) -> bool:
        """
        True when the cached topology can actually carry a message.

        `connection.is_closed` is not enough, on two counts. A
        `RobustConnection` reports `is_closed == False` for the entire time it is
        retrying a dropped connection, and a channel can be closed by the broker
        -- a `consumer_timeout`, or a `PRECONDITION_FAILED` from an ack that
        arrived late -- while the connection stays up and healthy. Either way the
        cached `queue` and `exchange` are dead objects, and a guard that only
        looks at the connection hands them back for the life of the process:
        every publish then fails with `ChannelInvalidStateError` and every ack
        with it, while the process stays up and looks fine.
        """
        if self.connection is None or self.connection.is_closed:
            return False
        if self.queue is None or self.exchange is None:
            return False
        for channel in (self.queue.channel, self.exchange.channel):
            if channel is None or channel.is_closed:
                return False
        return True

    async def _await_recovery(self) -> bool:
        """
        Give aio-pika's own restoration a bounded chance to finish. True if it did.

        A `RobustChannel` reopens itself after the broker closes it, and a
        `RobustConnection` re-declares the whole topology after reconnecting, so
        the usual answer to a dead channel is simply to wait a moment. Tearing
        the connection down on every transient close would churn connections and
        race the library's own recovery.

        Bounded because that recovery is not guaranteed. `RobustChannel._on_close`
        skips the restore entirely if a previous restore left it part-way
        (`__restored` cleared), and nothing clears that state again on its own --
        so the wait has to end in a rebuild rather than in more waiting.
        """
        try:
            async with asyncio.timeout(self.recovery_timeout):
                for channel in (self.queue.channel, self.exchange.channel):
                    ready = getattr(channel, "ready", None)
                    if ready is not None:
                        await ready()
        # Broad on purpose: this is a probe, and its answer is the return value.
        # Whatever went wrong, the caller's next move is to rebuild. CancelledError
        # is a BaseException, so an enclosing deadline still aborts the wait.
        except Exception:
            return False
        return self.is_usable()

    async def _rebuild(self) -> None:
        """
        Build a fresh connection and topology, discarding anything cached.

        Callers hold `_connect_lock`.
        """
        stale, self.connection, self.queue, self.exchange = (
            self.connection,
            None,
            None,
            None,
        )
        if stale is not None:
            # Closed rather than simply dropped: a RobustConnection owns a
            # reconnect task that outlives every reference to it, so abandoning
            # one leaks a task that goes on retrying forever behind the
            # connection replacing it.
            await self._discard(stale)

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
            queue = await consume_channel.declare_queue(self.queue_name, durable=True)

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
        # `is_usable` would then report a healthy client, and every later
        # publish would fail for the life of the process.
        self.connection = connection
        self.queue = queue
        self.exchange = exchange
        # Set here rather than straight after `connect_robust`, so the
        # gauge means "usable", not just "socket open".
        metrics.penumbra_broker_connected.set(1)
        logger.info(
            "AMQP topology ready: consuming %s (prefetch %d), publishing to %s.",
            self.queue_name,
            self.prefetch_count,
            self.exchange_name,
        )

    async def connect(self):
        async with self._connect_lock:
            if self.connection is None:
                # First call, or a previous attempt that cached nothing. There is
                # no topology to recover, so go straight to building one.
                await self._rebuild()
            elif not self.is_usable():
                # The gauge is what distinguishes "up but not consuming" from an
                # idle queue.
                metrics.penumbra_broker_connected.set(0)
                logger.warning(
                    "Cached AMQP topology for queue %s is no longer usable "
                    "(connection up: %s). Waiting up to %ss for aio-pika to "
                    "restore it.",
                    self.queue_name,
                    self.connection is not None and not self.connection.is_closed,
                    self.recovery_timeout,
                )
                if await self._await_recovery():
                    logger.info(
                        "AMQP topology for queue %s restored without a rebuild.",
                        self.queue_name,
                    )
                    metrics.penumbra_broker_connected.set(1)
                else:
                    logger.error(
                        "AMQP topology for queue %s did not come back within %ss; "
                        "rebuilding the connection. Left alone this is the state "
                        "that stops an instance consuming indefinitely while the "
                        "process stays up.",
                        self.queue_name,
                        self.recovery_timeout,
                    )
                    metrics.penumbra_amqp_topology_rebuilds.inc(1)
                    await self._rebuild()

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
            # `json.dumps`, not `str`. Heritrix parses these with org.json, whose
            # tokeniser is lenient enough to accept a Python dict repr, however,
            # it does not accept `None`: it special-cases only
            # true/false/null, so a bare `None` falls through to being read as the
            # *string* "None", and AMQPUrlReceiver.populateHeritableMetadata then
            # tags the URL with a source of "None" rather than leaving it unset.
            # Strict JSON is a subset of what that parser accepts, so this stays
            # compatible while fixing the null case and the escaping with it.
            await exchange.publish(
                aio_pika.Message(
                    body=json.dumps(umbra_response.asdict()).encode(),
                    content_type="application/json",
                ),
                routing_key=routing_key,
            )

    async def close_connection(self) -> None:
        if self.connection and not self.connection.is_closed:
            await self.connection.close()
