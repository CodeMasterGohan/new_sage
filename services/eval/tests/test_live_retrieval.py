"""Model-independent contracts for the live endpoint evaluator."""
import sys
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from live_retrieval import evaluate, load_cases, percentile, valid_citation_url


def test_citation_validation():
    assert valid_citation_url("https://github.com/org/repo/blob/abcdef/docs/x.md")
    for bad in ("", "file:///tmp/document", "https://example.invalid/a",
                "http://localhost:8001/doc", "https://user:secret@example.com/docs"):
        assert not valid_citation_url(bad)


def test_quality_metrics_are_separate_and_version_scoped():
    rows = [
        {"id": "ok", "library_id": "react", "version": "19.2.0",
         "topic": "useEffect", "expected_symbol": "useEffect",
         "expected_source_url": "https://react.dev/reference/react/useEffect"},
        {"id": "wrong", "library_id": "react", "version": "18.3.1",
         "topic": "useEffect", "expected_symbol": "useEffect"},
    ]
    def fake(body):
        version = "19.2.0"
        return {
            "version": version, "version_fallback": body["version"] != version,
            "passages": [{"text": "useEffect is a Hook",
                          "url": "https://react.dev/reference/react/useEffect"}],
        }
    report = evaluate(rows, fake, k=5)
    assert report["version_accuracy"] == 0.5
    assert report["citation_url_validity"] == 1.0
    assert report["expected_symbol_hit_at_k"] == 1.0
    assert report["expected_source_hit_at_k"] == 1.0
    assert report["request_failures"] == []


def test_missing_citations_and_request_errors():
    def broken(body):
        if body["library_id"] == "broken":
            raise ConnectionError("offline")
        return {"version": "1", "passages": [{"url": "", "text": "something"}]}
    cases = [
        {"library_id": "ok", "version": "1", "topic": "api"},
        {"library_id": "broken", "version": "1", "topic": "api"},
    ]
    report = evaluate(cases, broken)
    assert report["citation_url_validity"] == 0.0
    assert len(report["request_failures"]) == 1
    assert percentile([1, 2, 3, 4], 0.95) == 4


def test_loading_fails_on_missing_fields(tmp_path):
    file = tmp_path / "cases.jsonl"
    file.write_text('{"library_id":"react"}\n')
    with pytest.raises(ValueError, match="version"):
        load_cases(file)


def test_failed_labeled_query_is_counted_as_quality_miss():
    cases = [
        {"library_id": "react", "version": "19.2.0",
         "topic": "useEffect()", "expected_symbol": "useEffect",
         "expected_source_url": "https://react.dev/reference/react/useEffect"},
    ]
    def offline(_):
        raise ConnectionError("unreachable")
    report = evaluate(cases, offline)
    assert report["expected_symbol_cases"] == 1
    assert report["expected_source_cases"] == 1
    assert report["expected_symbol_hit_at_k"] == 0.0
    assert report["expected_source_hit_at_k"] == 0.0
    assert report["version_accuracy"] == 0.0


def test_symbol_boundary_does_not_match_plural_only():
    cases = [{"library_id": "pkg", "version": "1", "topic": "aws_instance",
              "expected_symbol": "aws_instance"}]
    report = evaluate(cases, lambda _: {
        "version": "1",
        "passages": [{"text": "Only aws_instances_plural is mentioned",
                      "url": "https://example.org/docs"}],
    })
    assert report["expected_symbol_hit_at_k"] == 0.0
