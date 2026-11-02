"""Keeper folder tools.

Read-only in v1 by design (docs/tickets/PRD-21235.md, Non-goals): folder
create/update/delete are deferred, not refused -- the ticket asks for live
access to secrets, not vault-structure administration.

Tool naming convention: keeper_<action>_<resource>
"""

import json
from collections.abc import Callable

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from .. import keeper_client
from ..errors import ToolError


def register(mcp: FastMCP, client_factory: Callable[[], str | None]) -> None:
    @mcp.tool(
        annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True),
    )
    async def keeper_list_folders() -> str:
        """List all folders visible to this vault's bound configuration."""
        try:
            client = keeper_client.build_client(client_factory())
            raw_folders = await keeper_client.call_with_retry(
                client.get_folders, op="get_folders"
            )
            folders = [
                {
                    "uid": f.uid,
                    "name": f.name,
                    "parent_uid": f.parent_uid or None,
                }
                for f in raw_folders
            ]
            return json.dumps({"folders": folders}, separators=(",", ":"), ensure_ascii=False)
        except ToolError as exc:
            return exc.to_json()
