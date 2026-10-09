"""Shared test helpers: deterministic word-count token_len."""

from __future__ import annotations

import pytest


def word_len(text: str) -> int:
    """Deterministic stand-in for a tokenizer (1 word ~= 1 token)."""
    return len(text.split())


@pytest.fixture()
def token_len():
    return word_len
