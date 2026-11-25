from typing import Any


class HeliumError(Exception):
    """
    Raised when a general Helium API error has occurred.
    """

    def __init__(self,
                 error: str | BaseException,
                 meta: dict[str, Any] | None = None) -> None:
        super(HeliumError, self).__init__(error)

        #: Metadata for context around the error that was raised.
        self.meta: dict[str, Any] | None = meta


class HeliumAuthError(HeliumError):
    """
    Raised when Helium doesn't accept the token (it's wrong, expired, or revoked).
    """
    pass


class HeliumPermissionError(HeliumError):
    """
    Raised when the token is valid but wasn't granted access to what was asked for.
    """
    pass


class HeliumValidationError(HeliumError):
    """
    Raised when Helium rejects the request's data. ``meta["errors"]`` is Helium's error body.
    """
    pass


class HeliumNotFoundError(HeliumError):
    """
    Raised when what was asked for doesn't exist (or isn't the token user's). ``meta["errors"]`` is Helium's error
    body.
    """
    pass


class HeliumRateLimitedError(HeliumError):
    """
    Raised when Helium asks for fewer requests. ``meta["errors"]`` is Helium's error body.
    """
    pass


class HeliumConnectionError(HeliumError):
    """
    Raised when Helium can't be reached (the network, a timeout, or an error on Helium's side).
    """
    pass


class HeliumServerError(HeliumConnectionError):
    """
    Raised when Helium answers with an error on its side (a 5xx).
    """
    pass
