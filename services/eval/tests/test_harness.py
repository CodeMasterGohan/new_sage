"""Harness tests with fake-rigged collaborators (no models, no network).

Covers: BM25 branch report shape, version-routing predicate capture
(including the unresolvable-version -> latest fallback path), chunk
calibration, and the hybrid branch's BLOCKED exit.
"""

import sys
from pathlib import Path

import pytest

EVAL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(EVAL_DIR))
sys.path.insert(0, str(EVAL_DIR.parent / "mcp_server" / "src"))
sys.path.insert(0, str(EVAL_DIR.parent))

from corpus import EvalDoc, EvalItem  # noqa: E402
from harness import EvalHarness, build_harness  # noqa: E402
from mcp_server.models import LibraryRecord  # noqa: E402
from mcp_server.resolver import LibraryResolver  # noqa: E402


def _records():
    return [
        LibraryRecord(
            library_id="acme",
            name="Acme",
            aliases=["ac"],
            ecosystem="pypi",
            docs_url="https://acme.dev",
            repo_url="https://github.com/acme/acme",
            versions=["2.0.0", "1.0.0"],
            latest="2.0.0",
        )
    ]


def _harness():
    items = [
        EvalItem(
            id="acme-01",
            library="acme",
            version="2.0.0",
            type="how",
            question="How do I frobnicate with Acme?",
            gold_passage_text="Call acme.frobnicate() to frobnicate. It returns a frobnicated widget.",
            gold_url="https://acme.dev/frob",
            gold_section=("API", "frobnicate"),
        ),
        EvalItem(
            id="acme-02",
            library="acme",
            version="9.9.9",  # not a real version -> fallback path
            type="signature",
            question="What is the signature of acme.frobnicate?",
            gold_passage_text="The signature is acme.frobnicate(level=1). The level argument controls intensity.",
            gold_url="https://acme.dev/frob",
            gold_section=("API", "frobnicate"),
        ),
    ]
    docs = [
        EvalDoc(
            library="acme",
            version="2.0.0",
            url="https://docs.new-sage.local/acme/2.0.0/",
            markdown=(
                "# Acme 2.0.0\n\n## Frobnication\n\n"
                "Call acme.frobnicate() to frobnicate. It returns a frobnicated widget.\n\n"
                "```python\nacme.frobnicate(level=2)\n```\n\n"
                "## Signature\n\n"
                "The signature is acme.frobnicate(level=1). The level argument controls intensity.\n"
            ),
        ),
        EvalDoc(
            library="acme",
            version="9.9.9",
            url="https://docs.new-sage.local/acme/9.9.9/",
            markdown=(
                "# Acme 9.9.9\n\n## Frobnication\n\n"
                "Call acme.frobnicate() to frobnicate. It returns a frobnicated widget.\n\n"
                "## Signature\n\n"
                "The signature is acme.frobnicate(level=1). The level argument controls intensity.\n"
            ),
        ),
    ]
    return EvalHarness(
        items=items,
        docs=docs,
        resolver=LibraryResolver(_records()),
        seed_records=_records(),
    )


def test_bm25_report_shape():
    h = _harness()
    report = h.run_bm25(ks=(1, 3))
    assert report["branch"] == "bm25"
    assert report["n_items"] == 2
    assert set(report["recall"]) == {"@1", "@3"}
    assert set(report["recall_by_type"]) == {"how", "changed", "signature"}
    assert report["version_routing"]["total"] == 2
    assert "blocked" in report and report["blocked"]
    assert len(report["items"]) == 2
    for it in report["items"]:
        assert it["gold_chunk_id"]
        assert isinstance(it["ranked_chunk_ids"], list)


def test_bm25_finds_gold_for_exact_query():
    h = _harness()
    report = h.run_bm25(ks=(1, 3, 5))
    by_id = {it["id"]: it for it in report["items"]}
    # the how-question names the exact API; BM25 should surface it at rank 1
    assert by_id["acme-01"]["recall@1"] == 1.0


def test_version_routing_predicate_and_fallback():
    h = _harness()
    report = h.run_bm25(ks=(1,))
    by_id = {it["id"]: it for it in report["items"]}
    good = by_id["acme-01"]
    assert good["predicate"] == "acme==2.0.0"
    assert good["version_routing_ok"] is True
    assert good["version_fallback"] is False
    # 9.9.9 is not a known version -> FakeCatalog falls back to latest 2.0.0:
    # the predicate must NOT claim 9.9.9, and routing counts as incorrect.
    bad = by_id["acme-02"]
    assert bad["predicate"] == "acme==2.0.0"
    assert bad["version_fallback"] is True
    assert bad["version_routing_ok"] is False
    assert report["version_routing"]["correct"] == 1


def test_chunk_calibration_on_synthetic_docs():
    h = _harness()
    cal = h.chunk_calibration()
    assert cal["n_chunks"] >= 2
    assert cal["budget_compliant"] is True
    assert cal["code_blocks_intact"] is True
    assert cal["oversized_code_chunks"] == 0


def test_hybrid_branch_blocked_without_weights(monkeypatch, capsys):
    h = _harness()
    monkeypatch.delenv("MINILM_MODEL_PATH", raising=False)
    monkeypatch.delenv("MODEL_PATH", raising=False)
    monkeypatch.delenv("EMBEDDING_MODEL_PATH", raising=False)
    with pytest.raises(SystemExit) as exc:
        h.run_hybrid()
    assert exc.value.code == 2
    assert "BLOCKED" in capsys.readouterr().err


def test_real_corpus_builds_and_calibrates():
    # exercises the real chunker over all 20 hand-written docs
    h = build_harness()
    assert len(h.items) == 50
    assert len(h.docs) == 20
    cal = h.chunk_calibration()
    assert cal["n_chunks"] > 0
    assert cal["budget_compliant"] is True, cal["budget_violations"]
    assert cal["code_blocks_intact"] is True
