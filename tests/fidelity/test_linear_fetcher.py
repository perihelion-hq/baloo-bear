import json
from unittest.mock import patch

import pytest

from baloo.fidelity.linear_fetcher import fetch_linear_issue_content


class _Response:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return json.dumps(
            {
                "data": {
                    "issue": {
                        "identifier": "PER-603",
                        "title": "Record Hatchet endpoint boundary",
                        "description": "## Goal\n\nRecord the trust posture.",
                        "url": "https://linear.app/example/issue/PER-603/example",
                        "team": {"key": "PER", "name": "Perihelion"},
                        "state": {"name": "In Progress"},
                        "comments": {"nodes": []},
                    }
                }
            }
        ).encode()


@pytest.mark.asyncio
async def test_fetch_linear_issue_formats_plan_content(monkeypatch):
    monkeypatch.setenv("LINEAR_API_KEY", "lin_api_key")
    from baloo.config.settings import reset_settings

    reset_settings()
    with patch("baloo.fidelity.linear_fetcher.request.urlopen", return_value=_Response()):
        content = await fetch_linear_issue_content("PER-603")

    assert "# Linear Issue PER-603: Record Hatchet endpoint boundary" in content
    assert "Record the trust posture" in content
    assert "https://linear.app/example/issue/PER-603/example" in content
