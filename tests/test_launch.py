import logging
import os
from unittest.mock import AsyncMock, MagicMock

import pytest
from playwright.async_api import async_playwright

from penumbra import worker
from penumbra.models import Settings
from penumbra.worker import launch_browser


@pytest.mark.parametrize("value", ["", "   "])
def test_blank_executable_path_means_bundled(monkeypatch, value):
    """An empty templated env var must not be handed to Playwright as a path."""
    monkeypatch.setenv("PENUMBRA_CHROMIUM_EXECUTABLE_PATH", value)
    assert Settings().chromium_executable_path is None


def test_executable_path_is_read_from_the_environment(monkeypatch):
    monkeypatch.setenv(
        "PENUMBRA_CHROMIUM_EXECUTABLE_PATH", "/opt/chrome/chrome-headless-shell"
    )
    assert Settings().chromium_executable_path == "/opt/chrome/chrome-headless-shell"


@pytest.mark.asyncio
@pytest.mark.parametrize("path", [None, "/opt/chrome/chrome-headless-shell"])
async def test_launch_browser_passes_the_executable_path(monkeypatch, path):
    monkeypatch.setattr(worker.settings, "chromium_executable_path", path)
    pw = MagicMock()
    pw.chromium.launch = AsyncMock(return_value=MagicMock(version="156.0.8078.80"))

    await launch_browser(pw)

    pw.chromium.launch.assert_awaited_once_with(executable_path=path)


@pytest.mark.asyncio
async def test_launch_browser_runs_the_binary_it_is_pointed_at(monkeypatch, caplog):
    """End to end against a real binary, and the log names its version."""
    async with async_playwright() as pw:
        path = pw.chromium.executable_path
        if not os.path.exists(path):
            pytest.skip(f"no Chromium installed at {path}")
        monkeypatch.setattr(worker.settings, "chromium_executable_path", path)

        with caplog.at_level(logging.INFO, logger="penumbra.worker"):
            browser = await launch_browser(pw)
        try:
            assert browser.version
            assert f"Launched Chromium {browser.version} from {path}" in caplog.text
        finally:
            await browser.close()
