"""Tests for the graded response_format fallback in http_retry."""

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest


def _resp(status: int, text: str):
    r = MagicMock()
    r.status_code = status
    r.text = text
    if status >= 400:
        r.raise_for_status = MagicMock(
            side_effect=httpx.HTTPStatusError("err", request=MagicMock(), response=r)
        )
    else:
        r.raise_for_status = MagicMock()
    return r


@pytest.mark.asyncio
async def test_format_fallback_strict_to_nonstrict_to_json_object():
    from baloo.agent.http_retry import post_chat_with_format_fallback

    strict = {"type": "json_schema", "json_schema": {"name": "x", "strict": True, "schema": {}}}
    seen = []

    async def fake_post(url, headers=None, json=None):
        rf = (json or {}).get("response_format")
        seen.append(rf)
        # Reject any json_schema (strict or not); accept json_object.
        if isinstance(rf, dict) and rf.get("type") == "json_schema":
            return _resp(400, "response_format json_schema is not supported")
        return _resp(200, '{"a":1}')

    client = AsyncMock()
    client.post = fake_post
    resp = await post_chat_with_format_fallback(
        client,
        "http://x/chat/completions",
        headers={},
        base_body={"model": "m", "messages": []},
        response_format=strict,
        label="t",
    )
    assert resp.status_code == 200
    assert len(seen) == 3
    assert seen[0]["json_schema"]["strict"] is True
    assert seen[1]["json_schema"]["strict"] is False
    assert seen[2] == {"type": "json_object"}


@pytest.mark.asyncio
async def test_format_fallback_reraises_unrelated_400():
    from baloo.agent.http_retry import post_chat_with_format_fallback

    async def fake_post(url, headers=None, json=None):
        return _resp(400, "context length exceeded")  # no response_format/schema markers

    client = AsyncMock()
    client.post = fake_post
    with pytest.raises(httpx.HTTPStatusError):
        await post_chat_with_format_fallback(
            client,
            "http://x/chat/completions",
            headers={},
            base_body={"model": "m", "messages": []},
            response_format={
                "type": "json_schema",
                "json_schema": {"name": "x", "strict": True, "schema": {}},
            },
            label="t",
        )
