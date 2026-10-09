"""Tests for the P2 seed-corpus manifest and repo-wide pin hygiene (spec §14).

The hygiene sweep (``test_no_unflagged_placeholders``) is the enforcement
behind the policy: every manifest in ``manifests/`` must either have all pins
resolved or carry an EXPLICIT deferral (``defer_to_staging: true`` plus a
``staging_note``, or a registry entry below with a recorded reason).
A zeroed/placeholder sha256 with neither fails the suite.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from services.artifacts.src.artifact_manifest import (
    find_unresolved_pins,
    load_manifest,
    validate_manifest,
)

MANIFEST_DIR = Path(__file__).resolve().parent.parent / "manifests"

# Explicit, auditable exceptions to the no-placeholder rule. These manifests
# are COPIED VERBATIM from new_DRUID's completed research drafts (byte-
# identical), so per-entry flags cannot be added without altering them.
# Each entry records WHY the zeroed sha256 is acceptable: they are small
# git-stored config files (not LFS weights) whose checksums the manifests'
# own headers say MUST be recorded at controlled staging before approval.
# Key: manifest filename -> {file path: reason}.
DOCUMENTED_STAGING_DEFERRALS: dict[str, dict[str, str]] = {
    "embeddinggemma-2.yaml": {
        path: "small git-stored config; sha256 recorded at controlled staging "
        "(see manifest header + README staging procedure)"
        for path in (
            "config.json",
            "tokenizer_config.json",
            "preprocessor_config.json",
            "processor_config.json",
            "config_sentence_transformers.json",
            "sentence_bert_config.json",
            "modules.json",
            "1_Pooling/config.json",
            "2_Normalize/config.json",
            "chat_template.jinja",
        )
    },
    "minilm-l6-v2.yaml": {
        path: "small git-stored config; sha256 recorded at controlled staging "
        "(see manifest header + README staging procedure)"
        for path in (
            "config.json",
            "tokenizer.json",
            "tokenizer_config.json",
            "vocab.txt",
            "special_tokens_map.json",
        )
    },
}


def _load(name: str):
    path = MANIFEST_DIR / name
    assert path.is_file(), f"missing manifest: {name}"
    return path, load_manifest(path)


# ── repo hygiene sweep ────────────────────────────────────────────────


def test_all_manifests_validate():
    for name in ("embeddinggemma-2.yaml", "minilm-l6-v2.yaml", "seed-corpus.yaml"):
        path, manifest = _load(name)
        warnings = validate_manifest(manifest)
        assert warnings == [], f"{name}: unexpected warnings: {warnings}"


def test_no_unflagged_placeholders():
    """Fail if any manifest has a zeroed/placeholder pin without an explicit,
    documented deferral. Seed-corpus digests are the documented exception —
    but only when flagged via defer_to_staging + staging_note."""
    failures: list[str] = []
    for manifest_file in sorted(MANIFEST_DIR.glob("*.yaml")):
        manifest = load_manifest(manifest_file)
        deferrals = DOCUMENTED_STAGING_DEFERRALS.get(manifest_file.name, {})
        for pin in find_unresolved_pins(manifest):
            if pin.kind == "file" and pin.ref in deferrals:
                continue  # explicitly documented exception
            failures.append(f"{manifest_file.name} [{pin.kind}] {pin.ref}: {pin.detail}")
    assert failures == [], (
        "unflagged placeholder pins found:\n" + "\n".join(failures)
    )


def test_deferral_registry_covers_real_zeroed_entries():
    """Guard the registry itself: every registered path must actually exist
    in its manifest as a zeroed entry (catches registry drift)."""
    for filename, paths in DOCUMENTED_STAGING_DEFERRALS.items():
        manifest = load_manifest(MANIFEST_DIR / filename)
        zeroed = {
            f.path for f in manifest.files if f.sha256 == "0" * 64
        }
        for path in paths:
            assert path in zeroed, f"{filename}: registry path {path!r} is not a zeroed entry"


# ── completed model manifests: verified unchanged ─────────────────────


def test_embeddinggemma2_pins():
    _, manifest = _load("embeddinggemma-2.yaml")
    assert manifest.artifact_name == "google/embeddinggemma-2"
    assert manifest.upstream_source == "https://huggingface.co/google/embeddinggemma-2"
    assert manifest.upstream_revision == "914f7f89142e33e77833254d9c9b90c3cef7303b"
    assert manifest.license == "Apache-2.0"
    by_path = {f.path: f.sha256 for f in manifest.files}
    assert (
        by_path["model.safetensors"]
        == "197a32965d4b1105faf060417baa899e193fb73cd401f42ec9295234d5553d79"
    )
    assert (
        by_path["tokenizer.json"]
        == "4d777ef5bdc1aa36227abdfb77c3e49e7b9c892d16e1b6bda41c393504828be4"
    )
    assert (
        by_path["tokenizer.model"]
        == "e594c8a90eb08d8bda498ff4747977dc827ae0c3c56b5c0d41a605a22d02ef03"
    )


def test_minilm_pins():
    _, manifest = _load("minilm-l6-v2.yaml")
    assert manifest.artifact_name == "cross-encoder/ms-marco-MiniLM-L6-v2"
    assert (
        manifest.upstream_source
        == "https://huggingface.co/cross-encoder/ms-marco-MiniLM-L6-v2"
    )
    assert manifest.upstream_revision == "233902d25c440f23af6f7d6e94d2946bac0bee0a"
    assert manifest.license == "Apache-2.0"
    by_path = {f.path: f.sha256 for f in manifest.files}
    assert (
        by_path["model.safetensors"]
        == "821d1aa69520101d6e0737f78a042ae25b19e5cb9160701909d10434f4aeb0ae"
    )


# ── seed-corpus manifest ──────────────────────────────────────────────


def _seed_sources():
    _, manifest = _load("seed-corpus.yaml")
    return {s.name: s for s in manifest.seed_sources}


def test_seed_corpus_has_three_sources():
    sources = _seed_sources()
    assert set(sources) == {
        "DevDocs docsets (monthly image)",
        "MDN content",
        "tldr-pages",
    }


def test_seed_corpus_devdocs_digest():
    src = _seed_sources()["DevDocs docsets (monthly image)"]
    assert src.kind == "docker-image"
    assert src.upstream_url == "https://ghcr.io/freecodecamp/devdocs"
    # Resolved 2026-10-08 from the GHCR registry API (metadata only).
    assert (
        src.digest
        == "8623d9b9c6c3ac12bafbd3e378711c6f2d61a0baf23350c9da6e9261b3d66a60"
    )
    assert "8623d9b9c6c3ac12bafbd3e378711c6f2d61a0baf23350c9da6e9261b3d66a60" in src.revision


def test_seed_corpus_mdn_sha():
    src = _seed_sources()["MDN content"]
    assert src.kind == "git-repo"
    assert src.upstream_url == "https://github.com/mdn/content"
    # HEAD as of 2026-10-08 (`git ls-remote ... HEAD`).
    assert src.revision == "fd0b11ad5b5014a9333578adcc4bc98fb9024da3"
    assert src.license == "CC-BY-SA"


def test_seed_corpus_tldr_sha():
    src = _seed_sources()["tldr-pages"]
    assert src.kind == "git-repo"
    assert src.upstream_url == "https://github.com/tldr-pages/tldr"
    # HEAD as of 2026-10-08 (`git ls-remote ... HEAD`).
    assert src.revision == "6bbd7cc43d1c5bd12e35a1f2245eea9144fde1ba"
    assert src.license == "CC-BY-SA 4.0"


def test_seed_corpus_devdocs_per_docset_license_note():
    src = _seed_sources()["DevDocs docsets (monthly image)"]
    assert src.license == "varies per docset"
    assert "per-docset license" in src.license_note
    assert "ingest time" in src.license_note


def test_seed_corpus_resolution_commands_recorded():
    for name, src in _seed_sources().items():
        assert src.resolution_command.strip(), f"{name}: resolution_command missing"
        assert src.stage_dir.strip(), f"{name}: stage_dir missing"


def test_seed_corpus_internal_location():
    _, manifest = _load("seed-corpus.yaml")
    assert manifest.internal_location == "/artifacts/seed-corpus"


def test_seed_corpus_has_no_unresolved_pins():
    _, manifest = _load("seed-corpus.yaml")
    assert find_unresolved_pins(manifest) == []
