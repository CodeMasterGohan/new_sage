"""Reuse the contractual embedding strings (spec §8).

Single source of truth: the vendored ``vendor/ingestion/embeddings.py``
owns the contractual task-aware prefix pairs, the page-image record shape,
and the backend abstractions (``SentenceTransformersGemmaBackend``,
``MockEmbeddingBackend``). These files are BYTE-IDENTICAL copies of the
verified new_DRUID sources (``services/ingestion/embeddings.py``,
``chunker.py``, ``config.py``, ``tree.py``, ``identifiers.py``), whose
``PrefixContract`` strings were verified verbatim against the released
``google/embeddinggemma-2`` checkpoint.

The strings are never redefined or retyped here — they are loaded from the
vendored files via :func:`load_ingestion_embeddings`. Any future
regeneration of the vendor directory must preserve byte-identity with the
verified source.

FUTURE: when new_sage's own ``services/ingestion/embeddings.py`` exists
with the same contract, point this loader at that tree instead of the
vendor directory — the loader mechanics stay the same.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

_MODULE_NAME = "sage_contract.ingestion.embeddings"
_PACKAGE_NAME = "sage_contract.ingestion"
_PARENT_NAME = "sage_contract"

# The five vendored sibling files (all stdlib-only).
_SIBLING_FILES = (
    "embeddings.py",
    "chunker.py",
    "config.py",
    "tree.py",
    "identifiers.py",
)


def _vendor_root() -> Path:
    # services/embedding/reuse.py -> parent is services/embedding/.
    return Path(__file__).resolve().parent / "vendor"


def _ensure_stub_package(name: str, path: Path) -> None:
    """Register a package module with ``__path__`` but no executed __init__."""
    if name in sys.modules:
        return
    module = types.ModuleType(name)
    module.__path__ = [str(path)]  # type: ignore[attr-defined]
    sys.modules[name] = module


def load_ingestion_embeddings():
    """Return the vendored ``ingestion.embeddings`` contract module.

    The module is loaded once and cached in ``sys.modules``. Raises
    RuntimeError if the vendored files are absent (fail closed — the prefix
    contract is mandatory, never optional).
    """
    if _MODULE_NAME in sys.modules:
        return sys.modules[_MODULE_NAME]

    vendor_root = _vendor_root()
    ingestion_dir = vendor_root / "ingestion"
    missing = [f for f in _SIBLING_FILES if not (ingestion_dir / f).is_file()]
    if missing:
        raise RuntimeError(
            "embedding service requires the vendored prefix-contract "
            f"files, missing: {missing} (expected under {ingestion_dir}). "
            "The §8 prefix contract is mandatory — refusing to start with a "
            "duplicated or absent contract."
        )

    _ensure_stub_package(_PARENT_NAME, vendor_root)
    _ensure_stub_package(_PACKAGE_NAME, ingestion_dir)

    spec = importlib.util.spec_from_file_location(
        _MODULE_NAME, ingestion_dir / "embeddings.py"
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not create import spec for {_MODULE_NAME}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[_MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module
