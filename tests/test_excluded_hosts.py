import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from prometheus_client import REGISTRY
from pydantic import ValidationError

from penumbra import worker
from penumbra.models import Settings, UmbraMessage
from penumbra.queues import AsyncMessageClient
from penumbra.worker import excluded_host, publish_umbra_response
from tests.test_process_page import message_maker


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("www.googletagmanager.com,www.google-analytics.com", id="comma"),
        pytest.param("www.googletagmanager.com www.google-analytics.com", id="space"),
        pytest.param(
            "\n  www.googletagmanager.com,\n  www.google-analytics.com,\n",
            id="multiline-trailing-comma",
        ),
        pytest.param(
            "WWW.GoogleTagManager.com. , www.google-analytics.com", id="case-and-dot"
        ),
    ],
)
def test_hosts_are_read_from_the_environment(monkeypatch, value):
    monkeypatch.setenv("PENUMBRA_EXCLUDED_OUTLINK_HOSTS", value)
    assert Settings().excluded_outlink_hosts == {
        "www.googletagmanager.com",
        "www.google-analytics.com",
    }


@pytest.mark.parametrize("value", ["", "  ", " , "])
def test_blank_hosts_exclude_nothing(monkeypatch, value):
    """An empty templated env var means no exclusions, not a host named ''."""
    monkeypatch.setenv("PENUMBRA_EXCLUDED_OUTLINK_HOSTS", value)
    assert Settings().excluded_outlink_hosts == frozenset()


def test_hosts_default_to_none(monkeypatch):
    monkeypatch.delenv("PENUMBRA_EXCLUDED_OUTLINK_HOSTS", raising=False)
    assert Settings().excluded_outlink_hosts == frozenset()


@pytest.mark.parametrize(
    "entry",
    [
        "https://www.googletagmanager.com",
        "www.googletagmanager.com/gtm.js",
        "www.googletagmanager.com:443",
    ],
)
def test_a_url_instead_of_a_host_fails_at_startup(monkeypatch, entry):
    """Each of these would otherwise match nothing, and nobody would notice."""
    monkeypatch.setenv("PENUMBRA_EXCLUDED_OUTLINK_HOSTS", f"example.org,{entry}")
    with pytest.raises(ValidationError, match="bare hostnames"):
        Settings()


@pytest.fixture
def excluded(monkeypatch):
    hosts = frozenset({"www.googletagmanager.com", "connect.facebook.net"})
    monkeypatch.setattr(worker.settings, "excluded_outlink_hosts", hosts)
    return hosts


@pytest.mark.parametrize(
    "url, host",
    [
        (
            "https://www.googletagmanager.com/gtm.js?id=GTM-X",
            "www.googletagmanager.com",
        ),
        ("http://WWW.GOOGLETAGMANAGER.COM/gtm.js", "www.googletagmanager.com"),
        ("https://www.googletagmanager.com:8443/gtm.js", "www.googletagmanager.com"),
        ("https://www.googletagmanager.com./gtm.js", "www.googletagmanager.com"),
        ("https://connect.facebook.net/en_US/fbevents.js", "connect.facebook.net"),
        # Exact match only: neither the parent domain nor a sibling.
        ("https://googletagmanager.com/gtm.js", None),
        ("https://tags.www.googletagmanager.com/gtm.js", None),
        # The host, not anywhere else in the URL.
        ("https://example.com/?next=https://www.googletagmanager.com/", None),
        ("https://example.com/www.googletagmanager.com", None),
        # Unparseable: left to Heritrix to judge, as before.
        ("http://[::1/", None),
        ("data:text/plain,hello", None),
    ],
)
def test_excluded_host(excluded, url, host):
    assert excluded_host(url) == host


def test_nothing_is_excluded_by_default(monkeypatch):
    monkeypatch.setattr(worker.settings, "excluded_outlink_hosts", frozenset())
    assert excluded_host("https://www.googletagmanager.com/gtm.js") is None


@pytest.mark.asyncio
async def test_publish_drops_excluded_hosts(excluded):
    parent = UmbraMessage(json.loads(message_maker("https://example.com").body))
    kept = "https://example.com/about"
    dropped = "https://www.googletagmanager.com/gtm.js?id=GTM-X"

    def count() -> float:
        return (
            REGISTRY.get_sample_value(
                "penumbra_urls_dropped_excluded_host_total",
                {"host": "www.googletagmanager.com"},
            )
            or 0
        )

    before = count()
    client = MagicMock(spec=AsyncMessageClient)
    client.publish_message = AsyncMock()
    await publish_umbra_response(client, parent, {kept, dropped})

    assert [c.args[0].url for c in client.publish_message.await_args_list] == [kept]
    assert count() == before + 1
    # Never published, so not remembered as sent either.
    assert (parent.client_id, dropped) not in worker.recently_sent
