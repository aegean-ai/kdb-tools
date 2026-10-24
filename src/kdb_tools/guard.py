"""Refuse a prompt that would not fit the model's context.

A server that receives a longer prompt does not reject it. Ollama keeps the start and the end,
drops the middle and answers as if nothing happened, so the model can return an empty or wrong
answer with no error. Check every prompt before sending it, and after a run compare
result.usage().input_tokens with this estimate: a large gap means the server cut the prompt.
"""

from __future__ import annotations

CHARS_PER_TOKEN = 3.5  # a rough average for English text and markdown
SHARE = 0.75  # leave the rest of the context for the model's answer


class PromptTooLong(RuntimeError):
    pass


def estimate(text: str) -> int:
    return int(len(text) / CHARS_PER_TOKEN)


def check(text: str, context: int) -> None:
    tokens, budget = estimate(text), int(context * SHARE)
    if tokens > budget:
        raise PromptTooLong(f"about {tokens} tokens, more than the {budget} that fit a {context}-token context")
