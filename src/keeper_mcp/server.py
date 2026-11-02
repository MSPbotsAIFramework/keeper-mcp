from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from .config import Settings
from .middleware import get_keeper_config

SERVICE_INSTRUCTIONS = (
    "Keeper Secrets Manager: read and manage the secrets, fields, and folders "
    "in a tenant's own Keeper vault. Typical flow: keeper_list_secrets or "
    "keeper_list_folders to discover what exists, then keeper_get_secret or "
    "keeper_get_secret_field for a specific value. keeper_create_secret / "
    "keeper_update_secret / keeper_delete_secret mutate the vault and should "
    "only be used on explicit, named records -- there is no bulk or "
    "folder-wide delete. Secret values are returned in full by design; this "
    "service does not mask or redact them."
)


def create_mcp_server(settings: Settings) -> FastMCP:
    """Build the FastMCP server instance and register all tools.

    `stateless_http=True` is required (not just "no session stickiness") --
    the MCP Python SDK's default session manager rejects any request that
    doesn't start with an `initialize` handshake (SOP §1.3). DNS-rebinding
    protection is disabled because this service is never exposed directly to
    the internet; it only ever receives proxied calls from the Gateway
    (SOP §7).
    """
    mcp = FastMCP(
        name="keeper-mcp",
        instructions=SERVICE_INSTRUCTIONS,
        stateless_http=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    def client_factory() -> str | None:
        """Return the current request's bound KSM config (base64), or None."""
        return get_keeper_config()

    from .tools import folders, secrets

    secrets.register(mcp, client_factory)
    folders.register(mcp, client_factory)

    return mcp
