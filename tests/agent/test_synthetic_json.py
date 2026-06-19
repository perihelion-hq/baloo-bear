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
async def test_passes_response_format_through_and_reports_finish_reason():
    """A caller-supplied response_format is sent verbatim and finish_reason surfaced."""
    rf = {"type": "json_schema", "json_schema": {"name": "x", "strict": True, "schema": {}}}
    response_json = {
        "choices": [{"message": {"content": '{"a": 1}'}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }
    acm, client = _mock_client(response_json)
    with patch("baloo.agent.synthetic_json.httpx.AsyncClient", return_value=acm):
        parsed, meta = await synthetic_json_completion(
            model="m", system_prompt="s", user_prompt="u", response_format=rf
        )
    assert client.post.call_args.kwargs["json"]["response_format"] == rf
    assert meta["finish_reason"] == "stop"
    assert parsed == {"a": 1}


@pytest.mark.asyncio
async def test_default_response_format_is_json_object_with_finish_reason():
    """With no response_format, the helper defaults to json_object and reports finish_reason."""
    response_json = {
        "choices": [{"message": {"content": '{"verdict": "fp"}'}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }
    acm, client = _mock_client(response_json)
    with patch("baloo.agent.synthetic_json.httpx.AsyncClient", return_value=acm):
        _, meta = await synthetic_json_completion(model="m", system_prompt="s", user_prompt="u")
    assert client.post.call_args.kwargs["json"]["response_format"] == {"type": "json_object"}
    assert meta["finish_reason"] == "stop"


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


def _make_429() -> httpx.HTTPStatusError:
    """Build a 429 HTTPStatusError carrying a Retry-After header."""
    return httpx.HTTPStatusError(
        "429",
        request=httpx.Request("POST", "http://x"),
        response=httpx.Response(429, headers={"Retry-After": "1"}),
    )


@pytest.mark.asyncio
async def test_retries_on_429_then_succeeds():
    """A first POST hitting 429 retries with backoff; the second 200 succeeds."""
    ok_json = {
        "choices": [{"message": {"content": '{"verdict": "fp"}'}}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 2},
    }
    ok_resp = MagicMock()
    ok_resp.json = MagicMock(return_value=ok_json)
    ok_resp.raise_for_status = MagicMock()

    err_resp = MagicMock()
    err_resp.raise_for_status = MagicMock(side_effect=_make_429())

    client = AsyncMock()
    client.post = AsyncMock(side_effect=[err_resp, ok_resp])
    acm = MagicMock()
    acm.__aenter__ = AsyncMock(return_value=client)
    acm.__aexit__ = AsyncMock(return_value=False)

    with patch("baloo.agent.synthetic_json.httpx.AsyncClient", return_value=acm):
        with patch("baloo.agent.http_retry.asyncio.sleep", AsyncMock()) as sleep:
            parsed, meta = await synthetic_json_completion(
                model="m", system_prompt="s", user_prompt="u"
            )

    assert parsed == {"verdict": "fp"}
    assert meta["is_error"] is False
    assert client.post.await_count == 2
    sleep.assert_awaited_once()


@pytest.mark.asyncio
async def test_429_on_all_attempts_returns_error():
    """Persistent 429 exhausts retries and surfaces the existing failure result."""
    err_resp = MagicMock()
    err_resp.raise_for_status = MagicMock(side_effect=_make_429())

    client = AsyncMock()
    client.post = AsyncMock(return_value=err_resp)
    acm = MagicMock()
    acm.__aenter__ = AsyncMock(return_value=client)
    acm.__aexit__ = AsyncMock(return_value=False)

    with patch("baloo.agent.synthetic_json.httpx.AsyncClient", return_value=acm):
        with patch("baloo.agent.http_retry.asyncio.sleep", AsyncMock()):
            parsed, meta = await synthetic_json_completion(
                model="m", system_prompt="s", user_prompt="u"
            )

    assert parsed is None
    assert meta["is_error"] is True
    # First attempt + 3 retries == 4 POSTs.
    assert client.post.await_count == 4


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
