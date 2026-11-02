import contextvars

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from .config import Settings

# Per-request credential contextvar (SOP §3.3). Set by GatewayCredentialsMiddleware
# before the MCP handler runs and reset in `finally`. asyncio copies the context
# per task, so concurrent requests from different tenants never observe each
# other's value here — this is the mechanism AC7/AC9/TC9 depend on.
_keeper_config_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "keeper_config_b64", default=None
)


def get_keeper_config() -> str | None:
    """Return the current request's bound KSM config (base64), or None."""
    return _keeper_config_var.get()


class GatewayCredentialsMiddleware:
    """ASGI middleware: reads X-Keeper-Config and stores it in a ContextVar.

    Missing header on an /mcp request -> 401 with `required_headers` (SOP §3.2).
    No fallback to an environment variable exists anywhere in this service
    (SOP §3.1) — a request with no header simply has no credential available.
    """

    def __init__(self, app: ASGIApp, settings: Settings):
        self.app = app
        self.settings = settings
        self._header_name = settings.keeper_config_header.lower()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if not path.startswith("/mcp"):
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        # Header lookup is case-insensitive in Starlette.
        config_b64 = request.headers.get(self._header_name)

        if not config_b64:
            response = JSONResponse(
                {
                    "error": "Missing credentials",
                    "message": (
                        f"Missing required header: {self.settings.keeper_config_header}"
                    ),
                    "required_headers": [self.settings.keeper_config_header],
                },
                status_code=401,
            )
            await response(scope, receive, send)
            return

        token = _keeper_config_var.set(config_b64)
        try:
            await self.app(scope, receive, send)
        finally:
            _keeper_config_var.reset(token)
