"""Tests for the shared Synthetic direct-HTTP JSON helper."""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from baloo.agent.synthetic_json import synthetic_json_completion


def _mock_client(response_json: dict):
    resp = MagicMock()
    resp.json = MagicMock(return_value=response_json)
    resp.raise_for_status = MagicMock()
    client = AsyncMock()
    client.post = AsyncMock(return_value=resp)
    acm = MagicMock()
    acm.__aenter__ = AsyncMock(return_value=client)
    acm.__aexit__ = AsyncMock(return_value=False)
    return acm, client


@pytest.mark.asyncio
async def test_parses_json_object_and_reports_usage():
    response_json = {
        "choices": [{"message": {"content": '{"verdict": "fp", "reason": "looks safe"}'}}],
        "usage": {
            "prompt_tokens": 80,
            "completion_tokens": 12,
            "completion_tokens_details": {"reasoning_tokens": 4},
        },
    }
    acm, client = _mock_client(response_json)
    with patch("baloo.agent.synthetic_json.httpx.AsyncClient", return_value=acm):
        parsed, meta = await synthetic_json_completion(
            model="hf:zai-org/GLM-5.2", system_prompt="sys", user_prompt="usr"
        )
    assert parsed == {"verdict": "fp", "reason": "looks safe"}
    assert meta["is_error"] is False
    assert meta["error"] is None
    assert meta["provider"] == "synthetic"
    assert meta["model"] == "hf:zai-org/GLM-5.2"
    assert meta["input_tokens"] == 80
    assert meta["output_tokens"] == 12
    assert meta["thinking_tokens"] == 4
    assert meta["raw_content"] == '{"verdict": "fp", "reason": "looks safe"}'
    # Request forced JSON output and used the given model
    body = client.post.call_args.kwargs["json"]
    assert body["response_format"] == {"type": "json_object"}
    assert body["model"] == "hf:zai-org/GLM-5.2"
    assert body["messages"][0]["role"] == "system"
    assert body["messages"][-1]["content"] == "usr"


@pytest.mark.asyncio
async def test_http_error_returns_none_with_error_metadata():
    client = AsyncMock()
    client.post = AsyncMock(side_effect=httpx.HTTPError("boom"))
    acm = MagicMock()
    acm.__aenter__ = AsyncMock(return_value=client)
    acm.__aexit__ = AsyncMock(return_value=False)
    with patch("baloo.agent.synthetic_json.httpx.AsyncClient", return_value=acm):
        parsed, meta = await synthetic_json_completion(
            model="m", system_prompt="s", user_prompt="u"
        )
    assert parsed is None
    assert meta["is_error"] is True
    assert "boom" in meta["error"]
    assert meta["provider"] == "synthetic"


@pytest.mark.asyncio
async def test_unparseable_content_returns_none_with_error_flag():
    response_json = {
        "choices": [{"message": {"content": "this is not json at all"}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }
    acm, _ = _mock_client(response_json)
    with patch("baloo.agent.synthetic_json.httpx.AsyncClient", return_value=acm):
        parsed, meta = await synthetic_json_completion(
            model="m", system_prompt="s", user_prompt="u"
        )
    assert parsed is None
    assert meta["is_error"] is True
    assert meta["error"] == "unparseable_json"
    # raw content preserved for audit even when unparseable
    assert meta["raw_content"] == "this is not json at all"
