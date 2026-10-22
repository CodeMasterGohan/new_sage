"""Phase 2: exact API symbols, bounded ranking and diagnostic opt-in."""
from services.retrieval.code_search import extract_code_symbols, code_match_strength
from services.retrieval.models import FusedCandidate, StoredChunk
from services.retrieval.reranker import rerank_candidates
from services.retrieval.tests.test_retrieval import FakeStore, make_chunk, make_service


def test_extract_api_symbols_without_generic_prose():
    symbols = extract_code_symbols(
        "How does `aws_instance` use useEffect() with session.get()? "
        "Also parse_csv and `Client.fetch`."
    )
    assert {"aws_instance", "useEffect", "session.get", "parse_csv", "Client.fetch"} <= set(symbols)
    assert extract_code_symbols("What does this library support?") == []


def test_symbol_boundaries_and_headings():
    definition = make_chunk("definition", headings=("Resources", "aws_instance"))
    unrelated = make_chunk("unrelated", text="aws_instances plural")
    assert code_match_strength(["aws_instance"], definition) == 1.0
    assert code_match_strength(["aws_instance"], unrelated) == 0.0


class EqualReranker:
    def score(self, query, passages):
        return [1.0] * len(passages)


def test_code_definition_can_outrank_incidental_prose():
    unrelated = FusedCandidate(
        record=make_chunk("prose", text="Use aws_instance in some paragraph."),
        fusion_score=0.5,
    )
    reference = FusedCandidate(
        record=make_chunk("reference", headings=("aws_instance",), text="resource reference"),
        fusion_score=0.4,
    )
    ordered = rerank_candidates(
        "How to configure `aws_instance`?", [unrelated, reference],
        EqualReranker(), code_symbol_bonus=0.35,
    )
    assert ordered[0][0].record.chunk_id == "reference"
    assert ordered[0][1] > ordered[1][1]


def test_diagnostics_is_opt_in_and_preserves_default_response_shape():
    store = FakeStore(dense=[make_chunk("ref", headings=("API", "useEffect"))])
    svc = make_service(store=store)
    standard = svc.retrieve("react", version="19.0.0", topic="useEffect()")
    assert "diagnostics" not in standard.to_dict()

    explained = svc.retrieve(
        "react", version="19.0.0", topic="useEffect()", diagnostics=True
    )
    payload = explained.to_dict()
    diag = payload["diagnostics"]
    assert diag["version_id"] == "v19"
    assert diag["candidate_counts"]["dense"] == 1
    assert "timings_ms" in diag
    assert diag["top_hits"][0]["chunk_id"] == "ref"
    assert diag["top_hits"][0]["symbol_match"] == 1.0



def test_extract_code_symbols_includes_pascal_case_classes():
    """P0 Defect #4 regression: PascalCase class/type names like GeneratedField
    and QuerySet must be extracted as code symbols just like lowerCamelCase."""
    symbols = extract_code_symbols(
        "How do I define a column with GeneratedField and filter a QuerySet in Django?"
    )
    assert "GeneratedField" in symbols
    assert "QuerySet" in symbols
