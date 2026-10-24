from datetime import date

import pytest

from src.agent import pricing
from src.agent.pricing import UnknownModelError, cost_usd

HAIKU = "claude-haiku-5-5"


def test_verification_date_is_recorded():
    assert date.fromisoformat(pricing.VERIFIED_ON)


def test_plain_input_and_output():
    # Per-MTok rates, scaled to stay under the 100K long-prompt threshold.
    assert cost_usd(HAIKU, 100_000, 0) == pytest.approx(0.10 * 0.1)
    assert cost_usd(HAIKU, 0, 1_000_000) == pytest.approx(0.50)


def test_cache_write_and_read_are_priced_separately():
    assert cost_usd(HAIKU, 0, 0, cache_write_tokens=100_000) == pytest.approx(0.125 * 0.1)
    assert cost_usd(HAIKU, 0, 0, cache_read_tokens=100_000) == pytest.approx(0.01 * 0.1)
    # 100 + 250 + 100 + 250 micro-dollars
    assert cost_usd(HAIKU, 1000, 500, 2000, 10_000) == pytest.approx(0.0007)


def test_long_prompt_tier_counts_cache_tokens_toward_the_threshold():
    # prompt = 50k input + 60k cache read = 110k > 100k, so the whole request uses the higher tier.
    expected = (50_000 * 0.50 + 60_000 * 0.05 + 1_000 * 2.50) / 1_000_000
    assert cost_usd(HAIKU, 50_000, 1_000, cache_read_tokens=60_000) == pytest.approx(expected)


def test_long_prompt_threshold_is_exclusive():
    at = cost_usd(HAIKU, 100_000, 0)
    over = cost_usd(HAIKU, 100_001, 0)
    assert at == pytest.approx(100_000 * 0.10 / 1_000_000)
    assert over == pytest.approx(100_001 * 0.50 / 1_000_000)


def test_unknown_model_raises_instead_of_costing_zero():
    with pytest.raises(UnknownModelError, match="claude-haiku-5-5"):
        cost_usd("claude-made-up", 10, 10)
