"""Per-request Keeper SDK client construction, retry policy, and error mapping.

Every tool call builds a fresh `SecretsManager` from that request's own
`X-Keeper-Config` header value (via `InMemoryKeyValueStorage`) and discards it
at the end of the call. Keeper's own documentation confirms the bound config
is stable and concurrency-safe to reuse across independent, simultaneous
processes (docs/tickets/PRD-21235.md, "Configuration stability"), so no
locking or caching is needed here -- see Non-functional in that doc.
"""

import asyncio
import logging
from collections.abc import Callable

import requests
from keeper_secrets_manager_core import SecretsManager
from keeper_secrets_manager_core.exceptions import KeeperError, KeeperThrottleError
from keeper_secrets_manager_core.storage import InMemoryKeyValueStorage

from .errors import ToolError

logger = logging.getLogger("keeper_mcp")

# Bounded retry budget for transient network failures (SOP §5: single tool call,
# including retries, must stay well under the 120s upstream timeout).
MAX_RETRIES = 3
BASE_DELAY_SEC = 1.0

# Outbound (connect, read) timeout applied to every Keeper API call (SOP §14
# Checklist requires setting an outbound connect/read timeout). The vendor SDK
# (keeper-secrets-manager-core==17.3.0) calls `requests.post(...)` directly
# with no `timeout=` kwarg and exposes no parameter to set one (confirmed
# against the installed package's core.py: `SecretsManager.post_function`,
# `__upload_file_function`) -- `requests` otherwise waits indefinitely on a
# stalled connection. Worst case with MAX_RETRIES=3: 3 * 20s read timeout +
# backoff (1s + 2s) = ~63s, comfortably under the 120s per-call budget below.
_OUTBOUND_TIMEOUT_SEC = (5, 20)  # (connect, read)

_original_requests_post = requests.post


def _post_with_default_timeout(*args: object, **kwargs: object) -> requests.Response:
    """Wrap `requests.post` to inject a default timeout when the caller
    didn't pass one. Patched process-wide at import time because the vendor
    SDK calls the module-level function directly rather than through a
    `requests.Session` we could configure ourselves."""
    kwargs.setdefault("timeout", _OUTBOUND_TIMEOUT_SEC)
    return _original_requests_post(*args, **kwargs)


if not getattr(requests.post, "_keeper_mcp_patched", False):
    _post_with_default_timeout._keeper_mcp_patched = True  # type: ignore[attr-defined]
    requests.post = _post_with_default_timeout


def build_client(config_b64: str | None) -> SecretsManager:
    """Construct a SecretsManager from this request's header value.

    Raises ToolError("not_configured", ...) if no header value is present at
    all (should not normally happen -- the gateway middleware already returns
    401 before any tool runs -- but tools defend against it independently
    rather than trusting the middleware unconditionally).
    Raises ToolError("invalid_argument", ...) if the value does not decode as
    a usable KSM configuration.
    """
    if not config_b64:
        raise ToolError(
            "not_configured", "No Keeper configuration available for this request"
        )
    try:
        storage = InMemoryKeyValueStorage(config=config_b64)
        return SecretsManager(config=storage)
    except Exception as exc:
        raise ToolError(
            "invalid_argument", f"X-Keeper-Config could not be parsed: {exc}"
        ) from exc


async def call_with_retry[T](fn: Callable[[], T], *, op: str) -> T:
    """Run a Keeper SDK call, retrying transient upstream failures (AC8).

    `fn` is a blocking call into the vendor SDK (it ultimately calls
    `requests.post` synchronously). It is run via `asyncio.to_thread` so a
    slow or stalled upstream connection only blocks the worker thread
    handling this one request, never the shared event loop that every other
    tenant's concurrent `/mcp` request also runs on.

    - requests.exceptions.Timeout / ConnectionError (network-level): retried
      with exponential backoff (`asyncio.sleep`, also non-blocking) up to
      MAX_RETRIES, then mapped to upstream_error (retryable=true) if still
      failing.
    - KeeperThrottleError: the SDK has already exhausted its own internal
      403-throttle retry loop (exponential backoff + jitter, see vendor SDK
      core.py) before raising this -- mapped straight to rate_limited
      (retryable=true) without an additional retry layer here, to stay inside
      the 120s per-call budget.
    - KeeperError (any other SDK/application-level failure): not retried --
      these are generally permanent conditions (bad folder, bad field, access
      denied), not transient ones. Mapped via _map_keeper_error.
    """
    last_exc: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            return await asyncio.to_thread(fn)
        except KeeperThrottleError as exc:
            raise ToolError("rate_limited", str(exc)) from exc
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
            last_exc = exc
            if attempt < MAX_RETRIES - 1:
                delay = BASE_DELAY_SEC * (2**attempt)
                logger.warning(
                    "upstream network error on %s (attempt %d/%d): %s; retrying in %.1fs",
                    op,
                    attempt + 1,
                    MAX_RETRIES,
                    exc.__class__.__name__,
                    delay,
                )
                await asyncio.sleep(delay)
                continue
            break
        except KeeperError as exc:
            raise _map_keeper_error(exc) from exc
    raise ToolError(
        "upstream_error", f"{op} failed after {MAX_RETRIES} attempts: {last_exc}"
    )


def _map_keeper_error(exc: KeeperError) -> ToolError:
    """Map a KeeperError to the structured error vocabulary.

    The vendor SDK raises a single KeeperError class for almost every
    application-level failure, carrying only a human-readable `message` (see
    exceptions.py) -- there is no status_code to switch on. Only one message
    shape is pinned to source (the "Folder uid=... was not retrieved" string
    from SecretsManager.create_secret, core.py); the rest is a best-effort
    keyword match, since no live Keeper account was available to enumerate
    every backend message (docs/tickets/PRD-21235.md, Risks). Unmatched
    KeeperErrors default to upstream_error rather than guessing a more
    specific code.
    """
    message = str(getattr(exc, "message", None) or exc)
    lower = message.lower()
    if "folder uid=" in lower and "was not retrieved" in lower:
        return ToolError("invalid_argument", f"folder_uid: {message}")
    if "folder key" in lower and "not found" in lower:
        return ToolError("invalid_argument", f"folder_uid: {message}")
    if "denied" in lower or "not authorized" in lower or "unauthorized" in lower:
        return ToolError("unauthorized", message)
    return ToolError("upstream_error", message)
