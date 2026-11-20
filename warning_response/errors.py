"""Application error types.

Every error carries a machine-readable ``code`` so the CLI and the API can
report a stable reason separate from the human readable message.
"""

from __future__ import annotations


class WarningResponseError(Exception):
    """Base class for all application errors."""

    code = "error"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code

    def as_dict(self) -> dict:
        return {"code": self.code, "message": str(self)}


class InputDataError(WarningResponseError):
    """Input records are missing, malformed or internally inconsistent."""

    code = "input_data_error"


class NotFoundError(WarningResponseError):
    """A referenced record does not exist."""

    code = "not_found"


class ConflictError(WarningResponseError):
    """The operation is not allowed in the current state."""

    code = "conflict"


class StorageError(WarningResponseError):
    """The persisted store could not be read or written safely."""

    code = "storage_error"
