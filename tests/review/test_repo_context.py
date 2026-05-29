import tempfile
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

from baloo.agent.client import BalooAgent
from baloo.github.models import FileChange, PRContext, PRDiscussionContext, PRMetadata
from baloo.review.repo_context import (
    collect_repository_guidelines,
    enrich_pr_context_with_repository_guidelines,
    materialize_repository,
)


def test_baloo_agent_accepts_target_repo_cwd():
    agent = BalooAgent(cwd="/tmp/target-repo")
    assert agent.options.cwd == "/tmp/target-repo"


def test_collects_root_and_closest_agent_guidelines_for_changed_paths():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "AGENTS.md").write_text("root rules")
        (root / "CONTRIBUTING.md").write_text("contrib rules")
        (root / "server").mkdir()
        (root / "server" / "AGENTS.md").write_text("server rules")
        (root / "server" / "orchestrator").mkdir()
        (root / "server" / "orchestrator" / "AGENTS.md").write_text("orchestrator rules")
        (root / "infra").mkdir()
        (root / "infra" / "AGENTS.md").write_text("infra rules")

        guidelines = collect_repository_guidelines(
            root,
            ["server/orchestrator/app.py", "infra/deploy/main.tf"],
        )

    assert "## AGENTS.md" in guidelines
    assert "root rules" in guidelines
    assert "## server/AGENTS.md" in guidelines
    assert "server rules" in guidelines
    assert "## server/orchestrator/AGENTS.md" in guidelines
    assert "orchestrator rules" in guidelines
    assert "## infra/AGENTS.md" in guidelines
    assert "infra rules" in guidelines
    assert "## CONTRIBUTING.md" in guidelines
    assert "contrib rules" in guidelines


def test_enriches_pr_context_guidelines_from_repo_checkout():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "AGENTS.md").write_text("root rules")
        (root / "server").mkdir()
        (root / "server" / "AGENTS.md").write_text("server rules")
        pr_context = PRContext(
            metadata=PRMetadata(
                repo_full_name="org/repo",
                pr_number=1,
                title="test",
                description="",
                author="me",
                base_branch="main",
                head_branch="feat/test",
                head_sha="abc",
                files_changed=[
                    FileChange(
                        filename="server/app.py",
                        status="modified",
                        additions=1,
                        deletions=0,
                        changes=1,
                    )
                ],
                repo_guidelines="old root guideline",
            ),
            discussion=PRDiscussionContext(),
            diff="diff",
        )
        enriched = enrich_pr_context_with_repository_guidelines(pr_context, root)

    assert enriched is not pr_context
    assert "old root guideline" in enriched.repo_guidelines
    assert "server rules" in enriched.repo_guidelines


def test_materialize_repository_uses_credential_file_not_token_in_git_args():
    captured_args = []

    def fake_run(args, **kwargs):
        captured_args.append(args)
        return CompletedProcess(args, 0)

    with tempfile.TemporaryDirectory() as tmp:
        with (
            patch("baloo.review.repo_context.settings.repo_checkout_enabled", True),
            patch("baloo.review.repo_context.settings.repo_checkout_root", tmp),
            patch(
                "baloo.review.repo_context.GitHubAuth.get_installation_token",
                return_value="secret-token-value",
            ),
            patch("baloo.review.repo_context.subprocess.run", side_effect=fake_run),
        ):
            checkout = materialize_repository("org/repo", "abcdef123456", 123)

        assert checkout is not None
        assert checkout.exists()
        assert "secret-token-value" not in repr(captured_args)
        assert not list(Path(tmp).glob("*-auth-*"))
