"""Public authentication failures; never include credentials or upstream errors."""


class AuthenticationError(Exception):
    """An authentication failure that can safely cross the HTTP boundary."""

    def __init__(
        self, status_code: int, code: str, message: str, challenge: str | None = None
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.challenge = challenge


class AuthenticationRequired(AuthenticationError):
    def __init__(self) -> None:
        super().__init__(
            401,
            "authentication_required",
            "Sign in to use this service.",
            'Bearer realm="wright-arctic"',
        )


class InvalidCredentials(AuthenticationError):
    def __init__(self) -> None:
        super().__init__(
            401,
            "invalid_token",
            "The access token is invalid or expired. Renew it and try again.",
            'Bearer realm="wright-arctic", error="invalid_token"',
        )


class AuthenticationUnavailable(AuthenticationError):
    def __init__(self) -> None:
        super().__init__(
            503,
            "authentication_unavailable",
            "Authentication is temporarily unavailable. Try again shortly.",
        )
