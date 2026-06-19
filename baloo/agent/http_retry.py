"""Shared 429-aware retry wrapper for Synthetic /chat/completions POSTs.

Synthetic (``api.synthetic.new``) returns HTTP 429 "Too Many Requests" under
load. Without retry that surfaces as an immediate failure and cascades into
fail-closed reviews. This helper retries the POST on 429 (and optionally
transient timeouts) with backoff, honoring the ``Retry-After`` header when
present and otherwise using a small capped exponential schedule.

This module deliberately imports only ``asyncio``/``httpx``/``logging`` so it can
be shared by both ``synthetic_json`` and ``pi_runtime`` without creating an
import cycle (``synthetic_json`` already imports from ``pi_runtime``).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Number of additional attempts after the first try (so up to N+1 POSTs total).
_MAX_RETRIES = 3

# Capped exponential backoff (seconds) used when no Retry-After header is given.
# Index i is the delay before retry attempt i+1.
_BACKOFF_SCHEDULE = (1.0, 2.0, 4.0)
_BACKOFF_CAP = 8.0


def _retry_after_delay(response: httpx.Response) -> float | None:
    """Parse the Retry-After header (seconds form) if present and valid."""
    raw = response.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        delay = float(raw)
    except (TypeError, ValueError):
        return None
    if delay < 0:
        return None
    return min(delay, _BACKOFF_CAP)


async def post_with_retry_on_429(
    client: httpx.AsyncClient,
    url: str,
    *,
    headers: dict[str, str],
    json: Any,
    label: str = "synthetic",
    max_retries: int = _MAX_RETRIES,
) -> httpx.Response:
    """POST ``json`` to ``url`` retrying on HTTP 429 with backoff.

    Calls ``resp.raise_for_status()`` after each POST. On an
    ``httpx.HTTPStatusError`` whose status is 429 (or an
    ``httpx.TimeoutException``), waits and retries up to ``max_retries`` times.
    The backoff honors the ``Retry-After`` response header when present (seconds),
    otherwise uses a capped exponential schedule (1s, 2s, 4s; cap ~8s).

    Any non-429 ``HTTPStatusError`` or other exception propagates immediately
    (no retry), matching the prior fail-fast behavior. After exhausting retries
    on 429, the final ``HTTPStatusError`` is re-raised so callers reach their
    existing failure path.

    Returns the successful ``httpx.Response`` (status already raised-for).
    """
    attempt = 0
    while True:
        try:
            resp = await client.post(url, headers=headers, json=json)
            resp.raise_for_status()
            return resp
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 429 or attempt >= max_retries:
                if exc.response.status_code == 429:
                    logger.warning(
                        "%s: exhausted 429 retries (%d attempts), giving up",
                        label,
                        attempt + 1,
                    )
                raise
            delay = _retry_after_delay(exc.response)
            if delay is None:
                delay = min(_BACKOFF_SCHEDULE[attempt], _BACKOFF_CAP)
            logger.warning(
                "%s: HTTP 429 (attempt %d/%d), retrying in %.1fs",
                label,
                attempt + 1,
                max_retries + 1,
                delay,
            )
            await asyncio.sleep(delay)
            attempt += 1
        except httpx.TimeoutException as exc:
            if attempt >= max_retries:
                raise
            delay = min(_BACKOFF_SCHEDULE[attempt], _BACKOFF_CAP)
            logger.warning(
                "%s: request timeout (attempt %d/%d), retrying in %.1fs: %s",
                label,
                attempt + 1,
                max_retries + 1,
                delay,
                exc,
            )
            await asyncio.sleep(delay)
            attempt += 1
