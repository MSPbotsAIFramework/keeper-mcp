"""Structured error envelope (vendor-mcp-development SOP §4).

Tools never raise; they return this envelope (serialized to a compact JSON
string) as the tool result so an Agent can branch on `code` and decide
whether to retry.
"""

import json
from typing import Literal

ErrorCode = Literal[
    "not_configured",
    "unauthorized",
    "not_found",
    "invalid_argument",
    "rate_limited",
    "upstream_error",
]

# retryable=True only for the two codes that represent a transient upstream
# condition; everything else is a caller-side or permanent condition.
_RETRYABLE: dict[str, bool] = {
    "not_configured": False,
    "unauthorized": False,
    "not_found": False,
    "invalid_argument": False,
    "rate_limited": True,
    "upstream_error": True,
}


class ToolError(Exception):
    """Internal exception used to short-circuit a tool body to an envelope.

    Each tool catches this at its top level and converts it with `to_json()`.
    Keeping it as an exception (rather than threading error returns through
    every call site) keeps the tool bodies readable; it never crosses the
    tool boundary as a raised MCP error (SOP §4.1).
    """

    def __init__(self, code: ErrorCode, message: str):
        self.code = code
        self.message = message
        super().__init__(message)

    def to_json(self) -> str:
        return error_envelope(self.code, self.message)


def error_envelope(code: ErrorCode, message: str) -> str:
    """Serialize a structured error envelope as compact JSON (SOP §2.5/§4.2)."""
    payload = {
        "error": {
            "code": code,
            "message": message,
            "retryable": _RETRYABLE.get(code, False),
        }
    }
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
