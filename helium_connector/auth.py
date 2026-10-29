import hashlib
import logging
import time
from collections import OrderedDict
from typing import Any, Final

import httpx
from mcp.server.auth.provider import AccessToken

from helium_connector import __version__
from heliumedu.client import HeliumClient
from heliumedu.exception import HeliumAuthError

logger = logging.getLogger(__name__)

#: How long a verified token's user is reused before Helium is asked again, in seconds.
USER_CACHE_SECONDS: Final = 60
#: The most verified tokens remembered at once.
USER_CACHE_SIZE: Final = 1000
#: How the connector names itself to Helium, after the client package.
AGENT: Final = f"helium-connector/{__version__}"


class HeliumTokenVerifier:
    """
    Verifies bearer tokens by asking Helium who they belong to. Helium also checks each token's scopes on every
    request the connector makes with it, so the connector holds no keys and checks no signatures.
    """

    def __init__(self, api_url: str, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._api_url = api_url
        self._transport = transport
        self._users: OrderedDict[str, tuple[float, dict[str, Any]]] = OrderedDict()

    async def verify_token(self, token: str) -> AccessToken | None:
        """
        Accept a token Helium accepts.

        :param token: The bearer token.
        :return: The token's access info, or ``None`` when Helium refuses it (so the client re-authorizes).
        :raises: :class:`~heliumedu.exception.HeliumError`: When Helium can't be reached, so an outage isn't
            mistaken for a revoked token.
        """
        try:
            user = await self.user_for(token)
        except HeliumAuthError:
            return None
        return AccessToken(token=token, client_id="helium", scopes=[], subject=str(user["id"]))

    async def user_for(self, token: str) -> dict[str, Any]:
        """
        The user a token belongs to, from the cache when it was verified recently.

        :param token: The bearer token.
        :return: The user, as ``GET /auth/user/`` returns it.
        :raises: :class:`~heliumedu.exception.HeliumError`: When Helium refuses the token or can't be reached.
        """
        key = hashlib.sha256(token.encode("utf-8")).hexdigest()
        cached = self._users.get(key)
        if cached and cached[0] > time.monotonic():
            self._users.move_to_end(key)
            return cached[1]

        async with httpx.AsyncClient(transport=self._transport) as session:
            user = await HeliumClient(session, token, self._api_url, agent=AGENT).get_user()

        self._users[key] = (time.monotonic() + USER_CACHE_SECONDS, user)
        self._users.move_to_end(key)
        while len(self._users) > USER_CACHE_SIZE:
            self._users.popitem(last=False)
        return user
