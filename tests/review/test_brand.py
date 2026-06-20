"""Tests for the brand prefix used in Rocky's GitHub comments."""

from baloo.review.orchestrator import _brand_prefix


def test_brand_prefix_uses_rock_emoji_not_bear():
    """Rocky is the Project Hail Mary alien, not a bear — the comment prefix
    should lead with the rock emoji (🪨), never the legacy bear (🐻)."""
    prefix = _brand_prefix()
    assert prefix.startswith("🪨")
    assert "🐻" not in prefix
    # brand_name still present so is_baloo_actor self-detection keeps working
    assert "Rocky" in prefix
