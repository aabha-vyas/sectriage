"""Token prices and `cost_usd`.

Verified on 2026-10-24 against https://platform.claude.com/docs/en/about-claude/pricing
(the page shows no "last updated" date, so re-check it when this date gets old). Rates are USD per
million tokens. Only models whose full rate row was verified are listed: an unknown model raises
rather than silently costing $0. Sonnet 5.5 and Opus 5.5 are left out until the footnote on their
cache-hit prices has been read; add them from the same page.

Cache-read ratios differ per model, so each model carries explicit rates instead of multipliers.
"""

import hashlib
import json
from dataclasses import asdict, dataclass

VERIFIED_ON = "2026-10-24"


class UnknownModelError(KeyError):
    pass


@dataclass(frozen=True)
class Rates:
    input: float
    cache_write_5m: float
    cache_read: float
    output: float


@dataclass(frozen=True)
class ModelPrice:
    base: Rates
    # Applies to a request whose whole prompt (input + cache writes + cache reads) exceeds the
    # threshold. Each request is priced on its own.
    long_prompt_threshold: int | None = None
    long_prompt: Rates | None = None


PRICES: dict[str, ModelPrice] = {
    "claude-haiku-5-5": ModelPrice(
        base=Rates(input=0.10, cache_write_5m=0.125, cache_read=0.01, output=0.50),
        long_prompt_threshold=100_000,
        long_prompt=Rates(input=0.50, cache_write_5m=0.625, cache_read=0.05, output=2.50),
    ),
}


def cost_usd(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_write_tokens: int = 0,
    cache_read_tokens: int = 0,
) -> float:
    """Cost of one request. `input_tokens` excludes cache reads and writes (as the API reports)."""
    try:
        price = PRICES[model]
    except KeyError:
        raise UnknownModelError(
            f"no price for model {model!r}; supported: {', '.join(sorted(PRICES))}"
        ) from None
    rates = price.base
    if price.long_prompt and price.long_prompt_threshold is not None:
        prompt_len = input_tokens + cache_write_tokens + cache_read_tokens
        if prompt_len > price.long_prompt_threshold:
            rates = price.long_prompt
    return (
        input_tokens * rates.input
        + cache_write_tokens * rates.cache_write_5m
        + cache_read_tokens * rates.cache_read
        + output_tokens * rates.output
    ) / 1_000_000


def price_table_version(model: str) -> str | None:
    """SHA-256 of the rate row for `model`; None if the model has no price.

    It changes when any rate or threshold changes, not when `VERIFIED_ON` is re-checked, so a
    date refresh does not make cost numbers incomparable.
    """
    price = PRICES.get(model)
    if price is None:
        return None
    blob = json.dumps(asdict(price), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()
