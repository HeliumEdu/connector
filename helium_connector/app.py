import hmac
import logging
from collections.abc import Awaitable, Callable

import httpx
from mcp.server.auth.routes import create_protected_resource_routes
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.datastructures import MutableHeaders
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response
from starlette.routing import Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from helium_connector import __version__
from helium_connector.server import DOCS_URL, create_server
from helium_connector.settings import MCP_PATH, OPENAI_APPS_CHALLENGE_PATH, SCOPES, Settings

logger = logging.getLogger(__name__)


def create_app(settings: Settings, transport: httpx.AsyncBaseTransport | None = None) -> Starlette:
    """
    Build the connector's ASGI app: the MCP endpoint, its OAuth protected resource metadata, a status check, and
    OpenAI's domain verification token when one is set.
    It's stateless and answers in JSON, so any instance can serve any request. A browser opening the connector's URL is
    sent to its documentation.

    :param settings: The connector's configuration.
    :param transport: How requests reach Helium (the network unless given).
    :return: The app.
    """
    if settings.sentry_dsn:
        import sentry_sdk
        from sentry_sdk.integrations.httpx import HttpxIntegration
        from sentry_sdk.integrations.httpx2 import Httpx2Integration

        # Frame locals and request URLs carry students' planner content and search text
        sentry_sdk.init(dsn=settings.sentry_dsn, environment=settings.environment, release=__version__,
                        send_default_pii=False, include_local_variables=False,
                        disabled_integrations=[HttpxIntegration(), Httpx2Integration()])

    server = create_server(settings, transport)
    security = TransportSecuritySettings(enable_dns_rebinding_protection=bool(settings.allowed_hosts),
                                         allowed_hosts=settings.allowed_hosts)
    app = server.streamable_http_app(streamable_http_path=MCP_PATH,
                                     json_response=True,
                                     stateless_http=True,
                                     transport_security=security,
                                     host="0.0.0.0")
    auth = server.settings.auth
    if auth is not None and auth.resource_server_url is not None:
        # Ahead of the SDK's own: its metadata lists only scopes every token must hold, and a stateless endpoint
        # answering in JSON has no event stream to offer a GET
        app.router.routes[:0] = [
            *create_protected_resource_routes(resource_url=auth.resource_server_url,
                                              authorization_servers=[auth.issuer_url],
                                              scopes_supported=list(SCOPES)),
            Route(MCP_PATH, _no_event_stream, methods=["GET"]),
        ]
    app.router.routes.append(Route("/", _docs, methods=["GET"]))
    app.router.routes.append(Route("/status", _status, methods=["GET"]))
    if settings.openai_apps_challenge:
        app.router.routes.append(Route(OPENAI_APPS_CHALLENGE_PATH, _answers(settings.openai_apps_challenge),
                                       methods=["GET"]))
    app.add_middleware(ScopeChallengeMiddleware)
    if settings.origin_secret:
        app.add_middleware(OriginSecretMiddleware, secret=settings.origin_secret)
    app.add_middleware(MuppetsMiddleware)
    return app


class OriginSecretMiddleware:
    """
    Refuses requests that don't carry the origin secret CloudFront adds, so only CloudFront can reach the app.
    """

    def __init__(self, app: ASGIApp, secret: str) -> None:
        self.app = app
        self.secret = secret.encode("utf-8")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            sent = dict(scope["headers"]).get(b"x-origin-secret", b"")
            if not hmac.compare_digest(sent, self.secret):
                await JSONResponse({"error": "forbidden"}, status_code=403)(scope, receive, send)
                return
        await self.app(scope, receive, send)


class ScopeChallengeMiddleware:
    """
    Names the scopes to ask for in a 401's challenge, so a client requests them instead of relying on defaults.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_scope(message: Message) -> None:
            if message["type"] == "http.response.start" and message["status"] == 401:
                headers = MutableHeaders(scope=message)
                challenge = headers.get("www-authenticate")
                if challenge:
                    headers["www-authenticate"] = f'{challenge}, scope="{" ".join(SCOPES)}"'
            await send(message)

        await self.app(scope, receive, send_with_scope)


class MuppetsMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_header(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message).append("X-Muppets", "42")
            await send(message)

        await self.app(scope, receive, send_with_header)


async def _no_event_stream(request: Request) -> Response:
    if _from_a_browser(request):
        return await _docs(request)
    return Response(status_code=405, headers={"Allow": "POST"})


async def _docs(request: Request) -> Response:
    return RedirectResponse(DOCS_URL, status_code=302)


def _from_a_browser(request: Request) -> bool:
    accept = request.headers.get("accept", "")
    return "text/html" in accept and "text/event-stream" not in accept


def _answers(text: str) -> Callable[[Request], Awaitable[Response]]:
    async def answer(request: Request) -> Response:
        return PlainTextResponse(text)

    return answer


async def _status(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "version": __version__})
