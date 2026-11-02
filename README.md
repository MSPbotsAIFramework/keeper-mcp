# keeper-mcp

Keeper Secrets Manager MCP server for Claude — exposes a tenant's own Keeper vault
(secrets, fields, and folders) as MCP tools, wrapping Keeper's official
`keeper-secrets-manager-core` SDK.

**Tech stack:** Python 3.12 + uv + the official `mcp` SDK's FastMCP (Starlette/uvicorn)

Related requirement: PRD-21235 ([Security] Keeper Secrets Manager). See
`docs/tickets/PRD-21235.md` for the full product and technical design.

**Out of scope (v1):** folder create/update/delete, file/attachment tools, TOTP code
generation, and any OAuth flow — see `docs/tickets/PRD-21235.md` § Non-goals and
§ Decisions made by DEV01 for why each is deferred rather than refused.

<div align="center">

## MSPbots Connector

**The MCP Gateway built for MSPs: visible, controlled, and your data stays where it belongs.**

[**Get started at connect.mspbots.ai →**](https://connect.mspbots.ai)

</div>

---

MSPbots Connector gives your AI clients one MCP URL to reach your entire MSP stack (RMM, PSA, documentation, security, finance), and gives you control over who can call which tool and a record of everything that was called.

**Hosted service: [connect.mspbots.ai](https://connect.mspbots.ai)** · Free to use.

### Why Connector

![MSPbots Connector](https://stgoss.mspbots.ai/files/mcp/mspbots-mcp.png)

| | |
|---|---|
| **Visible** | Every tool call is logged: who, which vendor, when. Usage dashboard, CSV export, SIEM streaming. |
| **Controlled** | Role-based, per-tool allowlist. Deny by default. |
| **Protected** | Credentials encrypted per record. AI clients never see your vendor passwords or tokens. |
| **MSP-native** | Built for the MSP stack only: RMM, PSA, documentation, security, finance. |

### Quick start

1. **Sign up** at [connect.mspbots.ai](https://connect.mspbots.ai).
2. **Connect your vendors.** Pick from 150+ connectors and authorize each one on the vendor's own page, or bring your own API keys. No extra vendor accounts needed.
3. **Set access.** Create roles and decide which tools each role may call. Nothing is allowed until you allow it.
4. **Add your MCP URL to your AI client.** Copy the URL from your Connector dashboard into Claude, ChatGPT, Cursor, VS Code, Gemini, Codex, or any MCP-capable client.
5. **Ask your AI to do real work**, and watch every call appear in the audit log.

### Features

**Unified gateway**
- One MCP URL for all your AI clients
- 150+ connectors across the MSP stack
- Custom MCP servers supported, free
- Authorization happens on each vendor's own page; the AI never sees passwords or tokens
- Bring your own credentials: use the vendor API keys you already have
- Multiple accounts per app; connect once, reuse across AI clients
- Tool search: finds the right action for the task
- One sign-in; vendor credentials are injected behind the scenes and OAuth tokens refresh automatically

**Visibility**
- Audit log for every tool call (who, which vendor, when)
- Usage dashboard: call volume, top vendors, error rate, per-user activity
- Export audit logs to CSV, or stream to Loki, Graylog, or LogScale (SIEM)
- Optional recording of tool arguments and result summaries (off by default, enabled per organization)

**Control**
- Per-role, per-tool allowlist, deny by default
- owner / admin / member roles
- SCIM provisioning with Entra ID, Okta, JumpCloud, and Google Workspace

**Security**
- Credentials encrypted one record at a time (AES-256-GCM, per-record key)
- AI clients authenticate with OAuth 2.1 + PKCE
- Vendor responses are scanned for prompt-injection patterns and returned as structured data. This is one layer of defense, not a guarantee against every attack.
- Static egress IP for customer firewall and API allowlisting

### Pricing

**Free.**

Links: [connect.mspbots.ai](https://connect.mspbots.ai) · [mspbots.ai](https://mspbots.ai)

---

## Authentication headers

This service is **stateless**: it never stores, caches, or logs a tenant's Keeper
credential. Every request must carry one header:

| Header | Type | Required | Description | Example |
|---|---|---|---|---|
| `X-Keeper-Config` | string | Yes | Base64-encoded Keeper Secrets Manager **bound configuration** (`clientId`/`privateKey`/`appKey`/`hostname`/etc.), produced once by redeeming a One-Time Access Token via Keeper's own tooling — **never a one-time token itself**. Signs every request; does not rotate or expire on normal use, and the same blob may be used repeatedly and concurrently. | `eyJjbGllbnRJZCI6...` (long base64 string) |

Missing the header on a `/mcp` request returns `401` with a `required_headers` list, and
the service never calls Keeper. There is no environment-variable fallback anywhere in this
service (SOP §3.1) and no region/hostname override header — Keeper's own hostname is already
embedded in the bound config.

**Get a bound config:** as the tenant's Keeper admin, create an Application + Client Device in
your own Keeper Vault, generate a One-Time Access Token, and redeem it once (via the Keeper
Commander CLI or any `keeper-secrets-manager-*` SDK) to produce the bound configuration JSON —
base64-encode that JSON for this header. See
[Keeper's Secrets Manager docs](https://docs.keeper.io/en/secrets-manager/secrets-manager/developer-sdk-library)
for the redemption walkthrough.

## Quick Start

```bash
cd keeper-mcp
uv sync
MCP_HTTP_PORT=8080 uv run keeper-mcp
curl http://localhost:8080/health
```

This service only runs as an HTTP server (`POST /mcp`, Streamable HTTP) — there is no stdio
transport and no single-tenant "env mode"; every tool call carries its own credential header
(see Authentication headers), so there is nothing a shared environment variable could usefully
hold.

## Tool List

| Tool | Description | Parameters |
|---|---|---|
| `keeper_list_secrets` | List secrets, optionally filtered by folder or title substring | `folder_uid` (string, optional), `title_contains` (string, optional), `limit` (int, optional, default 50, hard cap 200), `offset` (int, optional, default 0) |
| `keeper_get_secret` | Get a secret's full field list, including values, by uid | `uid` (string, required) |
| `keeper_get_secret_field` | Get one field value via Keeper Notation (e.g. `uid/field/password`) | `notation` (string, required) |
| `keeper_create_secret` | Create a new secret in an existing folder | `folder_uid` (string, required), `title` (string, required), `fields` (object, required), `record_type` (string, optional, default `login`) |
| `keeper_update_secret` | Partially update a secret's fields — only named fields change | `uid` (string, required), `fields` (object, required) |
| `keeper_delete_secret` | Delete one or more secrets by explicit uid | `uids` (string[], required, non-empty) |
| `keeper_list_folders` | List all folders visible to this vault | None |

**No bulk or folder-wide delete exists** — `keeper_delete_secret` only accepts an explicit
`uids` list (see Known Limitations).

**`fields`** keys are Keeper standard field type names (e.g. `login`, `password`, `url`);
`notes` sets the record's notes, not a field. See
[Keeper's record-field reference](https://docs.keeper.io/en/secrets-manager/secrets-manager/developer-sdk-library/python-sdk)
for the full list.

## Test Examples

### tools/list
```bash
curl -X POST http://localhost:8080/mcp \
  -H "X-Keeper-Config: <base64 bound config>" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","method":"tools/list","id":1}'
```

### tools/call — get a secret
```bash
curl -X POST http://localhost:8080/mcp \
  -H "X-Keeper-Config: <base64 bound config>" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{
    "jsonrpc": "2.0",
    "method": "tools/call",
    "id": 2,
    "params": {
      "name": "keeper_get_secret",
      "arguments": {"uid": "<record-uid>"}
    }
  }'
```

### tools/call — create a secret
```bash
curl -X POST http://localhost:8080/mcp \
  -H "X-Keeper-Config: <base64 bound config>" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{
    "jsonrpc": "2.0",
    "method": "tools/call",
    "id": 3,
    "params": {
      "name": "keeper_create_secret",
      "arguments": {
        "folder_uid": "<folder-uid>",
        "title": "Service account",
        "fields": {"login": "svc@acme.com", "password": "s3cr3t"}
      }
    }
  }'
```

### Missing header → 401
```bash
curl -i -X POST http://localhost:8080/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","method":"tools/list","id":1}'
# HTTP/1.1 401 Unauthorized
# {"error":"Missing credentials","message":"Missing required header: X-Keeper-Config","required_headers":["X-Keeper-Config"]}
```

## API Reference

- [Keeper Secrets Manager overview / SDK index](https://docs.keeper.io/en/secrets-manager/secrets-manager/developer-sdk-library)
- [One-Time Access Token / bound configuration model](https://docs.keeper.io/keeperpam/secrets-manager/about/one-time-token.md)
- [Configuration stability (why the bound config, not the token, is reused)](https://docs.keeper.io/keeperpam/secrets-manager/about/secrets-manager-configuration.md)
- [Python SDK reference](https://docs.keeper.io/en/secrets-manager/secrets-manager/developer-sdk-library/python-sdk)
- [`keeper-secrets-manager-core` source](https://github.com/Keeper-Security/secrets-manager) (reference only — this service is a clean-room implementation against the vendor's own public docs, not a fork or port of this repository)

## Known Limitations

- **No folder create/update/delete, file/attachment, or TOTP tools in v1** — the ticket asked
  for live access to secrets, not vault-structure administration; see
  `docs/tickets/PRD-21235.md` § Decisions made by DEV01 for the reasoning and how to request
  them as a fast follow.
- **`keeper_delete_secret` is explicit-uid-only** — there is no "delete everything in folder X"
  or vault-wide variant, by design (a bad agent turn should not be able to wipe a tenant's
  vault).
- **`keeper_list_secrets` has no native upstream pagination** — the vendor SDK returns the
  whole vault's records per call; this service slices that result to `limit`/`offset`. A very
  large vault means a possibly large single upstream fetch per list call (not verified against
  a real large vault — no test account available).
- **No test Keeper account available at build time** — cross-tenant isolation (two real
  accounts) and a full live create→get→update→delete round trip are marked "needs a real
  vendor account" in the self-test report rather than executed.
- **v1 assumes `login`-shaped record fields** — if the primary real-world use case is a
  different record type (database credentials, SSH keys, etc.), `keeper_create_secret`'s field
  shape may need adjusting; not verified against every Keeper record type.
