"""Tests for the new_sage embedding service (spec §8).

Uses the mock backend (deterministic, dependency-free). No model downloads.
Contract tests cover the exact HTTP surface that the retrieval + ingestion
agents code against:

  POST /embed            {"texts": [...], "task": ...} -> {"vectors", "dimension"}
  POST /embed_documents  {"items": [...] , "task": ...} -> {"vectors", "dimension"}
  GET  /health           -> {"model", "dimension", "backend", "device", "model_loaded"}
"""

from __future__ import annotations

import hashlib
import math
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("EMBEDDING_BACKEND", "mock")

from services.embedding import reuse  # noqa: E402
from services.embedding.service import EmbeddingService  # noqa: E402
from services.embedding.settings import EmbeddingServiceSettings  # noqa: E402

import services.embedding.main as main_module  # noqa: E402

# sha256 of vendor/ingestion/embeddings.py (re-pinned 2026-10-09 after the
# intentional MinIO→RustFS rename; still byte-identical to new_DRUID source).
# The vendored file is a byte-identical copy of the verified new_DRUID
# source; this pins that guarantee against accidental edits.
VENDORED_EMBEDDINGS_SHA256 = (
    "df7812d9db152292d9d6d2b540bf4eade93c85757426f110678a94a1a34ef8b0"
)


@pytest.fixture()
def ing():
    return reuse.load_ingestion_embeddings()


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("EMBEDDING_BACKEND", "mock")
    for var in ("MODEL_PATH", "EMBEDDING_MODEL_PATH", "EMBEDDING_MODEL_DIR"):
        monkeypatch.delenv(var, raising=False)
    with TestClient(main_module.app) as c:
        yield c


def _norm(vec):
    return math.sqrt(sum(v * v for v in vec))


def _mock_vector(seed_text: str) -> list[float]:
    """Reproduce the mock backend's deterministic vector for a seed string."""
    ing = reuse.load_ingestion_embeddings()
    return ing.MockEmbeddingBackend(dimension=768).embed_texts([seed_text])[0]


# -- §8 prefix contract: vendored, never redefined --------------------------------

def test_vendored_contract_is_byte_identical():
    path = Path(reuse._vendor_root()) / "ingestion" / "embeddings.py"
    content = path.read_bytes().replace(b"\r\n", b"\n")
    digest = hashlib.sha256(content).hexdigest()
    assert digest == VENDORED_EMBEDDINGS_SHA256


def test_query_prefix_strings_match_contract(ing):
    # The contractual strings, verified verbatim against the released
    # google/embeddinggemma-2 checkpoint. The service reuses them from the
    # vendored module — it must never redefine them.
    assert ing.PrefixContract.QUERY_QA == "task: question answering | query: {query}"
    assert ing.PrefixContract.QUERY_CODE == "task: code retrieval | query: {query}"
    assert ing.PrefixContract.QUERY_SEARCH == "task: search result | query: {query}"
    assert ing.PrefixContract.DOC_PROSE_TITLE == "title: {heading} | text: {text}"
    assert ing.PrefixContract.DOC_CODE_TITLE == "title: {heading} | text: {text}"


def test_service_formats_through_vendored_module(ing):
    svc = EmbeddingService(EmbeddingServiceSettings(backend="mock"))
    assert svc.format_query(text="what is RAG?") == ing.build_query_text("what is RAG?", task="qa")
    assert svc.format_query(text="parse pdf", task="code_retrieval") == ing.build_query_text("parse pdf", task="code")
    assert svc.format_query(text="invoice 42", task="search_result") == ing.build_query_text("invoice 42", task="search")
    assert svc.format_document(title="Intro", text="hello") == "title: Intro | text: hello"


# -- POST /embed contract ---------------------------------------------------------

def test_embed_contract(client):
    resp = client.post(
        "/embed",
        json={"texts": ["how do I do X with library L?", "second query"], "task": "question_answering"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == {"vectors", "dimension"}
    assert body["dimension"] == 768
    assert len(body["vectors"]) == 2
    for vec in body["vectors"]:
        assert len(vec) == 768
        assert abs(_norm(vec) - 1.0) < 1e-6


def test_embed_applies_query_prefix_server_side(client):
    # The mock backend is content-seeded: the returned vector must equal the
    # vector for the literal prefixed string, proving the server applied the
    # §8 query prefix (the client never sends prefixes).
    resp = client.post("/embed", json={"texts": ["what is RAG?"], "task": "question_answering"})
    assert resp.status_code == 200
    expected = _mock_vector("task: question answering | query: what is RAG?")
    assert resp.json()["vectors"][0] == pytest.approx(expected)


def test_embed_task_selects_prefix(client):
    for task, prefix in (
        ("question_answering", "task: question answering | query:"),
        ("code_retrieval", "task: code retrieval | query:"),
        ("search_result", "task: search result | query:"),
    ):
        resp = client.post("/embed", json={"texts": ["parse pdf"], "task": task})
        assert resp.status_code == 200
        assert resp.json()["vectors"][0] == pytest.approx(_mock_vector(f"{prefix} parse pdf"))


def test_embed_prefix_actually_flows_into_vector(client):
    # Same text, different tasks -> different vectors (mock is content-seeded).
    qa = client.post("/embed", json={"texts": ["parse pdf"], "task": "question_answering"})
    code = client.post("/embed", json={"texts": ["parse pdf"], "task": "code_retrieval"})
    assert qa.status_code == 200 and code.status_code == 200
    assert qa.json()["vectors"][0] != code.json()["vectors"][0]


def test_embed_rejects_empty_texts(client):
    resp = client.post("/embed", json={"texts": []})
    assert resp.status_code == 422


def test_embed_rejects_unknown_task(client):
    resp = client.post("/embed", json={"texts": ["x"], "task": "bogus_task"})
    assert resp.status_code == 422


def test_embed_default_task_is_question_answering(client):
    resp = client.post("/embed", json={"texts": ["hello"]})
    assert resp.status_code == 200
    assert resp.json()["vectors"][0] == pytest.approx(_mock_vector("task: question answering | query: hello"))


# -- POST /embed_documents contract -------------------------------------------------

def test_embed_documents_contract(client):
    resp = client.post(
        "/embed_documents",
        json={
            "items": [
                {"title": "API Reference > connect()", "text": "Connects the client.", "is_code": False},
                {"title": "example.py", "text": "def f():\n    pass", "is_code": True},
            ],
            "task": "question_answering",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == {"vectors", "dimension"}
    assert body["dimension"] == 768
    assert len(body["vectors"]) == 2
    for vec in body["vectors"]:
        assert len(vec) == 768
        assert abs(_norm(vec) - 1.0) < 1e-6


def test_embed_documents_applies_document_prefix_server_side(client):
    resp = client.post(
        "/embed_documents",
        json={"items": [{"title": "Intro", "text": "some prose", "is_code": False}]},
    )
    assert resp.status_code == 200
    expected = _mock_vector("title: Intro | text: some prose")
    assert resp.json()["vectors"][0] == pytest.approx(expected)


def test_embed_documents_is_code_selects_code_prefix(client):
    plain = client.post(
        "/embed_documents",
        json={"items": [{"title": "example.py", "text": "def f(): pass", "is_code": False}]},
    )
    code = client.post(
        "/embed_documents",
        json={"items": [{"title": "example.py", "text": "def f(): pass", "is_code": True}]},
    )
    assert plain.status_code == 200 and code.status_code == 200
    # Both prefix templates are "title: {heading} | text: {text}" in the
    # current contract, but the code path is selected by is_code — the
    # service must honor the flag (templates may diverge in future).
    assert plain.json()["vectors"][0] == pytest.approx(
        _mock_vector("title: example.py | text: def f(): pass")
    )
    assert code.json()["vectors"][0] == pytest.approx(
        _mock_vector("title: example.py | text: def f(): pass")
    )


def test_embed_documents_rejects_empty_items(client):
    resp = client.post("/embed_documents", json={"items": []})
    assert resp.status_code == 422


def test_embed_documents_rejects_unknown_task(client):
    resp = client.post(
        "/embed_documents",
        json={"items": [{"title": "t", "text": "x"}], "task": "bogus_task"},
    )
    assert resp.status_code == 422


def test_embed_documents_title_defaults_to_empty(client):
    resp = client.post("/embed_documents", json={"items": [{"text": "no title"}]})
    assert resp.status_code == 200
    assert resp.json()["vectors"][0] == pytest.approx(_mock_vector("title:  | text: no title"))


# -- GET /health contract ------------------------------------------------------------

def test_health_contract(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == {"model", "dimension", "backend", "device", "model_loaded"}
    assert body["model"] == "google/embeddinggemma-2"
    assert body["dimension"] == 768
    assert body["backend"] == "mock"
    assert body["device"] == "cpu"
    assert body["model_loaded"] is True


# -- fail-closed behavior ---------------------------------------------------------------

def test_startup_fails_without_artifact(monkeypatch):
    monkeypatch.setenv("EMBEDDING_BACKEND", "st")
    monkeypatch.setenv("MODEL_PATH", "/nonexistent/models/embeddinggemma-2")
    with pytest.raises(RuntimeError):
        with TestClient(main_module.app):
            pass  # pragma: no cover


def test_startup_fails_without_path_env(monkeypatch):
    monkeypatch.setenv("EMBEDDING_BACKEND", "st")
    for var in ("MODEL_PATH", "EMBEDDING_MODEL_PATH", "EMBEDDING_MODEL_DIR"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(RuntimeError):
        with TestClient(main_module.app):
            pass  # pragma: no cover


def test_model_path_primary_env_name(monkeypatch):
    # MODEL_PATH is the primary env name for the pre-staged artifact.
    monkeypatch.setenv("EMBEDDING_BACKEND", "mock")
    monkeypatch.setenv("MODEL_PATH", "/models/embeddinggemma-2")
    settings = EmbeddingServiceSettings()
    assert settings.model_path == "/models/embeddinggemma-2"


def test_width_guard_rejects_unvalidated_width():
    with pytest.raises(ValueError, match="Matryoshka"):
        EmbeddingServiceSettings(backend="mock", dimension=100)


def test_validated_matryoshka_width_accepted():
    svc = EmbeddingService(EmbeddingServiceSettings(backend="mock", dimension=256))
    assert svc.settings.dimension == 256
    assert len(svc._backend.embed_texts(["x"])[0]) == 256


def test_input_char_guard(client):
    long_text = "x" * 40000
    resp = client.post("/embed", json={"texts": [long_text]})
    assert resp.status_code == 400
    assert "pre-chunked" in resp.json()["detail"]
