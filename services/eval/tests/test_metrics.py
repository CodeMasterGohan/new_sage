"""Unit tests for the pure eval metrics (no services, no models)."""

from types import SimpleNamespace

import pytest

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from metrics import recall_at_k, resolver_accuracy, version_correctness  # noqa: E402


class TestRecallAtK:
    def test_hit_at_1(self):
        assert recall_at_k(["a", "b", "c"], "a", 1) == 1.0

    def test_hit_within_k(self):
        assert recall_at_k(["a", "b", "c"], "c", 3) == 1.0

    def test_miss_beyond_k(self):
        assert recall_at_k(["a", "b", "c"], "c", 2) == 0.0

    def test_miss_absent(self):
        assert recall_at_k(["a", "b"], "z", 5) == 0.0

    def test_k_larger_than_list(self):
        assert recall_at_k(["a"], "a", 10) == 1.0

    def test_empty_ranking(self):
        assert recall_at_k([], "a", 3) == 0.0

    def test_k_zero_raises(self):
        with pytest.raises(ValueError):
            recall_at_k(["a"], "a", 0)


class TestVersionCorrectness:
    def test_exact_match(self):
        assert version_correctness("19.2.0", "19.2.0") is True

    def test_mismatch(self):
        assert version_correctness("18.3.1", "19.2.0") is False

    def test_none_retrieved(self):
        assert version_correctness(None, "19.2.0") is False

    def test_fallback_to_latest_is_a_miss(self):
        # asked 18.3.1, served latest 19.2.0 -> incorrect even though "a" version
        assert version_correctness("19.2.0", "18.3.1") is False


class _StubResolver:
    def __init__(self, mapping):
        self._mapping = mapping  # query -> library_id or None

    def resolve(self, query, ecosystem=None):
        lid = self._mapping.get(query)
        return SimpleNamespace(found=lid is not None, library_id=lid)


class TestResolverAccuracy:
    def test_all_correct(self):
        resolver = _StubResolver({"react": "react", "py": "python"})
        out = resolver_accuracy(
            [
                {"query": "react", "expected_library_id": "react"},
                {"query": "py", "expected_library_id": "python"},
            ],
            resolver,
        )
        assert out == {"total": 2, "correct": 2, "accuracy": 1.0, "failures": []}

    def test_wrong_and_unknown(self):
        resolver = _StubResolver({"react": "vue"})
        out = resolver_accuracy(
            [
                {"query": "react", "expected_library_id": "react"},
                {"query": "nosuchlib", "expected_library_id": None},
            ],
            resolver,
        )
        assert out["total"] == 2
        assert out["correct"] == 1
        assert out["accuracy"] == 0.5
        assert len(out["failures"]) == 1
        assert out["failures"][0]["query"] == "react"
