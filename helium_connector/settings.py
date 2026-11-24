import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from helium_connector import __version__
from pyheliumedu.client import DEFAULT_API_URL

DEFAULT_APP_URL: Final = "https://app.heliumedu.com"
DEFAULT_CONNECTOR_URL: Final = "https://mcp.heliumedu.com"
WEBSITE_URL: Final = "https://www.heliumedu.com"
DOCS_URL: Final = f"{WEBSITE_URL}/support/getting-started/where-to-start-with-helium/#use-ai-as-a-scheduling-assistant"
ICON_URL: Final = f"{WEBSITE_URL}/icon-512.png"
MCP_PATH: Final = "/mcp"
WELL_KNOWN_DIR: Final = Path(__file__).resolve().parent / "well_known"
AGENT: Final = f"helium-connector/{__version__}"

USER_CACHE_SECONDS: Final = 60
USER_CACHE_SIZE: Final = 1000

DEFAULT_WINDOW_DAYS: Final = 7
MAX_WINDOW_DAYS: Final = 366
DEFAULT_PAGE_SIZE: Final = 25
MAX_PAGE_SIZE: Final = 100
DEFAULT_NOTES_PAGE_SIZE: Final = 5
MAX_NOTES_PAGE_SIZE: Final = 10
MAX_NOTE_CHARS: Final = 4000

MAX_ASSIGNMENTS: Final = 1000
MAX_EVENTS: Final = 500


def configure_logging() -> None:
    """
    Log at ``INFO``, but ``httpx`` only at ``WARNING``, since its request URLs carry students' search text.
    """
    logging.basicConfig(level=logging.INFO)
    logging.getLogger().setLevel(logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)


@dataclass(frozen=True)
class Settings:
    """
    The connector's configuration, read from the environment. The MCP endpoint is ``connector_url`` plus ``/mcp``.
    When ``origin_secret`` is set, requests that don't carry it as ``X-Origin-Secret`` (which CloudFront adds) are
    refused, so the public function URL can't be used around CloudFront.
    """

    api_url: str = DEFAULT_API_URL
    app_url: str = DEFAULT_APP_URL
    connector_url: str = DEFAULT_CONNECTOR_URL
    origin_secret: str | None = None

    @property
    def resource_url(self) -> str:
        """
        The MCP endpoint's URL, which is also the resource its tokens are issued for.
        """
        return f"{self.connector_url}{MCP_PATH}"

    @classmethod
    def from_env(cls) -> "Settings":
        """
        Read the configuration from ``HELIUM_*`` environment variables, defaulting to production.

        :return: The configuration.
        """
        return cls(api_url=os.environ.get("HELIUM_API_URL", DEFAULT_API_URL).rstrip("/"),
                   app_url=os.environ.get("HELIUM_APP_URL", DEFAULT_APP_URL).rstrip("/"),
                   connector_url=os.environ.get("HELIUM_CONNECTOR_URL", DEFAULT_CONNECTOR_URL).rstrip("/"),
                   origin_secret=os.environ.get("HELIUM_ORIGIN_SECRET") or None)
