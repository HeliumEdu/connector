import asyncio
import logging
from contextlib import AsyncExitStack
from typing import Any

from mangum import Mangum

from helium_connector.app import create_app
from helium_connector.settings import Settings

logging.getLogger().setLevel(logging.INFO)
# Request URLs carry students' search text
logging.getLogger("httpx").setLevel(logging.WARNING)

app = create_app(Settings.from_env())

# The MCP session manager can start only once per process, but Mangum would run the app's lifespan on every
# invocation, so the lifespan is entered once per container, on a loop that outlives each invocation.
_loop = asyncio.new_event_loop()
asyncio.set_event_loop(_loop)
_lifespan = AsyncExitStack()
_loop.run_until_complete(_lifespan.enter_async_context(app.router.lifespan_context(app)))

_mangum = Mangum(app, lifespan="off")


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """
    The Lambda entrypoint, for a Function URL event.

    :param event: The invocation event.
    :param context: The invocation context.
    :return: The HTTP response.
    """
    return _mangum(event, context)
