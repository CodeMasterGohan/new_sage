"""Production tokenizer is reused within a worker and stays offline."""
import sys
import types

from services.ingestion import doc_chunker


def test_offline_tokenizer_loads_once(monkeypatch):
    calls = []

    class Tokenizer:
        def encode(self, text, add_special_tokens=False):
            return text.split()

    class FakeAutoTokenizer:
        @staticmethod
        def from_pretrained(path, **kwargs):
            calls.append((path, kwargs))
            return Tokenizer()

    fake = types.ModuleType("transformers")
    fake.AutoTokenizer = FakeAutoTokenizer
    monkeypatch.setitem(sys.modules, "transformers", fake)
    monkeypatch.setenv("MINILM_TOKENIZER_PATH", "/test/model")
    doc_chunker._load_tokenizer.cache_clear()
    try:
        assert doc_chunker._default_token_len("one two") == 2
        assert doc_chunker._default_token_len("three four") == 2
        assert len(calls) == 1
        assert calls[0][1]["local_files_only"] is True
    finally:
        doc_chunker._load_tokenizer.cache_clear()
