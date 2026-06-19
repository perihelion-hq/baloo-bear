"""Shared Synthetic direct-HTTP JSON helper for no-tools structured calls.

The pi RPC path cannot force ``response_format``, so single-shot structured
calls that do NOT need file tools — the FP verdict, the synchronize scope
decision, and thread replies — use this direct OpenAI-compatible call with
``response_format={"type": "json_object"}`` instead. That constrains GLM-5.2 to
emit a JSON object rather than prose or a code block.

The helper returns ``(parsed_json_or_None, metadata)``. It never raises: any
transport/parse failure surfaces as ``parsed is None`` with ``metadata["error"]``
set, so each caller can apply its own fail-open/closed policy and record an
actionable audit entry.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

import httpx

from baloo.agent.pi_runtime import _extract_json_from_text
from baloo.config.settings import get_settings

logger = logging.getLogger(__name__)


async def synthetic_json_completion(
    *,
    model: str,
    system_prompt: str,
    user_prompt: str,
    label: str = "synthetic-json",
    timeout: float = 120.0,
) -> tuple[Any, dict[str, Any]]:
    """Call Synthetic /chat/completions with response_format=json_object.

    Args:
        model: Synthetic model id (e.g. ``hf:zai-org/GLM-5.2``).
        system_prompt: System message content.
        user_prompt: User message content.
        label: Short label used in log lines for correlation.
        timeout: HTTP timeout in seconds.

    Returns:
        ``(parsed_json_or_None, metadata)``. ``metadata`` always contains
        ``model``, ``provider`` (``"synthetic"``), ``input_tokens``,
        ``output_tokens``, ``thinking_tokens``, ``cost_usd``, ``is_error``,
        ``error`` (None on success), and ``raw_content`` (the model's raw text,
        preserved for auditing even when unparseable).
    """
    settings = get_settings()
    api_key = settings.synthetic_api_key or os.environ.get("SYNTHETIC_API_KEY", "")
    base_url = (settings.synthetic_base_url or "https://api.synthetic.new/openai/v1").rstrip("/")

    metadata: dict[str, Any] = {
        "model": model,
        "provider": "synthetic",
        "input_tokens": 0,
        "output_tokens": 0,
        "thinking_tokens": 0,
        # Synthetic does not bill through our cost model; leave at 0.
        "cost_usd": 0.0,
        "is_error": False,
        "error": None,
        "raw_content": None,
        "duration_seconds": 0.0,
    }

    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "response_format": {"type": "json_object"},
    }

    start = time.time()
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json=body,
            )
            resp.raise_for_status()
            data = resp.json()

        content = data["choices"][0]["message"]["content"]
        usage = data.get("usage") or {}
        reasoning_tokens = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens", 0)
        metadata.update(
            {
                "input_tokens": usage.get("prompt_tokens", 0),
                "output_tokens": usage.get("completion_tokens", 0),
                "thinking_tokens": reasoning_tokens,
                "raw_content": content,
                "duration_seconds": time.time() - start,
            }
        )

        parsed = _extract_json_from_text(content)
        if parsed is None:
            metadata["is_error"] = True
            metadata["error"] = "unparseable_json"
            logger.warning("%s: synthetic JSON call returned unparseable content", label)
        return parsed, metadata

    except Exception as exc:
        metadata["is_error"] = True
        metadata["error"] = str(exc)
        metadata["duration_seconds"] = time.time() - start
        logger.warning("%s: synthetic JSON call failed: %s", label, exc)
        return None, metadata
