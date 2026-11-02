# PRD-21235 · Code review

## Round 1 · 2026-11-02 · reviewed dev/PRD-21235 @ 310c681 (base main @ 03543c6)

Verdict: changes requested

### Findings

1. [must-fix] docs/tickets/PRD-21235.md:211 — The frozen design doc's Self-test table claims
   `Chinese-text scan | ... | No output (clean)`, but the row itself contains four literal
   CJK fragments quoted from the pre-fix text (`已假设`, `不阻塞`, `自研`, plus the bracketed
   forms). Re-running the exact documented scan on this commit
   (`grep -rnP '[\x{4e00}-\x{9fff}]' --exclude-dir=.git --exclude-dir=.venv .`) confirms a hit
   at this line. This both contradicts the self-test claim and violates the vendor-mcp SOP's
   English-only rule (SOP §0.4 / §14 Checklist: "仓库内容全部英文 ... 文档 ... 均无中文"), which
   `references/mcp-track.md` §4.3 lists as a must-fix item ("仓库里没有中文"). Fix: rephrase the
   row so the before/after is described without embedding the literal CJK characters (e.g. name
   the fix in English prose, or reference the commit diff instead of quoting the old text).

2. [must-fix] src/keeper_mcp/keeper_client.py:30-93 (and every call site in
   src/keeper_mcp/tools/secrets.py, src/keeper_mcp/tools/folders.py) — No outbound
   connect/read timeout is ever set on the Keeper API calls, and the blocking vendor SDK calls
   (`client.get_secrets`, `client.create_secret`, `client.save`, `client.delete_secret`,
   `client.get_folders`, `client.get_notation`) run directly inside `async def` tool handlers
   with no `asyncio.to_thread`/executor offload. Confirmed against the installed
   `keeper-secrets-manager-core==17.3.0` source (`core.py:716` `SecretsManager.post_function`,
   `core.py:733` `__upload_file_function`): both call `requests.post(...)` with no `timeout=`
   argument, and the SDK exposes no parameter to set one. `requests` has no timeout by default,
   so a stalled Keeper API connection hangs indefinitely — it never raises
   `requests.exceptions.Timeout`, so the `call_with_retry` branch that handles it
   (keeper_client.py:74) is dead code in practice. Separately confirmed against the installed
   MCP SDK (`mcp/server/fastmcp/utilities/func_metadata.py:112-113`:
   `if fn_is_async: return await fn(**arguments_parsed_dict)`) that async tool functions are
   awaited directly on the event loop with no thread offload. The combination means one
   tenant's hung/slow Keeper connection — or even the in-process `time.sleep()` backoff at
   keeper_client.py:86 between retries, which also blocks the event loop instead of using
   `asyncio.sleep` — stalls the single-process event loop and therefore every other tenant's
   concurrent `/mcp` request, not just the slow one. This contradicts the design's own stated
   invariant (keeper_client.py:25 comment: "must stay well under the 120s upstream timeout" —
   nothing enforces this) and the SOP §14 Checklist items "设置出站超时（connect / read）" and
   "单次调用总耗时 < 120s". It also undercuts the concurrency-safety goal the whole design rests
   on (Non-functional § Idempotency & concurrency, AC7) — TC9's test only proves contextvar
   data isolation under mocked, effectively-instant calls; it does not exercise a real blocking
   call and so does not catch this. Fix: either pass an explicit timeout into the SDK's HTTP
   layer (e.g. monkeypatch/wrap `requests.Session` with a default timeout, since the SDK takes
   no `timeout` kwarg) and/or run each blocking SDK call via `asyncio.to_thread(...)` so a slow
   upstream call blocks only that one request's worker, not the shared event loop; replace the
   `time.sleep` backoff with `await asyncio.sleep` once the call itself is offloaded to a thread
   (or keep `time.sleep` only inside the thread-offloaded callable).

### Suggestions

1. [suggestion] src/keeper_mcp/tools/secrets.py:24 — `MAX_RESULT_CHARS = 20_000` is defined but
   never read anywhere in the file; it looks like it was meant to enforce the SOP §14 checklist's
   "单次返回 ≤ 20,000 字符" cap but doesn't actually truncate or flag oversized responses (e.g.
   `keeper_get_secret` returns a record's full field list with no size cap — a record with a very
   large field value, such as an SSH private key or a long note, could exceed 20,000 chars with no
   truncation or `has_more`-style indicator). Not blocking per REVIEW.md's dead-code-is-suggestion
   rule, but worth wiring up or removing.

### Fixes            ← appended by the dev leg on rework
