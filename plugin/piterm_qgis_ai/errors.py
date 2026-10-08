"""Error taxonomy for the Piterm Bridge wire protocol.

Every failure that crosses the wire is a structured error:

    {"ok": false, "error": {"code": "E_...", "message": "...", ...}}

``CommandError`` is raised by handlers; the dispatcher catches it (and anything
else) and converts it into the wire form. Stdlib-only.
"""

from __future__ import annotations


class ErrorCode:
    """Stable, greppable error codes (never translated, never reused)."""

    BAD_REQUEST = "E_BAD_REQUEST"            # malformed envelope / params
    PARSE = "E_PARSE"                        # JSON / frame decode failure
    UNKNOWN_COMMAND = "E_UNKNOWN_COMMAND"
    AUTH = "E_AUTH"                          # missing / invalid token
    AUTH_LOCKED = "E_AUTH_LOCKED"            # too many failures on connection
    FORBIDDEN = "E_FORBIDDEN"                # scope / policy refusal (read-only, deny-list)
    CONFIRM_REQUIRED = "E_CONFIRM_REQUIRED"  # destructive op needs params.__confirm
    NOT_FOUND = "E_NOT_FOUND"                # layer / layout / job / checkpoint missing
    AMBIGUOUS = "E_AMBIGUOUS"                # name matches multiple layers
    STATE = "E_STATE"                        # wrong state (not editable, no project...)
    INVALID = "E_INVALID"                    # semantic validation failure
    GEOMETRY = "E_GEOMETRY"                  # geometry parse / type mismatch
    CRS = "E_CRS"                            # unknown / untransformable CRS
    EXPRESSION = "E_EXPRESSION"              # QGIS expression invalid
    IO = "E_IO"                              # filesystem / datasource failure
    PATH_DENIED = "E_PATH_DENIED"            # outside allow-list or inside deny-list
    TIMEOUT = "E_TIMEOUT"                    # plugin-side deadline exceeded
    BUSY = "E_BUSY"                          # too many clients / rate limited
    PAYLOAD = "E_PAYLOAD"                    # message too large
    DISPATCH_GUARD = "E_DISPATCH_GUARD"      # re-entrant dispatch refused
    CANCELLED = "E_CANCELLED"                # job cancelled
    INTERNAL = "E_INTERNAL"                  # unexpected exception (with traceback detail)
    EXEC = "E_EXEC"                          # user code raised / sandbox violation


class CommandError(Exception):
    """A structured error raised by command handlers."""

    def __init__(self, code, message, detail=None, retryable=False, http_status=400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail
        self.retryable = retryable
        self.http_status = http_status

    def to_wire(self):
        out = {"code": self.code, "message": self.message}
        if self.detail is not None:
            out["detail"] = self.detail
        if self.retryable:
            out["retryable"] = True
        return out


def bad_request(message, **kw):
    return CommandError(ErrorCode.BAD_REQUEST, message, **kw)


def not_found(message, **kw):
    return CommandError(ErrorCode.NOT_FOUND, message, **kw)


def invalid(message, **kw):
    return CommandError(ErrorCode.INVALID, message, **kw)


def forbidden(message, **kw):
    return CommandError(ErrorCode.FORBIDDEN, message, **kw)


def path_denied(message, **kw):
    return CommandError(ErrorCode.PATH_DENIED, message, **kw)


def state(message, **kw):
    return CommandError(ErrorCode.STATE, message, **kw)


def io_error(message, **kw):
    return CommandError(ErrorCode.IO, message, **kw)


def internal(message, detail=None):
    return CommandError(ErrorCode.INTERNAL, message, detail=detail)
