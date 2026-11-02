"""Per-request Keeper SDK client construction, retry policy, and error mapping.

Every tool call builds a fresh `SecretsManager` from that request's own
`X-Keeper-Config` header value (via `InMemoryKeyValueStorage`) and discards it
at the end of the call. Keeper's own documentation confirms the bound config
is stable and concurrency-safe to reuse across independent, simultaneous
processes (docs/tickets/PRD-21235.md, "Configuration stability"), so no
locking or caching is needed here -- see Non-functional in that doc.
"""

import logging
import time
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


def call_with_retry[T](fn: Callable[[], T], *, op: str) -> T:
    """Run a Keeper SDK call, retrying transient upstream failures (AC8).

    - requests.exceptions.Timeout / ConnectionError (network-level): retried
      with exponential backoff up to MAX_RETRIES, then mapped to
      upstream_error (retryable=true) if still failing.
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
            return fn()
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
                time.sleep(delay)
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
