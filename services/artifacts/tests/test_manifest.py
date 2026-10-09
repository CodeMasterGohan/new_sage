"""Unit tests for artifact manifest validation/verification (spec §14)."""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
import yaml

from services.artifacts.src.artifact_manifest import (
    ArtifactManifest,
    ManifestError,
    ManifestFile,
    SeedSource,
    UnresolvedPin,
    check_revision_pinned,
    check_source_policy,
    check_url_policy,
    find_unresolved_pins,
    load_manifest,
    validate_manifest,
    verify_checksums,
    verify_seed_corpus,
)

GOOD_SHA = "a" * 64
ZEROED = "0" * 64


def _manifest_doc(**overrides):
    doc = {
        "artifact_name": "org/test-model",
        "upstream_source": "https://huggingface.co/org/test-model",
        "upstream_revision": "abc123def456abc123def456abc123def456abc12345",
        "internal_location": "/models/org/test-model",
        "license": "Apache-2.0",
        "files": [{"path": "config.json", "sha256": GOOD_SHA}],
        "approval_record": "APPROVAL-1",
    }
    doc.update(overrides)
    return {"artifact_manifest": doc}


def _write_manifest(tmp_path: Path, **overrides) -> Path:
    path = tmp_path / "manifest.yaml"
    path.write_text(yaml.safe_dump(_manifest_doc(**overrides)), encoding="utf-8")
    return path


def _good_manifest() -> ArtifactManifest:
    return ArtifactManifest(
        artifact_name="org/test-model",
        upstream_source="https://huggingface.co/org/test-model",
        upstream_revision="abc123",
        internal_location="/models/org/test-model",
        license="Apache-2.0",
        files=(ManifestFile(path="config.json", sha256=GOOD_SHA),),
        approval_record="APPROVAL-1",
    )


# ── structural validation ─────────────────────────────────────────────


def test_valid_manifest_loads(tmp_path: Path):
    manifest = load_manifest(_write_manifest(tmp_path))
    assert manifest.artifact_name == "org/test-model"
    assert len(manifest.files) == 1


def test_missing_top_level_key_rejected(tmp_path: Path):
    path = tmp_path / "m.yaml"
    path.write_text(yaml.safe_dump({"nope": 1}), encoding="utf-8")
    with pytest.raises(ManifestError):
        load_manifest(path)


def test_missing_required_field_rejected(tmp_path: Path):
    with pytest.raises(ManifestError):
        load_manifest(_write_manifest(tmp_path, license=""))


def test_empty_files_list_rejected_for_model_manifest(tmp_path: Path):
    with pytest.raises(ManifestError):
        load_manifest(_write_manifest(tmp_path, files=[]))


def test_empty_files_allowed_for_seed_manifest(tmp_path: Path):
    manifest = load_manifest(
        _write_manifest(
            tmp_path,
            files=[],
            seed_sources=[
                {
                    "name": "tldr-pages",
                    "kind": "git-repo",
                    "upstream_url": "https://github.com/tldr-pages/tldr",
                    "revision": "6bbd7cc43d1c5bd12e35a1f2245eea9144fde1ba",
                    "license": "CC-BY-SA 4.0",
                    "resolution_command": "git ls-remote https://github.com/tldr-pages/tldr.git HEAD",
                    "stage_dir": "tldr",
                }
            ],
        )
    )
    assert manifest.seed_sources[0].name == "tldr-pages"


def test_bad_sha256_format_rejected(tmp_path: Path):
    with pytest.raises(ManifestError):
        load_manifest(
            _write_manifest(
                tmp_path, files=[{"path": "a.bin", "sha256": "not-a-hash"}]
            )
        )


def test_absolute_file_path_rejected(tmp_path: Path):
    with pytest.raises(ManifestError):
        load_manifest(
            _write_manifest(
                tmp_path, files=[{"path": "/etc/passwd", "sha256": GOOD_SHA}]
            )
        )


def test_path_traversal_rejected(tmp_path: Path):
    with pytest.raises(ManifestError):
        load_manifest(
            _write_manifest(
                tmp_path, files=[{"path": "../escape.bin", "sha256": GOOD_SHA}]
            )
        )


def test_deferral_without_note_rejected(tmp_path: Path):
    with pytest.raises(ManifestError, match="staging_note"):
        load_manifest(
            _write_manifest(
                tmp_path,
                files=[{"path": "a.bin", "sha256": ZEROED, "defer_to_staging": True}],
            )
        )


def test_deferral_with_note_loads(tmp_path: Path):
    manifest = load_manifest(
        _write_manifest(
            tmp_path,
            files=[
                {
                    "path": "a.bin",
                    "sha256": ZEROED,
                    "defer_to_staging": True,
                    "staging_note": "recorded at staging",
                }
            ],
        )
    )
    entry = manifest.files[0]
    assert entry.defer_to_staging is True
    assert entry.staging_note == "recorded at staging"


# ── source policy ─────────────────────────────────────────────────────


def test_huggingface_source_allowed():
    check_source_policy(_good_manifest())  # must not raise


def test_ghcr_source_allowed_for_devdocs():
    check_url_policy("https://ghcr.io/freecodecamp/devdocs")  # must not raise


def test_modelscope_source_prohibited():
    manifest = _good_manifest()
    object.__setattr__(
        manifest, "upstream_source", "https://modelscope.cn/org/test-model"
    )
    with pytest.raises(ManifestError, match="prohibited"):
        check_source_policy(manifest)


def test_modelscope_subdomain_prohibited():
    manifest = _good_manifest()
    object.__setattr__(
        manifest, "upstream_source", "https://www.modelscope.cn/org/test-model"
    )
    with pytest.raises(ManifestError, match="prohibited"):
        check_source_policy(manifest)


def test_mirror_host_prohibited():
    manifest = _good_manifest()
    object.__setattr__(
        manifest, "upstream_source", "https://hf-mirror.com/org/test-model"
    )
    with pytest.raises(ManifestError, match="prohibited"):
        check_source_policy(manifest)


def test_unapproved_host_rejected():
    manifest = _good_manifest()
    object.__setattr__(
        manifest, "upstream_source", "https://files.example-cdn.net/model.bin"
    )
    with pytest.raises(ManifestError, match="allowlist"):
        check_source_policy(manifest)


def test_non_https_rejected():
    manifest = _good_manifest()
    object.__setattr__(
        manifest, "upstream_source", "http://huggingface.co/org/test-model"
    )
    with pytest.raises(ManifestError, match="https"):
        check_source_policy(manifest)


# ── revision pinning ──────────────────────────────────────────────────


def test_mutable_revision_warns():
    manifest = _good_manifest()
    object.__setattr__(manifest, "upstream_revision", "main")
    warnings = check_revision_pinned(manifest)
    assert any("mutable" in w for w in warnings)


def test_template_placeholder_warns():
    manifest = _good_manifest()
    object.__setattr__(manifest, "upstream_revision", "<immutable-commit-hash>")
    warnings = check_revision_pinned(manifest)
    assert warnings, "unfilled template placeholder must warn"


def test_pinned_revision_no_warnings():
    assert check_revision_pinned(_good_manifest()) == []


# ── unresolved pins ───────────────────────────────────────────────────


def test_zeroed_sha256_without_deferral_is_unresolved():
    manifest = _good_manifest()
    object.__setattr__(
        manifest, "files", (ManifestFile(path="config.json", sha256=ZEROED),)
    )
    unresolved = find_unresolved_pins(manifest)
    assert len(unresolved) == 1
    assert unresolved[0].kind == "file"
    assert unresolved[0].ref == "config.json"


def test_zeroed_sha256_with_explicit_deferral_is_not_unresolved():
    manifest = _good_manifest()
    object.__setattr__(
        manifest,
        "files",
        (
            ManifestFile(
                path="config.json",
                sha256=ZEROED,
                defer_to_staging=True,
                staging_note="recorded at staging",
            ),
        ),
    )
    assert find_unresolved_pins(manifest) == []


def test_real_sha256_is_not_unresolved():
    assert find_unresolved_pins(_good_manifest()) == []


def test_todo_seed_revision_without_deferral_is_unresolved():
    src = SeedSource(
        name="tldr-pages",
        kind="git-repo",
        upstream_url="https://github.com/tldr-pages/tldr",
        revision="TODO-resolve-at-staging",
        license="CC-BY-SA 4.0",
        resolution_command="git ls-remote https://github.com/tldr-pages/tldr.git HEAD",
        stage_dir="tldr",
    )
    manifest = _good_manifest()
    object.__setattr__(manifest, "seed_sources", (src,))
    unresolved = find_unresolved_pins(manifest)
    assert len(unresolved) == 1
    assert unresolved[0].kind == "seed-source"
    assert unresolved[0].ref == "tldr-pages"


def test_todo_seed_revision_with_explicit_deferral_is_not_unresolved():
    src = SeedSource(
        name="tldr-pages",
        kind="git-repo",
        upstream_url="https://github.com/tldr-pages/tldr",
        revision="TODO-resolve-at-staging",
        license="CC-BY-SA 4.0",
        resolution_command="git ls-remote https://github.com/tldr-pages/tldr.git HEAD",
        stage_dir="tldr",
        defer_to_staging=True,
        staging_note="resolve on the staging box with the recorded command",
    )
    manifest = _good_manifest()
    object.__setattr__(manifest, "seed_sources", (src,))
    assert find_unresolved_pins(manifest) == []


def test_todo_seed_digest_without_deferral_is_unresolved():
    src = SeedSource(
        name="DevDocs",
        kind="docker-image",
        upstream_url="https://ghcr.io/freecodecamp/devdocs",
        revision="TODO-resolve-at-staging",
        digest="TODO-resolve-at-staging",
        license="varies per docset",
        resolution_command="docker inspect ...",
        stage_dir="devdocs-image",
    )
    manifest = _good_manifest()
    object.__setattr__(manifest, "seed_sources", (src,))
    unresolved = find_unresolved_pins(manifest)
    kinds = {u.detail for u in unresolved}
    assert len(unresolved) == 2  # revision + digest
    assert all(u.kind == "seed-source" for u in unresolved)


def test_git_repo_requires_full_sha_or_explicit_placeholder(tmp_path: Path):
    with pytest.raises(ManifestError, match="40-char commit SHA"):
        load_manifest(
            _write_manifest(
                tmp_path,
                files=[],
                seed_sources=[
                    {
                        "name": "tldr-pages",
                        "kind": "git-repo",
                        "upstream_url": "https://github.com/tldr-pages/tldr",
                        "revision": "abc123",  # neither full SHA nor TODO
                        "license": "CC-BY-SA 4.0",
                        "resolution_command": "git ls-remote https://github.com/tldr-pages/tldr.git HEAD",
                        "stage_dir": "tldr",
                    }
                ],
            )
        )


def test_docker_image_requires_digest(tmp_path: Path):
    with pytest.raises(ManifestError, match="requires a 'digest'"):
        load_manifest(
            _write_manifest(
                tmp_path,
                files=[],
                seed_sources=[
                    {
                        "name": "DevDocs",
                        "kind": "docker-image",
                        "upstream_url": "https://ghcr.io/freecodecamp/devdocs",
                        "revision": "latest",
                        "license": "varies per docset",
                        "resolution_command": "docker inspect ...",
                        "stage_dir": "devdocs-image",
                    }
                ],
            )
        )


# ── checksum verification ─────────────────────────────────────────────


def test_checksums_verify_ok(tmp_path: Path):
    root = tmp_path / "staged"
    root.mkdir()
    (root / "config.json").write_bytes(b'{"a": 1}')
    digest = hashlib.sha256(b'{"a": 1}').hexdigest()
    manifest = _good_manifest()
    object.__setattr__(
        manifest, "files", (ManifestFile(path="config.json", sha256=digest),)
    )
    assert verify_checksums(manifest, root) == []


def test_missing_file_reported(tmp_path: Path):
    errors = verify_checksums(_good_manifest(), tmp_path / "empty")
    assert any("missing" in e for e in errors)


def test_checksum_mismatch_reported(tmp_path: Path):
    root = tmp_path / "staged"
    root.mkdir()
    (root / "config.json").write_bytes(b"tampered")
    errors = verify_checksums(manifest=_good_manifest(), root=root)
    assert any("mismatch" in e for e in errors)


def test_deferred_zeroed_entry_skipped_by_verify(tmp_path: Path):
    manifest = _good_manifest()
    object.__setattr__(
        manifest,
        "files",
        (
            ManifestFile(
                path="config.json",
                sha256=ZEROED,
                defer_to_staging=True,
                staging_note="recorded at staging",
            ),
        ),
    )
    assert verify_checksums(manifest, tmp_path / "empty") == []


def test_duplicate_file_entry_reported(tmp_path: Path):
    root = tmp_path / "staged"
    root.mkdir()
    (root / "config.json").write_bytes(b"x")
    digest = hashlib.sha256(b"x").hexdigest()
    manifest = _good_manifest()
    object.__setattr__(
        manifest,
        "files",
        (
            ManifestFile(path="config.json", sha256=digest),
            ManifestFile(path="config.json", sha256=digest),
        ),
    )
    errors = verify_checksums(manifest, root)
    assert any("duplicate" in e for e in errors)


# ── seed corpus staging verification ──────────────────────────────────


def _seed_manifest() -> ArtifactManifest:
    return ArtifactManifest(
        artifact_name="new_sage/seed-corpus",
        upstream_source="multiple — see seed_sources",
        upstream_revision="per-source (see seed_sources)",
        internal_location="/artifacts/seed-corpus",
        license="multiple — see seed_sources",
        files=(),
        approval_record="APPROVAL-1",
        seed_sources=(
            SeedSource(
                name="tldr-pages",
                kind="git-repo",
                upstream_url="https://github.com/tldr-pages/tldr",
                revision="6bbd7cc43d1c5bd12e35a1f2245eea9144fde1ba",
                license="CC-BY-SA 4.0",
                resolution_command="git ls-remote https://github.com/tldr-pages/tldr.git HEAD",
                stage_dir="tldr",
            ),
            SeedSource(
                name="DevDocs",
                kind="docker-image",
                upstream_url="https://ghcr.io/freecodecamp/devdocs",
                revision="latest",
                digest="b" * 64,
                license="varies per docset",
                resolution_command="docker inspect ...",
                stage_dir="devdocs-image",
            ),
        ),
    )


def test_verify_seed_corpus_ok(tmp_path: Path):
    tldr = tmp_path / "tldr"
    (tldr / ".git" / "refs" / "heads").mkdir(parents=True)
    (tldr / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (tldr / ".git" / "refs" / "heads" / "main").write_text(
        "6bbd7cc43d1c5bd12e35a1f2245eea9144fde1ba\n"
    )
    devdocs = tmp_path / "devdocs-image"
    devdocs.mkdir()
    (devdocs / "digest.txt").write_text(f"sha256:{'b' * 64}\n")
    assert verify_seed_corpus(_seed_manifest(), tmp_path) == []


def test_verify_seed_corpus_wrong_sha_reported(tmp_path: Path):
    tldr = tmp_path / "tldr"
    (tldr / ".git").mkdir(parents=True)
    (tldr / ".git" / "HEAD").write_text("c" * 40 + "\n")  # detached, wrong SHA
    errors = verify_seed_corpus(_seed_manifest(), tmp_path)
    assert any("checkout is at" in e for e in errors)


def test_verify_seed_corpus_missing_checkout_reported(tmp_path: Path):
    errors = verify_seed_corpus(_seed_manifest(), tmp_path)
    assert any("missing git checkout" in e for e in errors)
    assert any("missing digest marker" in e for e in errors)


# ── end-to-end validate ───────────────────────────────────────────────


def test_validate_manifest_ok_on_good_manifest():
    assert validate_manifest(_good_manifest()) == []


def test_validate_manifest_rejects_prohibited_seed_url():
    manifest = _seed_manifest()
    bad = SeedSource(
        name="evil",
        kind="git-repo",
        upstream_url="https://modelscope.cn/org/data",
        revision="d" * 40,
        license="???",
        resolution_command="n/a",
        stage_dir="evil",
    )
    object.__setattr__(manifest, "seed_sources", manifest.seed_sources + (bad,))
    with pytest.raises(ManifestError, match="prohibited"):
        validate_manifest(manifest)
