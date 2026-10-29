import os
from dataclasses import dataclass, field
from typing import Final

from heliumedu.client import DEFAULT_API_URL

#: Where the Helium web app is (links in tool results point here).
DEFAULT_APP_URL: Final = "https://app.heliumedu.com"
#: Where the connector itself is served.
DEFAULT_CONNECTOR_URL: Final = "https://mcp.heliumedu.com"
#: The path the MCP endpoint is served at.
MCP_PATH: Final = "/mcp"
#: The scopes clients are told to ask for: reading the planner, and importing a term.
SCOPES: Final = ("planner:read", "planner:import")


@dataclass(frozen=True)
class Settings:
    """
    The connector's configuration, read from the environment.
    """

    #: Where Helium's API is.
    api_url: str = DEFAULT_API_URL
    #: Where the Helium web app is.
    app_url: str = DEFAULT_APP_URL
    #: Where the connector is served (its MCP endpoint is this plus ``/mcp``).
    connector_url: str = DEFAULT_CONNECTOR_URL
    #: The OAuth authorization server that issues the connector's tokens (Helium's API unless set).
    issuer_url: str = DEFAULT_API_URL
    #: The ``Host`` values the MCP endpoint accepts; empty turns off DNS rebinding protection, which is for servers
    #: on a local address, not one behind CloudFront.
    allowed_hosts: list[str] = field(default_factory=list)
    #: The secret CloudFront sends as ``X-Origin-Secret``; when set, requests without it are refused, so the public
    #: function URL can't be used around CloudFront.
    origin_secret: str | None = None
    #: Where errors are reported, if anywhere.
    sentry_dsn: str | None = None
    #: The deployment's name, for error reports.
    environment: str = "prod"

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
        api_url = os.environ.get("HELIUM_API_URL", DEFAULT_API_URL).rstrip("/")
        hosts = os.environ.get("HELIUM_ALLOWED_HOSTS", "")
        return cls(api_url=api_url,
                   app_url=os.environ.get("HELIUM_APP_URL", DEFAULT_APP_URL).rstrip("/"),
                   connector_url=os.environ.get("HELIUM_CONNECTOR_URL", DEFAULT_CONNECTOR_URL).rstrip("/"),
                   issuer_url=os.environ.get("HELIUM_ISSUER_URL", api_url).rstrip("/"),
                   allowed_hosts=[host.strip() for host in hosts.split(",") if host.strip()],
                   origin_secret=os.environ.get("HELIUM_ORIGIN_SECRET") or None,
                   sentry_dsn=os.environ.get("HELIUM_SENTRY_DSN") or None,
                   environment=os.environ.get("HELIUM_ENVIRONMENT", "prod"))
