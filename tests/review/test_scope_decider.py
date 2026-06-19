"""Tests for the synchronize scope decider's Synthetic json_object path.

`_decide_synchronize_review_mode` asks the configured model whether a
synchronize push should be reviewed as ``scoped`` vs ``full_pr``. With the
default glm/synthetic model it now routes through the Synthetic direct-HTTP
``synthetic_json_completion`` helper (response_format=json_object) instead of
the pi runtime, so GLM returns a JSON object rather than prose/a code block.

These tests patch the helper (AsyncMock) and force provider=="synthetic" so
no pi process is spawned, then assert the decision/default handling is intact.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from baloo.agent.pi_runtime import PIAgentOptions
from baloo.github.models import FileChange, PRContext, PRDiscussionContext, PRMetadata
from baloo.review.orchestrator import _decide_synchronize_review_mode


def _file_change() -> FileChange:
    return FileChange(
        filename="auth.py",
        status="modified",
        additions=3,
        deletions=1,
        changes=4,
        patch="@@ -1,2 +1,3 @@\n-old\n+new line\n",
    )


def _pr_context() -> PRContext:
    """Minimal PRContext exercising every field the decider reads."""
    return PRContext(
        metadata=PRMetadata(
            repo_full_name="test/repo",
            pr_number=7,
            title="Test PR",
            description="A small change",
            author="test-user",
            base_branch="main",
            head_branch="feature/x",
            head_sha="abc123",
            files_changed=[_file_change()],
        ),
        discussion=PRDiscussionContext(),
        diff="diff --git a/auth.py b/auth.py\n@@ -1,2 +1,3 @@\n-old\n+new line\n",
    )


def _synthetic_meta(*, is_error: bool = False, error: str | None = None) -> dict:
    """Build a metadata dict matching synthetic_json_completion's contract."""
    return {
        "model": "hf:zai-org/GLM-5.2",
        "provider": "synthetic",
        "input_tokens": 100,
        "output_tokens": 20,
        "thinking_tokens": 0,
        "cost_usd": 0.0,
        "is_error": is_error,
        "error": error,
        "raw_content": None,
    }


def _synthetic_options() -> PIAgentOptions:
    """Options resolving to the Synthetic provider (the glm default)."""
    return PIAgentOptions(
        model="hf:zai-org/GLM-5.2",
        provider="synthetic",
        system_prompt="",
        thinking_level="minimal",
        max_turns=30,
    )


async def _run_decider():
    return await _decide_synchronize_review_mode(
        pr_context=_pr_context(),
        changed_files_changed=[_file_change()],
        scoped_diff="@@ -1,2 +1,3 @@\n+changed line\n",
    )


@pytest.mark.asyncio
async def test_scoped_decision_returns_scoped():
    with (
        patch(
            "baloo.review.orchestrator.get_agent_options",
            return_value=_synthetic_options(),
        ),
        patch(
            "baloo.review.orchestrator.synthetic_json_completion",
            new_callable=AsyncMock,
            return_value=({"mode": "scoped", "reason": "x"}, _synthetic_meta()),
        ) as mock_helper,
    ):
        mode, reason = await _run_decider()

    assert mode == "scoped"
    assert reason == "x"
    # Synthetic path must be used (and therefore no pi process spawned).
    mock_helper.assert_awaited_once()


@pytest.mark.asyncio
async def test_full_pr_decision_returns_full_pr():
    with (
        patch(
            "baloo.review.orchestrator.get_agent_options",
            return_value=_synthetic_options(),
        ),
        patch(
            "baloo.review.orchestrator.synthetic_json_completion",
            new_callable=AsyncMock,
            return_value=({"mode": "full_pr", "reason": "y"}, _synthetic_meta()),
        ) as mock_helper,
    ):
        mode, reason = await _run_decider()

    assert mode == "full_pr"
    assert reason == "y"
    mock_helper.assert_awaited_once()


@pytest.mark.asyncio
async def test_none_parsed_falls_back_to_full_pr():
    """Helper failure (parsed=None, meta.is_error) -> graceful full_pr default."""
    with (
        patch(
            "baloo.review.orchestrator.get_agent_options",
            return_value=_synthetic_options(),
        ),
        patch(
            "baloo.review.orchestrator.synthetic_json_completion",
            new_callable=AsyncMock,
            return_value=(None, _synthetic_meta(is_error=True, error="unparseable_json")),
        ) as mock_helper,
    ):
        mode, reason = await _run_decider()

    assert mode == "full_pr"
    assert reason == "Scope decision unavailable; defaulting to full PR"
    mock_helper.assert_awaited_once()


@pytest.mark.asyncio
async def test_scope_decider_uses_json_schema():
    """The scope decision call constrains output with response_format=json_schema(scope_decision)."""
    with (
        patch(
            "baloo.review.orchestrator.get_agent_options",
            return_value=_synthetic_options(),
        ),
        patch(
            "baloo.review.orchestrator.synthetic_json_completion",
            new_callable=AsyncMock,
            return_value=({"mode": "scoped", "reason": "x"}, _synthetic_meta()),
        ) as mock_helper,
    ):
        await _run_decider()

    rf = mock_helper.call_args.kwargs["response_format"]
    assert rf["type"] == "json_schema"
    assert rf["json_schema"]["name"] == "scope_decision"
    assert rf["json_schema"]["strict"] is True


@pytest.mark.asyncio
async def test_unexpected_mode_falls_back_to_full_pr():
    """Dict without a valid mode -> full_pr default."""
    with (
        patch(
            "baloo.review.orchestrator.get_agent_options",
            return_value=_synthetic_options(),
        ),
        patch(
            "baloo.review.orchestrator.synthetic_json_completion",
            new_callable=AsyncMock,
            return_value=({"garbage": 1}, _synthetic_meta()),
        ) as mock_helper,
    ):
        mode, reason = await _run_decider()

    assert mode == "full_pr"
    assert reason == "Scope decision unavailable; defaulting to full PR"
    mock_helper.assert_awaited_once()
