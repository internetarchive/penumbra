import json
from unittest.mock import AsyncMock, MagicMock, patch

import aio_pika
import pytest
from playwright.async_api import async_playwright
from prometheus_client import REGISTRY

from penumbra.queues import AsyncMessageClient
from penumbra.worker import Settings, process_page


def message_maker(url: str) -> MagicMock:
    message = MagicMock(spec=aio_pika.IncomingMessage)
    message.body = json.dumps(
        {
            "url": url,
            "metadata": {
                "heritableData": {
                    "source": "test",
                    "heritable": ["source", "heritable"],
                }
            },
            "clientId": "urls",
        }
    ).encode()
    return message


@pytest.mark.asyncio
async def test_process_page(monkeypatch):
    async with async_playwright() as playwright:
        # Launch a headless browser (you can set headless=False to see the browser in action)
        browser = await playwright.chromium.launch(headless=True)

        # Use a real URL for testing
        test_url = "https://example.com"
        message = message_maker(test_url)

        # Mock the publish_message method of the AsyncMessageClient class
        with patch.object(
            AsyncMessageClient, "publish_message", new_callable=AsyncMock
        ) as mock_publish_message:
            # Call the process_page function with the real browser instance and test URL
            client = AsyncMessageClient()
            await process_page(client, browser, message)

            # Assert that publish_message was called with the expected arguments
            assert mock_publish_message.called
            requested_docs_pre = REGISTRY.get_sample_value(
                "penumbra_resources_requested_total", {"resource_type": "document"}
            )
            fetched_docs_pre = REGISTRY.get_sample_value(
                "penumbra_resources_fetched_total", {"resource_type": "document"}
            )
            monkeypatch.setenv("penumbra_skip_resource_document", "1")
            settings = Settings()
            assert settings.skip_resource_document
            await process_page(client, browser, message)
            requested_docs_post = REGISTRY.get_sample_value(
                "penumbra_resources_requested_total", {"resource_type": "document"}
            )
            fetched_docs_post = REGISTRY.get_sample_value(
                "penumbra_resources_fetched_total", {"resource_type": "document"}
            )
            assert requested_docs_pre < requested_docs_post
            assert fetched_docs_pre == fetched_docs_post
        # Close the browser at the end of the test
        await browser.close()
