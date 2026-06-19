"""Tests for the ThreadAgent."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from baloo.agent.thread_agent import ThreadAgent, ThreadAgentResult
from baloo.github.models import DiscussionComment


def _make_comment(author: str, body: str, is_baloo: bool = False) -> DiscussionComment:
    now = datetime.now(timezone.utc)
    return DiscussionComment(
        id=1,
        author=author,
        body=body,
        created_at=now,
        updated_at=now,
        source="review_comment",
        is_baloo=is_baloo,
    )


@pytest.mark.asyncio
async def test_thread_agent_concede():
    """ThreadAgent returns disagreed_valid with reply and feedback signal."""
    agent = ThreadAgent()

    mock_response = {
        "classification": "disagreed_valid",
        "reply": "Got it, makes sense for retry loops.",
        "reasoning": "Developer explained retry semantics.",
        "feedback_signal": {
            "pattern": "except pass in retry loops is intentional",
            "category": "Silent Failures",
            "file_glob": "app/retry/*.py",
        },
    }

    with patch.object(agent, "_run_query", new_callable=AsyncMock) as mock_run:
        mock_run.return_value = (
            mock_response,
            {"cost_usd": 0.001, "model": "claude-haiku-4-5-20251001"},
        )

        result = await agent.classify(
            thread_comments=[
                _make_comment(
                    "baloo[bot]", "**[HIGH] Silent Failures** - except pass", is_baloo=True
                ),
                _make_comment("alice", "This is intentional for retry loops"),
            ],
            code_context="try:\n    op()\nexcept Exception:\n    pass",
            file_path="app/retry/handler.py",
            line_number=15,
        )

    assert isinstance(result, ThreadAgentResult)
    assert result.classification == "disagreed_valid"
    assert result.reply == "Got it, makes sense for retry loops."
    assert result.feedback_signal is not None
    assert result.feedback_signal["pattern"] == "except pass in retry loops is intentional"


@pytest.mark.asyncio
async def test_thread_agent_acknowledged():
    """ThreadAgent returns acknowledged with no reply."""
    agent = ThreadAgent()

    mock_response = {
        "classification": "acknowledged",
        "reply": None,
        "reasoning": "Developer says they fixed it.",
        "feedback_signal": None,
    }

    with patch.object(agent, "_run_query", new_callable=AsyncMock) as mock_run:
        mock_run.return_value = (mock_response, {"cost_usd": 0.0005})

        result = await agent.classify(
            thread_comments=[
                _make_comment("baloo[bot]", "Finding", is_baloo=True),
                _make_comment("dev", "Fixed in latest commit"),
            ],
            code_context="fixed code",
            file_path="src/foo.py",
            line_number=10,
        )

    assert result.classification == "acknowledged"
    assert result.reply is None
    assert result.feedback_signal is None


@pytest.mark.asyncio
async def test_thread_agent_parse_failure_returns_unclear():
    """Unparseable response defaults to unclear with no reply."""
    agent = ThreadAgent()

    with patch.object(agent, "_run_query", new_callable=AsyncMock) as mock_run:
        mock_run.return_value = (None, {"cost_usd": 0.0})

        result = await agent.classify(
            thread_comments=[
                _make_comment("baloo[bot]", "Finding", is_baloo=True),
                _make_comment("dev", "hmm"),
            ],
            code_context="code",
            file_path="f.py",
            line_number=1,
        )

    assert result.classification == "unclear"
    assert result.reply is None


@pytest.mark.asyncio
async def test_thread_agent_exception_returns_unclear():
    """Agent exceptions are caught and return unclear."""
    agent = ThreadAgent()

    with patch.object(agent, "_run_query", new_callable=AsyncMock) as mock_run:
        mock_run.side_effect = RuntimeError("PI crashed")

        result = await agent.classify(
            thread_comments=[
                _make_comment("baloo[bot]", "Finding", is_baloo=True),
                _make_comment("dev", "why?"),
            ],
            code_context="code",
            file_path="f.py",
            line_number=1,
        )

    assert result.classification == "unclear"
    assert result.reply is None


@pytest.mark.asyncio
async def test_thread_agent_synthetic_path_returns_structured():
    """Synthetic provider routes through the json_object helper and parses it."""
    # "glm" resolves to provider "synthetic" in the model registry, so
    # get_agent_options(...).provider == "synthetic" and _run_query uses the helper.
    agent = ThreadAgent(model="glm")

    helper_payload = {
        "classification": "disagreed_valid",
        "reply": "Understood, the retry loop swallows it on purpose.",
        "reasoning": "Developer explained intentional retry semantics.",
        "feedback_signal": {
            "pattern": "except pass in retry loops is intentional",
            "category": "Silent Failures",
            "file_glob": "app/retry/*.py",
        },
    }
    helper_meta = {
        "model": "hf:zai-org/GLM-5.2",
        "provider": "synthetic",
        "is_error": False,
        "error": None,
        "cost_usd": 0.0,
    }

    with patch(
        "baloo.agent.thread_agent.synthetic_json_completion",
        new_callable=AsyncMock,
    ) as mock_helper:
        mock_helper.return_value = (helper_payload, helper_meta)

        result = await agent.classify(
            thread_comments=[
                _make_comment(
                    "baloo[bot]", "**[HIGH] Silent Failures** - except pass", is_baloo=True
                ),
                _make_comment("alice", "This is intentional for retry loops"),
            ],
            code_context="try:\n    op()\nexcept Exception:\n    pass",
            file_path="app/retry/handler.py",
            line_number=15,
        )

    # Helper was invoked on the synthetic path with the thread system prompt.
    mock_helper.assert_awaited_once()
    call_kwargs = mock_helper.await_args.kwargs
    assert call_kwargs["model"] == "hf:zai-org/GLM-5.2"
    assert call_kwargs["label"] == "thread-agent"

    assert result.classification == "disagreed_valid"
    assert result.reply == "Understood, the retry loop swallows it on purpose."
    assert result.feedback_signal is not None
    assert result.feedback_signal["pattern"] == "except pass in retry loops is intentional"
    assert result.model == "hf:zai-org/GLM-5.2"


@pytest.mark.asyncio
async def test_thread_agent_synthetic_error_degrades_to_unclear():
    """Synthetic helper error / bad shape degrades to unclear, no crash."""
    agent = ThreadAgent(model="glm")

    error_meta = {
        "model": "hf:zai-org/GLM-5.2",
        "provider": "synthetic",
        "is_error": True,
        "error": "unparseable_json",
        "cost_usd": 0.0,
    }

    with patch(
        "baloo.agent.thread_agent.synthetic_json_completion",
        new_callable=AsyncMock,
    ) as mock_helper:
        mock_helper.return_value = (None, error_meta)

        result = await agent.classify(
            thread_comments=[
                _make_comment("baloo[bot]", "Finding", is_baloo=True),
                _make_comment("dev", "hmm"),
            ],
            code_context="code",
            file_path="f.py",
            line_number=1,
        )

    mock_helper.assert_awaited_once()
    assert result.classification == "unclear"
    assert result.reply is None
