"""Tests for the DevDocs monthly sync entrypoint (spec §5.1.1).

Offline: the registry/docker seams are faked, the catalog is real SQLite.
Covers manifest parsing (valid / missing / bad digest / unapproved),
registry digest resolution, docset staging + version stamps, diff
classifications (changed / unchanged / added / removed), the RQ enqueue
helper, and the no-manifest fail-fast error.
"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from services.catalog.models import Base
from services.catalog.repository import CatalogRepository
from services.crawler import jobs
from services.crawler.devdocs_manifest import (
    DEVDOCS_MANIFEST_ENV,
    DevDocsManifestError,
    default_manifest_path,
    docset_manifest_from_stage,
    docset_version_stamp,
    load_devdocs_image_ref,
    resolve_latest_digest,
    stage_image_docsets,
)
from services.crawler.jobs import (
    DEVDOCS_ECOSYSTEM,
    _require_explicit_extractor,
    devdocs_diff,
    devdocs_sync,
    devdocs_sync_impl,
    enqueue_devdocs_sync,
)
from services.ingestion.models import Page

DIGEST = "8623d9b9c6c3ac12bafbd3e378711c6f2d61a0baf23350c9da6e9261b3d66a60"


@pytest.fixture
def repo():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    return CatalogRepository(session)


def manifest_yaml(**overrides) -> str:
    """Minimal manifest mirroring the approved file's structure."""
    digest = overrides.get("digest", DIGEST)
    approval = overrides.get(
        "approval_record", "APPROVAL-2026-101 — APPROVED by test"
    )
    artifact_name = overrides.get("artifact_name", "new_sage/seed-corpus")
    lines = [
        "artifact_manifest:",
        '  artifact_name: "%s"' % artifact_name,
        "  files: []",
        "  seed_sources:",
    ]
    sources = overrides.get("seed_sources")
    if sources is None:
        lines += [
            '    - name: "DevDocs docsets (monthly image)"',
            '      kind: "docker-image"',
            '      upstream_url: "https://ghcr.io/freecodecamp/devdocs"',
            '      revision: "latest pinned to sha256:%s (test)"' % DIGEST,
            '      digest: "%s"' % digest,
            '      license: "varies per docset"',
            "      resolution_command: |",
            "        docker pull ghcr.io/freecodecamp/devdocs:latest",
            '      stage_dir: "devdocs-image"',
            '    - name: "MDN content"',
            '      kind: "git-repo"',
            '      upstream_url: "https://github.com/mdn/content"',
            '      revision: "fd0b11ad5b5014a9333578adcc4bc98fb9024da3"',
            '      license: "CC-BY-SA"',
            '      resolution_command: "git ls-remote https://github.com/mdn/content.git HEAD"',
            '      stage_dir: "mdn-content"',
        ]
    else:
        lines += sources.splitlines()
    if approval is not None:
        lines.append('  approval_record: "%s"' % approval)
    return "\n".join(lines) + "\n"


def git_only_sources() -> str:
    return "\n".join(
        [
            '    - name: "MDN content"',
            '      kind: "git-repo"',
            '      upstream_url: "https://github.com/mdn/content"',
            '      revision: "fd0b11ad5b5014a9333578adcc4bc98fb9024da3"',
            '      license: "CC-BY-SA"',
            '      resolution_command: "git ls-remote HEAD"',
            '      stage_dir: "mdn-content"',
        ]
    )


def write_manifest(tmp_path: Path, **overrides) -> Path:
    path = tmp_path / "seed-corpus.yaml"
    path.write_text(manifest_yaml(**overrides), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# manifest parsing
# ---------------------------------------------------------------------------


class TestLoadImageRef:
    def test_valid_manifest(self, tmp_path):
        path = write_manifest(tmp_path)
        ref = load_devdocs_image_ref(path)
        assert ref.repo == "ghcr.io/freecodecamp/devdocs"
        assert ref.digest == DIGEST
        assert ref.pinned == "ghcr.io/freecodecamp/devdocs@sha256:" + DIGEST
        assert "APPROVAL-2026-101" in ref.approval_record
        assert ref.manifest_path == path

    def test_real_approved_manifest(self):
        """The actual APPROVAL-2026-101 file parses with the pinned digest."""
        ref = load_devdocs_image_ref()
        assert ref.repo == "ghcr.io/freecodecamp/devdocs"
        assert ref.digest == DIGEST

    def test_missing_file_names_path(self, tmp_path):
        missing = tmp_path / "nope.yaml"
        with pytest.raises(DevDocsManifestError, match="nope.yaml"):
            load_devdocs_image_ref(missing)

    def test_missing_file_mentions_env_override(self, tmp_path):
        with pytest.raises(DevDocsManifestError) as exc:
            load_devdocs_image_ref(tmp_path / "nope.yaml")
        assert DEVDOCS_MANIFEST_ENV in str(exc.value)

    def test_bad_digest_rejected(self, tmp_path):
        path = write_manifest(tmp_path, digest="xyz-not-a-digest")
        with pytest.raises(DevDocsManifestError, match="digest"):
            load_devdocs_image_ref(path)

    def test_sha256_prefixed_digest_accepted(self, tmp_path):
        path = write_manifest(tmp_path, digest="sha256:" + DIGEST)
        assert load_devdocs_image_ref(path).digest == DIGEST

    def test_no_docker_image_source(self, tmp_path):
        path = write_manifest(tmp_path, seed_sources=git_only_sources())
        with pytest.raises(DevDocsManifestError, match="docker-image"):
            load_devdocs_image_ref(path)

    def test_missing_approval_record(self, tmp_path):
        path = write_manifest(tmp_path, approval_record=None)
        with pytest.raises(DevDocsManifestError, match="approval_record"):
            load_devdocs_image_ref(path)

    def test_wrong_artifact_name(self, tmp_path):
        path = write_manifest(tmp_path, artifact_name="something/else")
        with pytest.raises(DevDocsManifestError, match="artifact_name"):
            load_devdocs_image_ref(path)

    def test_garbage_yaml_rejected(self, tmp_path):
        path = tmp_path / "bad.yaml"
        path.write_text("just: [not, the, manifest\n", encoding="utf-8")
        with pytest.raises(DevDocsManifestError):
            load_devdocs_image_ref(path)

    def test_default_path_respects_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv(DEVDOCS_MANIFEST_ENV, str(tmp_path / "custom.yaml"))
        assert default_manifest_path() == tmp_path / "custom.yaml"

    def test_default_path_points_at_build_tree(self, monkeypatch):
        monkeypatch.delenv(DEVDOCS_MANIFEST_ENV, raising=False)
        assert default_manifest_path().name == "seed-corpus.yaml"
        assert default_manifest_path().is_file()


# ---------------------------------------------------------------------------
# registry digest resolution (metadata only)
# ---------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, body: bytes = b"", headers: dict | None = None):
        self._body = body
        # urllib headers are case-insensitive; mirror that.
        self.headers = {k.lower(): v for k, v in (headers or {}).items()}

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def make_opener(token: str = "tok", digest: str = "sha256:" + "ab" * 32):
    calls: list = []

    def opener(request, timeout):
        calls.append((request.get_method(), request.full_url))
        if "/token?" in request.full_url:
            assert request.get_method() == "GET"
            return FakeResponse(json.dumps({"token": token}).encode())
        assert request.get_method() == "HEAD"
        return FakeResponse(b"", {"Docker-Content-Digest": digest})

    opener.calls = calls  # type: ignore[attr-defined]
    return opener


class TestResolveLatestDigest:
    def test_resolves_via_token_and_head(self):
        opener = make_opener()
        digest = resolve_latest_digest("ghcr.io/freecodecamp/devdocs", opener=opener)
        assert digest == "ab" * 32
        methods_urls = opener.calls
        assert methods_urls[0][0] == "GET" and "/token?" in methods_urls[0][1]
        assert methods_urls[1][0] == "HEAD" and "/manifests/latest" in methods_urls[1][1]

    def test_bad_repo_rejected(self):
        with pytest.raises(DevDocsManifestError, match="bad image repo"):
            resolve_latest_digest("not-a-repo", opener=make_opener())

    def test_transport_failure_fails_closed(self):
        def boom(request, timeout):
            raise OSError("dns down")

        with pytest.raises(DevDocsManifestError, match="dns down"):
            resolve_latest_digest("ghcr.io/freecodecamp/devdocs", opener=boom)

    def test_unusable_digest_rejected(self):
        opener = make_opener(digest="not-a-digest")
        with pytest.raises(DevDocsManifestError, match="unusable digest"):
            resolve_latest_digest("ghcr.io/freecodecamp/devdocs", opener=opener)


# ---------------------------------------------------------------------------
# staging + version stamps
# ---------------------------------------------------------------------------


class FakeRun:
    """Fake subprocess.run for docker create/cp/rm."""

    def __init__(self, docsets: dict[str, dict] | None = None):
        self.cmds: list = []
        self.docsets = docsets or {"python": {"mtime": 111}}

    def __call__(self, cmd, **kwargs):
        self.cmds.append(cmd)
        if cmd[:2] == ["docker", "create"]:
            return SimpleNamespace(stdout="cid123\n")
        if cmd[:2] == ["docker", "cp"]:
            dest = Path(cmd[3])
            for name, index in self.docsets.items():
                d = dest / name
                d.mkdir(parents=True, exist_ok=True)
                (d / "db.json").write_text(json.dumps(index), encoding="utf-8")
            return SimpleNamespace(stdout="")
        return SimpleNamespace(stdout="")


class TestStageImageDocsets:
    def test_create_cp_rm_sequence(self, tmp_path):
        run = FakeRun()
        root = stage_image_docsets("ghcr.io/freecodecamp/devdocs@sha256:" + "ab" * 32,
                                   tmp_path, run=run)
        verbs = [c[1] for c in run.cmds]
        assert verbs == ["create", "cp", "rm"]
        assert run.cmds[0][2].startswith("ghcr.io/freecodecamp/devdocs@sha256:")
        assert root == tmp_path / "docs"
        assert (root / "python" / "db.json").is_file()

    def test_container_removed_on_cp_failure(self, tmp_path):
        class Boom(FakeRun):
            def __call__(self, cmd, **kwargs):
                self.cmds.append(cmd)
                if cmd[:2] == ["docker", "create"]:
                    return SimpleNamespace(stdout="cid9\n")
                if cmd[:2] == ["docker", "cp"]:
                    raise RuntimeError("no docker socket")
                return SimpleNamespace(stdout="")

        run = Boom()
        with pytest.raises(RuntimeError, match="no docker socket"):
            stage_image_docsets("img", tmp_path, run=run)
        assert run.cmds[-1] == ["docker", "rm", "cid9"]


class TestDocsetManifest:
    def test_mtime_is_the_version_stamp(self, tmp_path):
        d = tmp_path / "python"
        d.mkdir()
        (d / "db.json").write_text(json.dumps({"mtime": 1728000000, "entries": []}))
        assert docset_version_stamp(d) == "1728000000"

    def test_missing_mtime_falls_back_to_content_hash(self, tmp_path):
        d = tmp_path / "python"
        d.mkdir()
        (d / "index.json").write_text(json.dumps([{"path": "a", "name": "A"}]))
        stamp = docset_version_stamp(d)
        assert stamp is not None and stamp.startswith("sha256:")

    def test_no_index_returns_none(self, tmp_path):
        d = tmp_path / "empty"
        d.mkdir()
        assert docset_version_stamp(d) is None

    def test_manifest_from_stage(self, tmp_path):
        root = tmp_path / "docs"
        for name, mtime in (("python", 111), ("requests", 222)):
            d = root / name
            d.mkdir(parents=True)
            (d / "db.json").write_text(json.dumps({"mtime": mtime}))
        stray = root / "stray"
        stray.mkdir()
        assert docset_manifest_from_stage(root) == {"python": "111", "requests": "222"}

    def test_empty_stage_refuses(self, tmp_path):
        root = tmp_path / "docs"
        root.mkdir()
        with pytest.raises(DevDocsManifestError, match="no docsets found"):
            docset_manifest_from_stage(root)


# ---------------------------------------------------------------------------
# diff classifications
# ---------------------------------------------------------------------------


def _ready_version(repo, lib, version, source="seed"):
    row, _ = repo.get_or_create_version(lib.id, version, source=source)
    repo.mark_version_ready(row.id)
    return row


class TestDevdocsDiffClassifications:
    def test_changed_unchanged_added(self, repo):
        lib = repo.create_library(name="python", ecosystem=DEVDOCS_ECOSYSTEM)
        _ready_version(repo, lib, "3.12")
        lib2 = repo.create_library(name="requests", ecosystem=DEVDOCS_ECOSYSTEM)
        _ready_version(repo, lib2, "2.28")

        changes = devdocs_diff(
            {"python": "3.12", "requests": "2.31", "numpy": "2.0"}, repo
        )
        by = {c["docset"]: c for c in changes}
        assert by["python"]["classification"] == "unchanged"
        assert by["python"]["action"] == "skip"
        assert by["requests"]["classification"] == "changed"
        assert by["requests"]["action"] == "ingest"
        assert by["numpy"]["classification"] == "added"
        assert by["numpy"]["action"] == "ingest"

    def test_removed_docset(self, repo):
        gone = repo.create_library(name="gone-lib", ecosystem=DEVDOCS_ECOSYSTEM)
        _ready_version(repo, gone, "1.0")
        # Not DevDocs-sourced: must never be flagged removed.
        other = repo.create_library(name="other-lib")
        _ready_version(repo, other, "1.0")

        changes = devdocs_diff({"python": "3.12"}, repo)
        by = {c["docset"]: c for c in changes}
        assert by["gone-lib"]["classification"] == "removed"
        assert by["gone-lib"]["action"] == "skip"
        assert by["gone-lib"]["manifest_version"] is None
        assert by["gone-lib"]["catalog_version"] == "1.0"
        assert "other-lib" not in by

    def test_removed_never_ingests(self, repo):
        gone = repo.create_library(name="gone-lib", ecosystem=DEVDOCS_ECOSYSTEM)
        _ready_version(repo, gone, "1.0")
        ingested = [c for c in devdocs_diff({}, repo) if c["action"] == "ingest"]
        assert ingested == []


# ---------------------------------------------------------------------------
# impl: ecosystem marker
# ---------------------------------------------------------------------------


def _one_page_importer(docset_dir, *, library_id, version):
    return [
        Page(
            library_id=library_id,
            version=version,
            url="https://example.test/demo",
            title="Demo",
            markdown="# Demo\n\nbody",
            source="devdocs",
        )
    ]


def _noop_ingest(calls):
    def fake_ingest(library_id, version, pages, source, **kwargs):
        calls.append(library_id)
        catalog = kwargs["catalog"]
        for page in pages:
            catalog.upsert_page(page, chunk_count=0)
        catalog.activate_version(library_id, version)
        return {"pages": len(pages)}

    return fake_ingest


class TestDevdocsSyncImpl:
    def test_stamps_devdocs_ecosystem(self, repo):
        calls: list = []
        devdocs_sync_impl(
            {"demo": "1.0"},
            repo=repo,
            ingest=_noop_ingest(calls),
            extractor=lambda docset: Path("/fake") / docset,
            importer=_one_page_importer,
        )
        lib = repo.get_library_by_name("demo")
        assert lib is not None and lib.ecosystem == DEVDOCS_ECOSYSTEM

    def test_backfills_marker_on_existing_library(self, repo):
        lib = repo.create_library(name="demo")  # no marker (pre-marker row)
        calls: list = []
        devdocs_sync_impl(
            {"demo": "1.0"},
            repo=repo,
            ingest=_noop_ingest(calls),
            extractor=lambda docset: Path("/fake") / docset,
            importer=_one_page_importer,
        )
        assert repo.get_library_by_name("demo").ecosystem == DEVDOCS_ECOSYSTEM

    def test_require_explicit_extractor_fails_closed(self):
        with pytest.raises(DevDocsManifestError, match="no extractor"):
            _require_explicit_extractor("python")


# ---------------------------------------------------------------------------
# enqueue helper + entry point fail-fast
# ---------------------------------------------------------------------------


class TestEnqueueDevdocsSync:
    def test_enqueues_on_crawl_queue(self, monkeypatch):
        seen: dict = {}

        class FakeJob:
            id = "rq-42"

        class FakeQueue:
            def __init__(self, name, connection=None):
                seen["name"] = name
                seen["connection"] = connection

            def enqueue(self, func, **kwargs):
                seen["func"] = func
                seen["kwargs"] = kwargs
                return FakeJob()

        import rq

        monkeypatch.setattr(rq, "Queue", FakeQueue)
        fake_conn = object()
        assert enqueue_devdocs_sync(connection=fake_conn) == "rq-42"
        assert seen["name"] == jobs.CRAWL_QUEUE
        assert seen["connection"] is fake_conn
        assert seen["func"] == "services.crawler.jobs.devdocs_sync"


class TestDevdocsSyncEntryPoint:
    def test_no_manifest_missing_file_fails_fast(self, tmp_path, monkeypatch):
        """No bare NotImplementedError: names the manifest path instead."""
        missing = tmp_path / "absent.yaml"
        monkeypatch.setenv(DEVDOCS_MANIFEST_ENV, str(missing))
        with pytest.raises(DevDocsManifestError) as exc:
            devdocs_sync()
        assert str(missing) in str(exc.value)
        assert "NotImplementedError" not in type(exc.value).__name__
