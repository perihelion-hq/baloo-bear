"""LLM-powered false-positive verification for review findings.

After the main review agent produces findings, this module re-examines
each one in isolation using a cheap/fast model to classify it as a real
issue or a false positive.  FPs are dropped before posting.

Design: fail-open — if verification errors, the finding is kept.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from baloo.agent.config import get_agent_options
from baloo.agent.pi_runtime import PIAgentBase, PIAgentOptions
from baloo.agent.synthetic_json import synthetic_json_completion
from baloo.config.settings import get_settings
from baloo.github.models import PRContext, ReviewComment
from baloo.processor.fp_prompts import (
    FP_SYSTEM_PROMPT,
    build_verification_prompt,
    extract_diff_for_file,
)

logger = logging.getLogger(__name__)


@dataclass
class FPRejection:
    """A finding that was classified as a false positive."""

    comment: ReviewComment
    reason: str
    model: str
    cost_usd: float = 0.0


@dataclass
class FPStats:
    """Aggregate stats for the verification pass."""

    total_verified: int = 0
    kept: int = 0
    rejected: int = 0
    errors: int = 0
    total_cost_usd: float = 0.0
    duration_seconds: float = 0.0


@dataclass
class FPVerificationResult:
    """Result of the FP verification pass."""

    verified: list[ReviewComment] = field(default_factory=list)
    rejected: list[FPRejection] = field(default_factory=list)
    stats: FPStats = field(default_factory=FPStats)


class _FPVerifierAgent(PIAgentBase):
    """Thin PI agent for single-turn FP verification calls."""

    # Override the base retry prompt which asks for "findings"/"summary"
    # keys — the FP verifier expects a different schema.
    _JSON_RETRY_PROMPT = (
        "Your previous response could not be parsed as JSON. "
        "Respond with ONLY a raw JSON object, no markdown, no explanation: "
        '{"verdict": "real", "reason": "one concise sentence"} '
        'or {"verdict": "fp", "reason": "one concise sentence"}'
    )

    def __init__(self, options: PIAgentOptions):
        super().__init__(options)
        self.agent_name = "FPVerifier"


class FPVerifier:
    """Verify review findings and drop false positives.

    Each finding is checked independently by a cheap model.  Verifications
    run concurrently up to ``max_concurrent``.
    """

    def __init__(
        self,
        model: str | None = None,
        max_concurrent: int | None = None,
    ):
        settings = get_settings()
        self.model = model or settings.fp_verification_model
        self.max_concurrent = max_concurrent or settings.fp_verification_max_concurrent
        self.audit_log_path = settings.fp_audit_log_path

    async def verify(
        self,
        comments: list[ReviewComment],
        pr_context: PRContext,
    ) -> FPVerificationResult:
        """Verify a list of findings and return filtered results.

        Args:
            comments: Findings from the review agent.
            pr_context: PR context (for diff and metadata).

        Returns:
            FPVerificationResult with verified (kept) and rejected findings.
        """
        if not comments:
            return FPVerificationResult()

        start_time = time.time()
        semaphore = asyncio.Semaphore(self.max_concurrent)

        async def _verify_one(comment: ReviewComment) -> tuple[ReviewComment, dict]:
            async with semaphore:
                return await self._verify_single(comment, pr_context)

        # Run all verifications concurrently
        tasks = [_verify_one(c) for c in comments]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Collect results
        result = FPVerificationResult()
        stats = FPStats(total_verified=len(comments))

        for i, res in enumerate(results):
            comment = comments[i]

            if isinstance(res, Exception):
                # Fail-open: keep the finding on error
                logger.warning(
                    "FP verification error for %s:%s — keeping finding: %s",
                    comment.path,
                    comment.line,
                    res,
                )
                result.verified.append(comment)
                # The finding is kept and passed downstream, so it counts
                # toward `kept`. `errors` is an orthogonal counter tracking
                # how many were kept via the error path vs. a clean verdict.
                stats.kept += 1
                stats.errors += 1
                # Record the error in the audit log so systemic verification
                # failures are observable (otherwise every finding appears
                # "correctly kept" while the pass is silently a no-op).
                self._write_audit_entry(
                    comment=comment,
                    verdict="error",
                    reason=str(res),
                    model=self.model,
                    cost_usd=0.0,
                    pr_context=pr_context,
                    provider=None,
                    error=str(res),
                )
                continue

            verified_comment, verdict_data = res
            verdict = verdict_data.get("verdict", "real")
            reason = verdict_data.get("reason", "no reason given")
            cost = verdict_data.get("cost_usd", 0.0)
            model_used = verdict_data.get("model", self.model)
            provider_used = verdict_data.get("provider")
            # A non-None `error` means the verdict could not be obtained and the
            # finding was kept fail-open; count it under `errors` for parity
            # with the exception path so a systemic failure is observable.
            verdict_error = verdict_data.get("error")
            if verdict_error:
                stats.errors += 1

            stats.total_cost_usd += cost

            if verdict == "fp":
                rejection = FPRejection(
                    comment=comment,
                    reason=reason,
                    model=model_used,
                    cost_usd=cost,
                )
                result.rejected.append(rejection)
                stats.rejected += 1
                logger.info(
                    "FP rejected: %s:%s [%s] — %s",
                    comment.path,
                    comment.line,
                    comment.severity,
                    reason,
                )
            else:
                result.verified.append(comment)
                stats.kept += 1

            # Write audit log entry
            self._write_audit_entry(
                comment=comment,
                verdict=verdict,
                reason=reason,
                model=model_used,
                cost_usd=cost,
                pr_context=pr_context,
                provider=provider_used,
                error=verdict_error,
            )

        stats.duration_seconds = time.time() - start_time
        result.stats = stats

        logger.info(
            "FP verification complete: %d kept (of which %d via error fail-open), "
            "%d rejected, cost=$%.4f, duration=%.1fs",
            stats.kept,
            stats.errors,
            stats.rejected,
            stats.total_cost_usd,
            stats.duration_seconds,
        )

        return result

    async def _verify_single(
        self,
        comment: ReviewComment,
        pr_context: PRContext,
    ) -> tuple[ReviewComment, dict]:
        """Verify a single finding.

        Returns:
            Tuple of (original comment, verdict dict with keys: verdict, reason, cost_usd, model).
        """
        # Build context for this finding
        diff_context = extract_diff_for_file(pr_context.diff, comment.path)

        prompt = build_verification_prompt(
            comment=comment,
            diff_context=diff_context,
            file_context=None,  # Start with diff only; add file reads later if needed
            pr_title=pr_context.title,
            pr_description=pr_context.description,
            pr_commit_messages=pr_context.metadata.commit_messages or None,
        )

        # Resolve the configured provider/model. The default (glm/synthetic)
        # runs the verdict over the always-keyed Synthetic direct-HTTP
        # json_object path; non-synthetic models fall back to the pi runtime.
        opts = get_agent_options(self.model, thinking_level="off")
        provider = opts.provider
        model_id = opts.model

        if provider == "synthetic":
            # Synthetic direct-HTTP path: response_format=json_object forces a
            # JSON object out of GLM-5.2. The helper never raises — transport
            # or parse failures arrive as parsed=None + meta.is_error.
            parsed, meta = await synthetic_json_completion(
                model=model_id,
                system_prompt=FP_SYSTEM_PROMPT,
                user_prompt=prompt,
                label="FPVerifier",
            )
            return self._build_verdict(comment, parsed, meta, provider, model_id)

        # Non-synthetic fallback (e.g. anthropic): keep the existing pi
        # runtime path that drives a no-tools single-turn query.
        options = get_agent_options(
            model=self.model,
            thinking_level="off",
        )
        options.system_prompt = FP_SYSTEM_PROMPT
        # Disable tools: all necessary context (diff hunks) is already
        # embedded in the prompt. Without tools the model cannot waste
        # turns on file reads (which fail for new files anyway) and a
        # single turn is sufficient for the JSON verdict.
        options.no_tools = True
        options.max_turns = 2

        agent = _FPVerifierAgent(options)

        try:
            structured, metadata = await agent.run_query(prompt)
        except Exception as exc:
            logger.warning(
                "FP verification failed for %s:%s: %s",
                comment.path,
                comment.line,
                exc,
            )
            raise

        # The pi runtime never signals is_error in metadata; reuse the shared
        # verdict builder which fails open on a missing/invalid verdict shape.
        return self._build_verdict(comment, structured, metadata, provider, model_id)

    def _build_verdict(
        self,
        comment: ReviewComment,
        parsed: object,
        meta: dict,
        provider: str,
        model_id: str,
    ) -> tuple[ReviewComment, dict]:
        """Normalize a model response into a verdict dict (fail-open).

        A valid verdict requires ``parsed`` to be a dict whose ``verdict`` is
        ``"real"`` or ``"fp"`` and ``meta`` to not signal an error. Anything
        else is kept (fail-open) but the verdict dict carries an ``error``
        category and provider/model so a systemic failure (e.g. the
        placeholder-key bug) stays observable in the audit log.
        """
        cost = meta.get("cost_usd", 0.0)
        model_used = meta.get("model", model_id)

        verdict = parsed.get("verdict") if isinstance(parsed, dict) else None
        reason = parsed.get("reason") if isinstance(parsed, dict) else None

        # A valid verdict must be real|fp AND carry a non-empty reason. A drop
        # ("fp") with no reason is malformed — never silently drop a finding on
        # an unexplained verdict; fail open and record the error instead.
        if (
            not meta.get("is_error")
            and verdict in ("real", "fp")
            and isinstance(reason, str)
            and reason.strip()
        ):
            return comment, {
                "verdict": verdict,
                "reason": reason.strip(),
                "cost_usd": cost,
                "model": model_used,
                "provider": provider,
            }

        # Fail-open: keep the finding, but surface why the verdict was unusable.
        if meta.get("is_error"):
            error = meta.get("error") or "verification error"
        elif parsed is None:
            error = "empty response (possible tool-use abort)"
        elif not isinstance(parsed, dict):
            error = "unparseable verdict"
        elif verdict not in ("real", "fp"):
            error = "invalid verdict value"
        else:
            error = "missing or empty reason"
        logger.warning(
            "FP verifier could not obtain a verdict for %s:%s (%s) — keeping",
            comment.path,
            comment.line,
            error,
        )
        return comment, {
            "verdict": "real",
            "reason": error,
            "error": error,
            "cost_usd": cost,
            "model": model_used,
            "provider": provider,
        }

    def _write_audit_entry(
        self,
        comment: ReviewComment,
        verdict: str,
        reason: str,
        model: str,
        cost_usd: float,
        pr_context: PRContext,
        provider: str | None = None,
        error: str | None = None,
    ) -> None:
        """Append a JSONL audit log entry.

        ``provider`` records which lane produced the verdict; ``error`` carries
        a short failure category when the verdict could not be obtained (the
        finding was then kept fail-open). Together they make a systemic failure
        such as the placeholder-key bug observable from the audit log.
        """
        if not self.audit_log_path:
            return

        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "repo": pr_context.repo_full_name,
            "pr_number": pr_context.pr_number,
            "commit_sha": getattr(pr_context, "head_sha", None),
            "finding": {
                "file": comment.path,
                "line": comment.line,
                "severity": comment.severity,
                "category": comment.category,
                "title": _extract_title(comment.body),
            },
            "verdict": verdict,
            "reason": reason,
            "model": model,
            "provider": provider,
            "review_model": get_settings().agent_model,
            "cost_usd": cost_usd,
        }
        if error:
            entry["error"] = error

        try:
            path = Path(self.audit_log_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception as exc:
            logger.warning("Failed to write FP audit log: %s", exc)


def _extract_title(body: str) -> str:
    """Extract the title from a formatted comment body."""
    for line in body.split("\n"):
        line = line.strip()
        if line.startswith("**") and line.endswith("**"):
            return line.strip("*").strip()
        if line and not line.startswith("**Category") and not line.startswith("**Severity"):
            return line.strip("*").strip()[:100]
    return ""
