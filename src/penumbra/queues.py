from contextlib import asynccontextmanager

import aio_pika

from penumbra import models


class AsyncMessageClient:
    def __init__(
        self,
        amqp_url="amqp://guest:guest@localhost:5672/%2f",
        queue_name="urls",
        exchange_name="umbra",
        routing_key="urls",
    ):
        """
        AsyncMessageClient

        :param amqp_url:
        :param queue_name:
        :param exchange_name:
        :param routing_key:
        """
        self.amqp_url = amqp_url
        self.queue_name = queue_name
        self.exchange_name = exchange_name
        self.routing_key = routing_key
        self.connection = None
        self.exchange = None
        self.queue = None

    async def connect(self):
        if self.connection is None or self.connection.is_closed:
            self.connection = await aio_pika.connect_robust(self.amqp_url)
            channel = await self.connection.channel()
            self.queue = await channel.declare_queue(self.queue_name, durable=True)
            self.exchange = await channel.declare_exchange(
                self.exchange_name, type=aio_pika.ExchangeType.DIRECT, durable=True
            )
            await self.queue.bind(self.exchange, routing_key=self.routing_key)

        return self.queue, self.exchange

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
        Publish an UmbraResponse message to the specified exchange
        """
        # Get the cached connection
        queue, exchange = await self.connect()

        # Publish the message
        routing_key = umbra_response.client_id
        await exchange.publish(
            aio_pika.Message(body=str(umbra_response.asdict()).encode()),
            routing_key=routing_key,
        )

    async def close_connection(self) -> None:
        if self.connection and not self.connection.is_closed:
            await self.connection.close()
