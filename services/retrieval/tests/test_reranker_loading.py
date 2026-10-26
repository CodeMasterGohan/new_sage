"""Keep the loader compatible with the pinned CrossEncoder API and offline paths."""
import sys
from types import SimpleNamespace

import pytest

from services.retrieval.reranker import MiniLMReranker


@pytest.mark.parametrize('separate_tokenizer', [False, True])
def test_pinned_cross_encoder_api_and_local_tokenizer(monkeypatch, tmp_path, separate_tokenizer):
    model = tmp_path / 'model'
    model.mkdir()
    tokenizer = tmp_path / 'tokenizer' if separate_tokenizer else model
    tokenizer.mkdir(exist_ok=True)
    loaded = []
    replacement = object()

    class CrossEncoder:
        # sentence-transformers 6.1 removed the tokenizer_name argument.
        def __init__(self, model_name_or_path, *, device, local_files_only):
            assert (model_name_or_path, device, local_files_only) == (str(model), 'cpu', True)
            self.tokenizer = None

    def load_tokenizer(path, *, local_files_only):
        loaded.append((path, local_files_only))
        return replacement

    monkeypatch.setitem(sys.modules, 'sentence_transformers', SimpleNamespace(CrossEncoder=CrossEncoder))
    monkeypatch.setitem(sys.modules, 'transformers', SimpleNamespace(
        AutoTokenizer=SimpleNamespace(from_pretrained=load_tokenizer)))
    reranker = MiniLMReranker(str(model), str(tokenizer))
    assert loaded == ([(str(tokenizer), True)] if separate_tokenizer else [])
    assert reranker._model.tokenizer is (replacement if separate_tokenizer else None)
