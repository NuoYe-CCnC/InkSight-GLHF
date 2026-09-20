"""Verified model capability registry for news generation.

DeepSeek official model/pricing docs list a 1M context and 384K maximum output
for ``deepseek-v4-flash``.  The small minimums below are InkSight policy floors,
not provider limits: they reserve enough conservative space for mandatory
instructions, two candidates, and a structured JSON response.
"""
from __future__ import annotations

MODEL_LIMITS = {
    "deepseek-v4-flash": {
        "context_tokens": 1_000_000,
        "max_output_tokens": 384_000,
        "source": "https://api-docs.deepseek.com/quick_start/pricing/",
        "verified": "2026-09-10",
    },
}

MIN_INPUT_TOKENS = 2300
MIN_OUTPUT_TOKENS = 128


def capability(model: str) -> dict:
    row = MODEL_LIMITS.get(str(model))
    if not row:
        raise ValueError(f"unknown model capability: {model}")
    return dict(row)


def validate_generation_limits(model: str, input_tokens, output_tokens) -> list[str]:
    errors = []
    for name, value in (("max_input_tokens", input_tokens),
                        ("max_output_tokens", output_tokens)):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            errors.append(f"{name}: expected positive integer")
    try:
        limits = capability(model)
    except ValueError as exc:
        return errors + [str(exc)]
    if errors:
        return errors
    if input_tokens < MIN_INPUT_TOKENS:
        errors.append(f"max_input_tokens: {input_tokens} is below InkSight minimum {MIN_INPUT_TOKENS}")
    if output_tokens < MIN_OUTPUT_TOKENS:
        errors.append(f"max_output_tokens: {output_tokens} is below InkSight minimum {MIN_OUTPUT_TOKENS}")
    if output_tokens > limits["max_output_tokens"]:
        errors.append(f"max_output_tokens: exceeds verified model maximum {limits['max_output_tokens']}")
    if input_tokens + output_tokens > limits["context_tokens"]:
        errors.append(f"max_input_tokens + max_output_tokens: exceeds verified context {limits['context_tokens']}")
    return errors
