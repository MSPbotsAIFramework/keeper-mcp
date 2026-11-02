"""Keeper secret (record) tools.

Tool naming convention: keeper_<action>_<resource>

Returning the secret value is this service's entire purpose (docs/tickets/
PRD-21235.md AC3) -- `keeper_get_secret` never masks field values. The write
tools only ever accept explicit resource IDs, never a folder-wide or
vault-wide bulk operation (AC6, Decisions made by DEV01).
"""

import json
from collections.abc import Callable
from typing import Any

from keeper_secrets_manager_core.dto.dtos import VALID_RECORD_FIELDS, RecordCreate, RecordField
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from .. import keeper_client
from ..errors import ToolError

DEFAULT_LIMIT = 50
MAX_LIMIT = 200
MAX_RESULT_CHARS = 20_000


def _secret_summary(record: Any) -> dict:
    """Project a Record down to list-view fields (uid/title/folder_uid/type).

    Field values are deliberately excluded here (SOP §2.5 token economy /
    AC1) -- full field values are only returned by keeper_get_secret.
    """
    return {
        "uid": record.uid,
        "title": record.title,
        "folder_uid": record.folder_uid or None,
        "type": record.type,
    }


def _record_fields(record: Any) -> list[dict]:
    fields = record.dict.get("fields") or []
    return [
        {
            "type": f.get("type"),
            "label": f.get("label"),
            "value": f.get("value", []),
        }
        for f in fields
        if isinstance(f, dict)
    ]


def register(mcp: FastMCP, client_factory: Callable[[], str | None]) -> None:
    @mcp.tool(
        annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True),
    )
    async def keeper_list_secrets(
        folder_uid: str | None = None,
        title_contains: str | None = None,
        limit: int = DEFAULT_LIMIT,
        offset: int = 0,
    ) -> str:
        """List secrets, optionally filtered by folder_uid or title_contains.

        Returns summaries only (uid/title/folder_uid/type), capped at limit
        (default 50, hard cap 200); use keeper_get_secret for field values.
        has_more is true if more results exist."""
        try:
            capped_limit = max(1, min(limit, MAX_LIMIT))
            capped_offset = max(0, offset)
            client = keeper_client.build_client(client_factory())
            records = await keeper_client.call_with_retry(
                client.get_secrets, op="get_secrets"
            )

            if folder_uid:
                records = [r for r in records if r.folder_uid == folder_uid]
            if title_contains:
                needle = title_contains.lower()
                records = [r for r in records if needle in (r.title or "").lower()]

            total = len(records)
            page = records[capped_offset : capped_offset + capped_limit]
            result = {
                "secrets": [_secret_summary(r) for r in page],
                "has_more": capped_offset + capped_limit < total,
            }
            return json.dumps(result, separators=(",", ":"), ensure_ascii=False)
        except ToolError as exc:
            return exc.to_json()

    @mcp.tool(
        annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True),
    )
    async def keeper_get_secret(uid: str) -> str:
        """Get a secret's full field list, including values, by uid.

        Returns not_found if the bound configuration cannot see this uid
        (unknown, or belongs to a different vault)."""
        try:
            client = keeper_client.build_client(client_factory())
            records = await keeper_client.call_with_retry(
                lambda: client.get_secrets(uids=[uid]), op="get_secrets"
            )
            if not records:
                raise ToolError("not_found", f"No secret visible for uid={uid}")
            record = records[0]
            result = {
                "uid": record.uid,
                "title": record.title,
                "type": record.type,
                "fields": _record_fields(record),
            }
            return json.dumps(result, separators=(",", ":"), ensure_ascii=False)
        except ToolError as exc:
            return exc.to_json()

    @mcp.tool(
        annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True),
    )
    async def keeper_get_secret_field(notation: str) -> str:
        """Get one field value via Keeper Notation, e.g. "uid/field/password".

        Prefer this over keeper_get_secret when only one value is needed, to
        keep the result small."""
        try:
            client = keeper_client.build_client(client_factory())
            try:
                value = await keeper_client.call_with_retry(
                    lambda: client.get_notation(notation), op="get_notation"
                )
            except ValueError as exc:
                raise ToolError("invalid_argument", f"notation: {exc}") from exc
            return json.dumps({"value": value}, separators=(",", ":"), ensure_ascii=False)
        except ToolError as exc:
            return exc.to_json()

    @mcp.tool(
        annotations=ToolAnnotations(destructiveHint=False, idempotentHint=False),
    )
    async def keeper_create_secret(
        folder_uid: str,
        title: str,
        fields: dict[str, Any],
        record_type: str = "login",
    ) -> str:
        """Create a new secret in an existing folder.

        fields keys are Keeper standard field type names (e.g. login,
        password, url; notes sets the record's notes, not a field). Returns
        invalid_argument naming the bad field or folder_uid."""
        try:
            record = RecordCreate(record_type, title)
            record_fields: list[RecordField] = []
            for field_type, value in fields.items():
                if field_type == "notes":
                    record.notes = str(value)
                    continue
                if field_type not in VALID_RECORD_FIELDS:
                    raise ToolError(
                        "invalid_argument",
                        f"fields.{field_type}: not a valid Keeper field type",
                    )
                record_fields.append(RecordField(field_type=field_type, value=value))
            record.fields = record_fields

            client = keeper_client.build_client(client_factory())
            new_uid = await keeper_client.call_with_retry(
                lambda: client.create_secret(folder_uid, record), op="create_secret"
            )
            return json.dumps({"uid": new_uid}, separators=(",", ":"), ensure_ascii=False)
        except ToolError as exc:
            return exc.to_json()

    @mcp.tool(
        annotations=ToolAnnotations(destructiveHint=True, idempotentHint=False),
    )
    async def keeper_update_secret(uid: str, fields: dict[str, Any]) -> str:
        """Partially update a secret's fields; only the named fields change.

        Fields already on the record but not named in fields are left
        untouched. Returns not_found if the bound configuration cannot see
        this uid."""
        try:
            for field_type in fields:
                if field_type not in VALID_RECORD_FIELDS:
                    raise ToolError(
                        "invalid_argument",
                        f"fields.{field_type}: not a valid Keeper field type",
                    )

            client = keeper_client.build_client(client_factory())
            records = await keeper_client.call_with_retry(
                lambda: client.get_secrets(uids=[uid]), op="get_secrets"
            )
            if not records:
                raise ToolError("not_found", f"No secret visible for uid={uid}")
            record = records[0]

            existing_fields = record.dict.setdefault("fields", [])
            for field_type, value in fields.items():
                value_list = value if isinstance(value, list) else [value]
                existing = next(
                    (f for f in existing_fields if f.get("type") == field_type), None
                )
                if existing is not None:
                    existing["value"] = value_list
                else:
                    existing_fields.append({"type": field_type, "value": value_list})
            record._update()  # noqa: SLF001 -- same pattern the SDK's own setters use

            await keeper_client.call_with_retry(lambda: client.save(record), op="save")
            result = {"uid": uid, "updated_fields": list(fields.keys())}
            return json.dumps(result, separators=(",", ":"), ensure_ascii=False)
        except ToolError as exc:
            return exc.to_json()

    @mcp.tool(
        annotations=ToolAnnotations(destructiveHint=True, idempotentHint=True),
    )
    async def keeper_delete_secret(uids: list[str]) -> str:
        """Delete one or more secrets by explicit, non-empty uids list.

        No folder-wide or vault-wide delete exists: a "delete everything in
        folder X" request is not representable by this tool's schema."""
        try:
            if not uids:
                raise ToolError("invalid_argument", "uids: must contain at least one UID")

            client = keeper_client.build_client(client_factory())
            response = await keeper_client.call_with_retry(
                lambda: client.delete_secret(uids), op="delete_secret"
            )

            deleted: list[str] = []
            not_found: list[str] = []
            by_uid = {
                entry.get("recordUid"): entry
                for entry in (response or [])
                if isinstance(entry, dict) and entry.get("recordUid")
            }
            for requested_uid in uids:
                entry = by_uid.get(requested_uid)
                code = str(entry.get("responseCode", "")).lower() if entry else ""
                if entry is not None and code in ("", "success", "ok"):
                    deleted.append(requested_uid)
                else:
                    not_found.append(requested_uid)

            result = {"deleted": deleted, "not_found": not_found}
            return json.dumps(result, separators=(",", ":"), ensure_ascii=False)
        except ToolError as exc:
            return exc.to_json()
