"""Integration tests for BalooAgent with PI runtime."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from baloo.agent.client import BalooAgent
from baloo.github.models import FileChange, PRContext, PRDiscussionContext, PRMetadata
from baloo.processor.formatter import CommentFormatter


@pytest.fixture
def sample_pr_context():
    """Create a sample PR context for testing."""
    metadata = PRMetadata(
        repo_full_name="test/repo",
        pr_number=123,
        title="Test PR",
        description="Test description",
        author="testuser",
        base_branch="main",
        head_branch="feature",
        head_sha="abc123",
        files_changed=[
            FileChange(
                filename="test.py",
                status="modified",
                additions=10,
                deletions=5,
                changes=15,
                patch="@@ -1,5 +1,10 @@\n+new code",
            )
        ],
    )
    discussion = PRDiscussionContext()
    return PRContext(
        metadata=metadata,
        discussion=discussion,
        diff="diff --git a/test.py b/test.py\n@@ -1,5 +1,10 @@",
    )


def _make_pi_events(
    structured_output: dict | None, usage: dict = None, is_error: bool = False
) -> list[bytes]:
    """Build the sequence of JSONL events a PI process would emit."""
    usage = usage or {
        "input": 500,
        "output": 100,
        "cacheRead": 0,
        "cacheWrite": 0,
        "cost": {"total": 0.01},
    }

    if structured_output is not None:
        assistant_text = json.dumps(structured_output)
    else:
        assistant_text = ""

    events = [
        {"type": "response", "command": "set_thinking_level", "success": True},
        {"type": "response", "command": "prompt", "success": True},
        {"type": "agent_start"},
        {"type": "turn_start"},
        {"type": "message_start", "message": {"role": "assistant"}},
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": assistant_text}] if assistant_text else [],
                "model": "claude-sonnet-4-6",
                "usage": usage,
                "stopReason": "error" if is_error else "stop",
            },
        },
        {"type": "turn_end"},
        {"type": "agent_end"},
    ]
    return [json.dumps(e).encode("utf-8") + b"\n" for e in events]


def _mock_pi_process(events: list[bytes]):
    """Create a mocked PI subprocess with given events on stdout."""
    proc = AsyncMock()
    proc.returncode = None
    proc.stdin = AsyncMock()
    proc.stdin.write = MagicMock()
    proc.stdin.drain = AsyncMock()
    proc.stdout = AsyncMock(spec=asyncio.StreamReader)

    event_iter = iter(events)

    async def fake_readline():
        try:
            return next(event_iter)
        except StopIteration:
            return b""

    proc.stdout.readline = fake_readline
    proc.stderr = AsyncMock()
    proc.kill = MagicMock()
    proc.wait = AsyncMock()
    return proc


class TestBalooAgentErrorHandling:
    """Tests for error handling in BalooAgent."""

    @pytest.mark.asyncio
    async def test_review_pr_handles_process_crash(self, sample_pr_context):
        """Test graceful handling of PI process crash."""
        events = [
            json.dumps(
                {"type": "response", "command": "set_thinking_level", "success": True}
            ).encode()
            + b"\n",
            json.dumps({"type": "response", "command": "prompt", "success": True}).encode() + b"\n",
            b"",  # EOF — process crashed
        ]

        agent = BalooAgent()

        with patch("baloo.agent.pi_runtime.asyncio.create_subprocess_exec") as mock_exec:
            mock_exec.return_value = _mock_pi_process(events)
            result = await agent.review_pr(sample_pr_context)

            # Should return graceful result, not crash
            assert result.comments == []

    @pytest.mark.asyncio
    async def test_review_pr_handles_connection_error(self, sample_pr_context):
        """Test handling when PI binary cannot be spawned."""
        agent = BalooAgent()

        with patch(
            "baloo.agent.pi_runtime.asyncio.create_subprocess_exec",
            side_effect=FileNotFoundError("pi not found"),
        ):
            result = await agent.review_pr(sample_pr_context)

            assert "error" in result.summary.lower() or "failed" in result.summary.lower()
            assert result.comments == []

    @pytest.mark.asyncio
    async def test_review_pr_handles_empty_response(self, sample_pr_context):
        """No structured output is an agent failure, NOT a clean approval.

        Previously this asserted approve is True, which silently degraded a
        failed review into a false 'no issues' approval. The fix: an empty/
        unparseable response sets agent_error and must not approve.
        """
        events = _make_pi_events(None)
        agent = BalooAgent()

        with patch("baloo.agent.pi_runtime.asyncio.create_subprocess_exec") as mock_exec:
            mock_exec.return_value = _mock_pi_process(events)
            result = await agent.review_pr(sample_pr_context)

            assert result.comments == []
            assert result.metadata["agent_error"] is True
            assert result.approve is False
            assert result.request_changes is False
            # Summary must signal the failure, not a clean approval
            assert "Approve. Approve. Approve." not in result.summary

    @pytest.mark.asyncio
    async def test_max_turns_reached_sets_error_category(self, sample_pr_context):
        """When PI hits its turn limit, error_category must be max_turns_reached not no_output."""
        with patch.object(
            BalooAgent,
            "_run_with_fallback",
            new=AsyncMock(return_value=(None, {"max_turns_reached": True, "num_turns": 20})),
        ):
            agent = BalooAgent()
            result = await agent.review_pr(sample_pr_context)

        assert result.metadata["agent_error"] is True
        assert result.metadata["error_category"] == "max_turns_reached"

    @pytest.mark.asyncio
    async def test_review_pr_normalizes_top_level_findings_array(self, sample_pr_context):
        """GLM-5.2 sometimes returns findings as a top-level JSON array instead
        of {"findings":[...]}. That output must be NORMALIZED into real comments
        end-to-end — not discarded into a fail-closed agent_error (the #677 bug).
        """
        array_output = [
            {
                "file": "a.py",
                "line": 3,
                "severity": "HIGH",
                "category": "Security",
                "title": "Command injection",
                "description": "unsanitized input flows into os.system",
            }
        ]
        with patch.object(
            BalooAgent,
            "_run_with_fallback",
            new=AsyncMock(return_value=(array_output, {"model": "glm", "output_tokens": 100})),
        ):
            agent = BalooAgent()
            result = await agent.review_pr(sample_pr_context)

        assert result.metadata.get("agent_error") is not True
        assert len(result.comments) == 1
        assert result.comments[0].path == "a.py"
        assert result.request_changes is True  # HIGH security -> request changes
        assert result.approve is False

    @pytest.mark.asyncio
    async def test_review_pr_non_review_dict_fails_closed(self, sample_pr_context):
        """A valid JSON object that is NOT a review (no findings list) must fail
        closed, not silently approve with zero comments."""
        with patch.object(
            BalooAgent,
            "_run_with_fallback",
            new=AsyncMock(return_value=({"summary": {"total_issues": 0}}, {"model": "glm"})),
        ):
            agent = BalooAgent()
            result = await agent.review_pr(sample_pr_context)

        assert result.comments == []
        assert result.metadata["agent_error"] is True
        assert result.approve is False
        assert "Approve. Approve. Approve." not in result.summary

    @pytest.mark.asyncio
    async def test_json_retry_with_zero_findings_fails_closed(self, sample_pr_context):
        """A JSON retry means the agent's primary output was unparseable. Recovering
        ZERO findings from that is not trustworthy enough to bless a clean approval —
        this is exactly what false-approved a real eval() RCE in production (GLM
        emitted prose, the json_object retry returned an empty/echo object, and the
        empty result degraded into 'No issue. Code good.').
        """
        with patch.object(
            BalooAgent,
            "_run_with_fallback",
            new=AsyncMock(
                return_value=(
                    {"findings": [], "summary": {"total_issues": 0}},
                    {"json_retry": True, "model": "glm", "output_tokens": 120},
                )
            ),
        ):
            agent = BalooAgent()
            result = await agent.review_pr(sample_pr_context)

        assert result.comments == []
        assert result.metadata["agent_error"] is True
        assert result.metadata["error_category"] == "json_parse_error"
        assert result.approve is False
        assert result.request_changes is False
        # Must not degrade into a clean approval
        assert "Approve. Approve. Approve." not in result.summary

    @pytest.mark.asyncio
    async def test_review_pr_handles_error_stop_reason(self, sample_pr_context):
        """Test handling of error stop reason from PI."""
        events = _make_pi_events(None, is_error=True)
        agent = BalooAgent()

        with patch("baloo.agent.pi_runtime.asyncio.create_subprocess_exec") as mock_exec:
            mock_exec.return_value = _mock_pi_process(events)
            result = await agent.review_pr(sample_pr_context)

            assert result.comments == []


class TestBalooAgentSuccessPath:
    """Tests for successful agent execution with structured output."""

    @pytest.mark.asyncio
    async def test_review_pr_successful_with_findings(self, sample_pr_context):
        """Test successful review with valid structured findings."""
        structured = {
            "findings": [
                {
                    "file": "test.py",
                    "line": 5,
                    "severity": "HIGH",
                    "category": "Security",
                    "title": "SQL Injection",
                    "description": "Unsafe query",
                }
            ],
            "summary": {"total_issues": 1},
        }
        events = _make_pi_events(structured)
        agent = BalooAgent()

        with patch("baloo.agent.pi_runtime.asyncio.create_subprocess_exec") as mock_exec:
            mock_exec.return_value = _mock_pi_process(events)
            result = await agent.review_pr(sample_pr_context)

            assert len(result.comments) == 1
            assert result.comments[0].path == "test.py"
            assert result.comments[0].severity == "HIGH"  # Security enforced to HIGH
            assert result.approve is False  # HIGH severity still requests changes
            assert result.request_changes is True

    @pytest.mark.asyncio
    async def test_review_pr_successful_no_findings(self, sample_pr_context):
        """Test successful review with no findings."""
        structured = {"findings": [], "summary": {"total_issues": 0}}
        events = _make_pi_events(structured)
        agent = BalooAgent()

        with patch("baloo.agent.pi_runtime.asyncio.create_subprocess_exec") as mock_exec:
            mock_exec.return_value = _mock_pi_process(events)
            result = await agent.review_pr(sample_pr_context)

            assert result.comments == []
            assert result.approve is True
            assert result.request_changes is False
            assert "Approve. Approve. Approve." in result.summary

    @pytest.mark.asyncio
    async def test_review_pr_medium_severity_approves(self, sample_pr_context):
        """Test that MEDIUM/LOW severity issues don't block approval."""
        structured = {
            "findings": [
                {"file": "test.py", "line": 1, "severity": "MEDIUM", "title": "Minor issue"}
            ],
        }
        events = _make_pi_events(structured)
        agent = BalooAgent()

        with patch("baloo.agent.pi_runtime.asyncio.create_subprocess_exec") as mock_exec:
            mock_exec.return_value = _mock_pi_process(events)
            result = await agent.review_pr(sample_pr_context)

            assert len(result.comments) == 1
            assert result.approve is True  # MEDIUM doesn't block
            assert result.request_changes is False


class TestBalooAgentModelSelection:
    """Tests for model selection logic."""

    @pytest.mark.asyncio
    async def test_uses_haiku_for_simple_pr(self):
        """Test that Haiku model is set for simple PRs."""
        simple_context = PRContext(
            metadata=PRMetadata(
                repo_full_name="test/repo",
                pr_number=1,
                title="Update README",
                description="Update docs",
                author="dev",
                base_branch="main",
                head_branch="docs",
                head_sha="abc",
                files_changed=[
                    FileChange(
                        filename="README.md", status="modified", additions=2, deletions=1, changes=3
                    )
                ],
            ),
            discussion=PRDiscussionContext(),
            diff="diff --git a/README.md",
        )

        events = _make_pi_events({"findings": [], "summary": {}})
        agent = BalooAgent()

        with patch("baloo.agent.pi_runtime.asyncio.create_subprocess_exec") as mock_exec:
            mock_exec.return_value = _mock_pi_process(events)
            await agent.review_pr(simple_context, model_override="haiku")

            assert agent.options.model == "claude-haiku-4-5-20251001"

    @pytest.mark.asyncio
    async def test_uses_sonnet_for_complex_pr(self, sample_pr_context):
        """Test that Sonnet is used for complex PRs."""
        sample_pr_context.files_changed.extend(
            [
                FileChange(
                    filename=f"file{i}.py", status="added", additions=50, deletions=0, changes=50
                )
                for i in range(5)
            ]
        )

        events = _make_pi_events({"findings": [], "summary": {}})
        agent = BalooAgent()

        with patch("baloo.agent.pi_runtime.asyncio.create_subprocess_exec") as mock_exec:
            mock_exec.return_value = _mock_pi_process(events)
            await agent.review_pr(sample_pr_context, model_override="sonnet")

            assert agent.options.model == "claude-sonnet-4-6"

    @pytest.mark.asyncio
    async def test_uses_opus_for_security_pr(self):
        """Test that Opus is used for security-sensitive PRs."""
        security_context = PRContext(
            metadata=PRMetadata(
                repo_full_name="test/repo",
                pr_number=1,
                title="Update auth flow",
                description="Refactor authentication",
                author="dev",
                base_branch="main",
                head_branch="feature/auth",
                head_sha="abc",
                files_changed=[
                    FileChange(
                        filename="src/auth/login.py",
                        status="modified",
                        additions=20,
                        deletions=5,
                        changes=25,
                    )
                ],
            ),
            discussion=PRDiscussionContext(),
            diff="diff --git a/src/auth/login.py b/src/auth/login.py\n+def verify_token():",
        )

        events = _make_pi_events({"findings": [], "summary": {}})
        agent = BalooAgent()

        with patch("baloo.agent.pi_runtime.asyncio.create_subprocess_exec") as mock_exec:
            mock_exec.return_value = _mock_pi_process(events)
            await agent.review_pr(security_context, model_override="opus")

            assert agent.options.model == "claude-opus-4-6"


class TestBalooAgentSeveritySummary:
    """Tests for severity summary generation."""

    @pytest.mark.asyncio
    async def test_summary_includes_critical_severity(self, sample_pr_context):
        """Test that CRITICAL severity is included in summary."""
        structured = {
            "findings": [
                {
                    "file": "test.py",
                    "line": 1,
                    "severity": "CRITICAL",
                    "category": "Security",
                    "title": "Critical issue",
                }
            ],
        }
        events = _make_pi_events(structured)
        agent = BalooAgent()

        with patch("baloo.agent.pi_runtime.asyncio.create_subprocess_exec") as mock_exec:
            mock_exec.return_value = _mock_pi_process(events)
            result = await agent.review_pr(sample_pr_context)

            assert "Critical" in result.summary
            assert "🔴" in result.summary

    @pytest.mark.asyncio
    async def test_summary_includes_low_severity(self, sample_pr_context):
        """Test that LOW severity is included in summary."""
        structured = {
            "findings": [{"file": "test.py", "line": 1, "severity": "LOW", "title": "Low issue"}],
        }
        events = _make_pi_events(structured)
        agent = BalooAgent()

        with patch("baloo.agent.pi_runtime.asyncio.create_subprocess_exec") as mock_exec:
            mock_exec.return_value = _mock_pi_process(events)
            result = await agent.review_pr(sample_pr_context)

            assert "Low" in result.summary
            assert "🔵" in result.summary


class TestBalooAgentFallback:
    """Tests for model fallback behavior."""

    @pytest.mark.asyncio
    async def test_fallback_to_secondary_model(self, sample_pr_context):
        """Test that primary failure triggers fallback to secondary model."""
        # Primary model fails
        fail_events = [
            json.dumps(
                {"type": "response", "command": "set_thinking_level", "success": True}
            ).encode()
            + b"\n",
            json.dumps(
                {
                    "type": "response",
                    "command": "prompt",
                    "success": False,
                    "error": "API key invalid",
                }
            ).encode()
            + b"\n",
        ]
        # Fallback model succeeds
        success_events = _make_pi_events({"findings": [], "summary": {}})

        agent = BalooAgent()
        call_count = 0

        with patch("baloo.agent.pi_runtime.asyncio.create_subprocess_exec") as mock_exec:

            def side_effect(*args, **kwargs):
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    return _mock_pi_process(fail_events)
                return _mock_pi_process(success_events)

            mock_exec.side_effect = side_effect

            result = await agent.review_pr(sample_pr_context)

            # Should succeed via fallback
            assert result.approve is True
            assert result.metadata.get("fallback_used") is True
            assert "primary_error" in result.metadata

    @pytest.mark.asyncio
    async def test_no_fallback_when_same_model(self, sample_pr_context):
        """Test that fallback is skipped when it's the same as primary."""
        fail_events = [
            json.dumps(
                {"type": "response", "command": "set_thinking_level", "success": True}
            ).encode()
            + b"\n",
            json.dumps(
                {"type": "response", "command": "prompt", "success": False, "error": "API error"}
            ).encode()
            + b"\n",
        ]

        agent = BalooAgent()
        # Set fallback to same as primary
        with patch(
            "baloo.config.settings.settings.agent_fallback_model",
            f"{agent.options.provider}/{agent.options.model}",
        ):
            with patch("baloo.agent.pi_runtime.asyncio.create_subprocess_exec") as mock_exec:
                mock_exec.return_value = _mock_pi_process(fail_events)
                result = await agent.review_pr(sample_pr_context)

                # Should fail (no fallback attempted)
                assert "error" in result.summary.lower() or "failed" in result.summary.lower()


class TestBalooAgentMetadata:
    """Tests for metadata in reviews."""

    @pytest.mark.asyncio
    async def test_metadata_captures_cost_and_tokens(self, sample_pr_context):
        """Test that metadata includes cost and token counts."""
        usage = {
            "input": 2000,
            "output": 500,
            "cacheRead": 1000,
            "cacheWrite": 200,
            "cost": {"total": 0.10},
        }
        structured = {"findings": [], "summary": {}}
        events = _make_pi_events(structured, usage=usage)
        agent = BalooAgent()

        with patch("baloo.agent.pi_runtime.asyncio.create_subprocess_exec") as mock_exec:
            mock_exec.return_value = _mock_pi_process(events)
            result = await agent.review_pr(sample_pr_context)

            assert result.metadata["input_tokens"] == 2000
            assert result.metadata["output_tokens"] == 500
            assert result.metadata["cache_read_tokens"] == 1000
            assert result.metadata["cache_write_tokens"] == 200
            assert result.metadata["cost_usd"] == pytest.approx(0.10)

    def test_format_summary_agent_error_is_not_clean_approval(self):
        """An agent_error summary must signal failure, never a clean approval."""
        metadata = {
            "model": "hf:zai-org/GLM-5.2",
            "agent_error": True,
            "error_category": "no_output",
            "input_tokens": 800,
            "output_tokens": 0,
            "cost_usd": 0.0,
            "num_turns": 1,
            "duration_seconds": 5.0,
        }

        result = CommentFormatter.format_summary([], metadata)

        assert "Approve. Approve. Approve." not in result
        assert "No finish" in result

    def test_format_metadata_section(self):
        """Test summary formatting with metadata."""
        metadata = {
            "model": "claude-sonnet-4-6",
            "input_tokens": 2000,
            "output_tokens": 500,
            "cache_read_tokens": 1000,
            "cache_write_tokens": 200,
            "thinking_tokens": 0,
            "thinking_budget": None,
            "cost_usd": 0.10,
            "num_turns": 3,
            "duration_seconds": 15.0,
        }

        result = CommentFormatter.format_summary([], metadata)

        assert "**Model:** `claude-sonnet-4-6`" in result
        assert "**Cache:** 200 write / 1,000 read" in result
        assert "**Cost:** $0.1000" in result
