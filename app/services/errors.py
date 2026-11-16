"""Safe domain failures shared by the API and hosting adapters."""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


class ServiceError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        status: int = 409,
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.headers = headers or {}


class Unavailable(ServiceError):
    def __init__(self) -> None:
        super().__init__(
            "catalog_unavailable", "The metadata catalog is unavailable.", 503
        )


class NotFound(ServiceError):
    def __init__(self) -> None:
        super().__init__("not_found", "The requested resource is unavailable.", 404)


class Forbidden(ServiceError):
    def __init__(self) -> None:
        super().__init__("forbidden", "This operation is not permitted.", 403)


class Conflict(ServiceError):
    def __init__(self, code: str = "conflict") -> None:
        super().__init__(code, "The resource changed. Refresh and retry.")


async def _error(_request: Request, error: Exception) -> JSONResponse:
    if not isinstance(error, ServiceError):
        raise error
    return JSONResponse(
        {"code": error.code, "message": error.message},
        status_code=error.status,
        headers={"Cache-Control": "no-store", **error.headers},
    )


def install_service_error_handler(application: FastAPI) -> None:
    application.add_exception_handler(ServiceError, _error)
