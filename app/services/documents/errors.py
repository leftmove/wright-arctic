"""Safe document failures, independent of SQL driver messages."""


class DocumentError(Exception):
    def __init__(self, code: str, message: str, status: int) -> None:
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


class Unavailable(DocumentError):
    def __init__(self) -> None:
        super().__init__(
            "documents_unavailable", "Document storage is unavailable.", 503
        )


class NotFound(DocumentError):
    def __init__(self) -> None:
        super().__init__("document_not_found", "Document is unavailable.", 404)


class Forbidden(DocumentError):
    def __init__(self) -> None:
        super().__init__("document_forbidden", "This operation is not permitted.", 403)


class Conflict(DocumentError):
    def __init__(self, code: str = "document_conflict") -> None:
        super().__init__(
            code,
            "The document changed or the operation ID was reused. Refresh and retry.",
            409,
        )


class InvalidNative(DocumentError):
    def __init__(self, code: str = "invalid_native_state") -> None:
        super().__init__(code, "The native document cannot be accepted.", 422)


class TooLarge(DocumentError):
    def __init__(self) -> None:
        super().__init__(
            "document_too_large", "Document request exceeds its size limit.", 413
        )
