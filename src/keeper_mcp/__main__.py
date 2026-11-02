import sys

from .config import get_settings
from .middleware import GatewayCredentialsMiddleware
from .server import create_mcp_server


def build_app(mcp, settings):
    """Wrap the FastMCP Starlette app with a local-only /health route.

    `/health` must never depend on the Keeper API (SOP §1.1): it is a pure
    liveness probe for the container orchestrator, and Keeper being slow or
    down must not cause this service to be restarted.
    """
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    from starlette.routing import Mount, Route

    async def health(_: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    mcp_app = mcp.streamable_http_app()  # Starlette app owning the session-manager lifespan
    mounted = GatewayCredentialsMiddleware(mcp_app, settings)

    # Mount() does not run a sub-app's lifespan, so the streamable-http session
    # manager's task group would never start ("Task group is not initialized").
    # Propagate the MCP app's lifespan to the outer app explicitly (SOP §7).
    return Starlette(
        routes=[Route("/health", health), Mount("/", app=mounted)],
        lifespan=lambda app: mcp_app.router.lifespan_context(app),
    )


def main() -> None:
    settings = get_settings()
    mcp = create_mcp_server(settings)

    import uvicorn

    app = build_app(mcp, settings)
    print(
        f"keeper-mcp listening on http://{settings.mcp_http_host}:{settings.mcp_http_port}/mcp",
        file=sys.stderr,
    )
    uvicorn.run(app, host=settings.mcp_http_host, port=settings.mcp_http_port)


if __name__ == "__main__":
    main()
