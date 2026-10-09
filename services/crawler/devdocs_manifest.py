"""Manifest plumbing for the monthly DevDocs image sync (spec §5.1.1).

Built around the approved seed manifest
(``services/artifacts/manifests/seed-corpus.yaml``, APPROVAL-2026-101),
which pins the DevDocs image digest (``sha256:8623d9b9…b3d66a60``):

- :func:`load_devdocs_image_ref` — parse the manifest and return the
  digest-pinned image reference (repo + digest). No PyYAML: the crawler
  image is stdlib-only outside its pinned runtime deps, so this is a
  strict YAML-subset parser that understands exactly the structure this
  repo's manifest generator writes and fails closed on anything else.
- :func:`resolve_latest_digest` — metadata-only GHCR digest resolution
  (anonymous pull token + ``HEAD`` on ``manifests/latest``), stdlib
  ``urllib``. No image bytes move here; the pull itself stays in
  :func:`services.crawler.jobs._pull_devdocs_image`.
- :func:`stage_image_docsets` — ``docker create`` / ``docker cp`` /
  ``docker rm`` the docs tree out of the pulled image into a local stage
  directory (production-only; tests inject a fake ``run``).
- :func:`docset_manifest_from_stage` — build ``{docset: version-stamp}``
  from each staged docset's ``db.json``/``index.json``. The version stamp
  is DevDocs' own ``mtime`` field when present (DevDocs only re-scrapes
  changed docsets, so an unchanged docset keeps its stamp and diffs as
  "unchanged"), falling back to a content hash of the index file.

Network use is limited to the explicitly approved steps: the registry
metadata call and the ``docker pull`` itself. Everything else is local.
All failures raise :class:`DevDocsManifestError` (a ``ValueError``) with
an actionable message — never a bare ``NotImplementedError``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request

log = logging.getLogger("sage.crawler.devdocs_manifest")

#: Env var overriding where the approved seed manifest is read from.
DEVDOCS_MANIFEST_ENV = "DEVDOCS_MANIFEST_PATH"

#: Docset tree inside the DevDocs image. Assumption to verify against the
#: real image on first production run; overridable via env.
DEVDOCS_IMAGE_DOCS_PATH_ENV = "DEVDOCS_IMAGE_DOCS_PATH"
DEFAULT_IMAGE_DOCS_PATH = "/app/public/docs"

#: The manifest this plumbing understands.
EXPECTED_ARTIFACT_NAME = "new_sage/seed-corpus"

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class DevDocsManifestError(ValueError):
    """The DevDocs sync cannot proceed: bad/missing manifest, unresolvable
    digest, or an image that cannot be staged. Always carries an actionable
    message (path, env var, or command involved)."""


@dataclass(frozen=True)
class DevDocsImageRef:
    """Digest-pinned DevDocs image reference from the approved manifest."""

    repo: str  # e.g. "ghcr.io/freecodecamp/devdocs"
    digest: str  # bare 64-hex sha256
    approval_record: str
    manifest_path: Path

    @property
    def pinned(self) -> str:
        """The manifest-pinned image reference (``repo@sha256:<digest>``)."""
        return "%s@sha256:%s" % (self.repo, self.digest)


def default_manifest_path() -> Path:
    """Where the approved seed manifest lives.

    ``DEVDOCS_MANIFEST_PATH`` wins; otherwise
    ``services/artifacts/manifests/seed-corpus.yaml`` next to this
    package (which is also where the crawler-worker image carries it).
    """
    env = os.environ.get(DEVDOCS_MANIFEST_ENV)
    if env:
        return Path(env)
    return (
        Path(__file__).resolve().parent.parent
        / "artifacts"
        / "manifests"
        / "seed-corpus.yaml"
    )


# ---------------------------------------------------------------------------
# strict YAML-subset parser (no PyYAML in the crawler image)
# ---------------------------------------------------------------------------


def _strip_scalar(raw: str, where: str) -> str:
    text = raw.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        return text[1:-1]
    if text[:1] in ("'", '"'):
        raise DevDocsManifestError("%s: unterminated quote in %r" % (where, raw))
    return text


def _parse_seed_corpus_yaml(text: str, path: Path) -> dict:
    """Parse the approved manifest's fixed structure.

    Understands exactly what this repo's manifest generator writes:
    a top-level ``artifact_manifest:`` mapping, scalar fields at one
    indent level, a ``seed_sources:`` list of ``- `` mappings, and ``|``
    literal blocks. Comments and blank lines are skipped. Anything else
    raises :class:`DevDocsManifestError` naming the line.
    """
    where = str(path)
    top: dict = {}
    sources: list[dict] = []
    current: dict | None = None
    literal_key: str | None = None
    literal_lines: list[str] = []
    literal_indent = 0
    in_sources = False
    seen_top = False

    def end_literal() -> None:
        nonlocal literal_key, literal_lines
        if literal_key is not None and current is not None:
            # Clip chomping: literal content, single trailing newline.
            current[literal_key] = "\n".join(literal_lines).rstrip("\n") + "\n"
        literal_key = None
        literal_lines = []

    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        loc = "%s:%d" % (where, lineno)
        if literal_key is not None:
            indent = len(raw_line) - len(raw_line.lstrip(" "))
            if raw_line.strip() == "" or indent > literal_indent:
                literal_lines.append(
                    raw_line[literal_indent:] if len(raw_line) > literal_indent else ""
                )
                continue
            end_literal()
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "\t" in raw_line[: len(raw_line) - len(raw_line.lstrip())]:
            raise DevDocsManifestError("%s: tabs are not allowed" % loc)
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        body = raw_line.strip()

        if indent == 0:
            if body != "artifact_manifest:":
                raise DevDocsManifestError(
                    "%s: expected top-level 'artifact_manifest:', got %r"
                    % (loc, body)
                )
            seen_top = True
            in_sources = False
        elif indent == 2:
            if not seen_top:
                raise DevDocsManifestError("%s: content before 'artifact_manifest:'" % loc)
            if body == "seed_sources:":
                in_sources = True
                continue
            in_sources = False  # a scalar field ends the seed_sources list
            if ":" not in body:
                raise DevDocsManifestError("%s: expected 'key: value', got %r" % (loc, body))
            key, _, value = body.partition(":")
            key = key.strip()
            if not key or " " in key:
                raise DevDocsManifestError("%s: bad key %r" % (loc, key))
            value = value.strip()
            if value == "[]":
                top[key] = []
            elif value:
                top[key] = _strip_scalar(value, loc)
            else:
                raise DevDocsManifestError(
                    "%s: key %r needs a value or '[]' (only 'seed_sources:' "
                    "may be a nested block)" % (loc, key)
                )
        elif indent == 4:
            if not body.startswith("- "):
                raise DevDocsManifestError(
                    "%s: expected '- ' list item, got %r" % (loc, body)
                )
            current = {}
            sources.append(current)
            in_sources = True
            item_body = body[2:]
            if ":" not in item_body:
                raise DevDocsManifestError(
                    "%s: expected '- key: value', got %r" % (loc, body)
                )
            key, _, value = item_body.partition(":")
            key = key.strip()
            if not key or " " in key:
                raise DevDocsManifestError("%s: bad key %r" % (loc, key))
            current[key] = _strip_scalar(value.strip(), loc)
        elif indent == 6:
            if current is None:
                raise DevDocsManifestError("%s: field outside a list item" % loc)
            if ":" not in body:
                raise DevDocsManifestError("%s: expected 'key: value', got %r" % (loc, body))
            key, _, value = body.partition(":")
            key = key.strip()
            value = value.strip()
            if not key or " " in key:
                raise DevDocsManifestError("%s: bad key %r" % (loc, key))
            if value == "|":
                literal_key = key
                literal_lines = []
                literal_indent = indent
            elif value:
                current[key] = _strip_scalar(value, loc)
            else:
                raise DevDocsManifestError(
                    "%s: key %r needs a value or '|' block" % (loc, key)
                )
        else:
            raise DevDocsManifestError(
                "%s: unsupported indentation (%d spaces)" % (loc, indent)
            )

    end_literal()
    if not seen_top:
        raise DevDocsManifestError("%s: no 'artifact_manifest:' section found" % where)
    top["seed_sources"] = sources
    return top


def _is_placeholder(value: str) -> bool:
    return "todo" in value.lower()


def _normalize_digest(raw: str, where: str) -> str:
    digest = raw.strip()
    if digest.lower().startswith("sha256:"):
        digest = digest[len("sha256:") :]
    if not _SHA256_RE.match(digest.lower()):
        raise DevDocsManifestError(
            "%s: 'digest' must be a 64-char hex image digest "
            "(optionally 'sha256:'-prefixed), got %r" % (where, raw)
        )
    return digest.lower()


def _image_repo(upstream_url: str, where: str) -> str:
    try:
        parts = urlsplit(upstream_url.strip())
    except ValueError as exc:
        raise DevDocsManifestError(
            "%s: bad 'upstream_url' %r: %s" % (where, upstream_url, exc)
        ) from None
    if parts.scheme not in ("http", "https") or not parts.netloc or not parts.path.strip("/"):
        raise DevDocsManifestError(
            "%s: 'upstream_url' must be an http(s) URL with a repository "
            "path, got %r" % (where, upstream_url)
        )
    return parts.netloc + "/" + parts.path.strip("/")


def load_devdocs_image_ref(manifest_path: str | Path | None = None) -> DevDocsImageRef:
    """Parse the approved seed manifest → digest-pinned image reference.

    Raises :class:`DevDocsManifestError` naming the path (and the
    ``DEVDOCS_MANIFEST_PATH`` override) when the file is missing, and
    naming the offending field when a pin is malformed. Never guesses.
    """
    path = Path(manifest_path) if manifest_path is not None else default_manifest_path()
    if not path.is_file():
        raise DevDocsManifestError(
            "DevDocs seed manifest not found at %s. The monthly sync needs "
            "the APPROVAL-2026-101 manifest (services/artifacts/manifests/"
            "seed-corpus.yaml); point %s at it or mount it into the "
            "crawler-worker image." % (path, DEVDOCS_MANIFEST_ENV)
        )
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise DevDocsManifestError(
            "cannot read DevDocs seed manifest at %s: %s" % (path, exc)
        ) from None

    parsed = _parse_seed_corpus_yaml(text, path)
    where = str(path)

    artifact_name = parsed.get("artifact_name", "")
    if artifact_name != EXPECTED_ARTIFACT_NAME:
        raise DevDocsManifestError(
            "%s: expected artifact_name %r, got %r — refusing to sync "
            "against an unrecognized manifest" % (where, EXPECTED_ARTIFACT_NAME, artifact_name)
        )
    approval = parsed.get("approval_record", "")
    if not approval or _is_placeholder(approval):
        raise DevDocsManifestError(
            "%s: 'approval_record' is missing or a placeholder — the sync "
            "only runs against an approved manifest" % where
        )

    sources = parsed.get("seed_sources") or []
    if not sources:
        raise DevDocsManifestError("%s: 'seed_sources' is empty" % where)
    entry = next((s for s in sources if s.get("kind") == "docker-image"), None)
    if entry is None:
        raise DevDocsManifestError(
            "%s: no seed_source with kind 'docker-image' found" % where
        )
    entry_where = "%s: seed_sources entry %r" % (where, entry.get("name", "?"))
    digest = _normalize_digest(entry.get("digest", ""), entry_where)
    repo = _image_repo(entry.get("upstream_url", ""), entry_where)

    log.info(
        "devdocs manifest: image=%s pinned=%s approval=%r",
        repo,
        "sha256:%s" % digest,
        approval,
    )
    return DevDocsImageRef(
        repo=repo, digest=digest, approval_record=approval, manifest_path=path
    )


# ---------------------------------------------------------------------------
# registry metadata (no image bytes)
# ---------------------------------------------------------------------------


def _urlopen(request: Request, timeout: float):
    from urllib.request import urlopen

    return urlopen(request, timeout=timeout)


def resolve_latest_digest(
    repo: str,
    *,
    opener=None,
    timeout: float = 30.0,
) -> str:
    """Resolve ``repo``'s ``:latest`` image-index digest (metadata only).

    Anonymous registry token + ``HEAD`` on ``manifests/latest`` — the same
    call the manifest's ``resolution_command`` documents. ``opener`` is an
    injectable ``(Request, timeout) ->`` context-manager response (tests
    fake it). Fails closed: any transport or parse problem raises
    :class:`DevDocsManifestError`.
    """
    host, _, image_path = repo.partition("/")
    if not host or not image_path:
        raise DevDocsManifestError(
            "bad image repo %r: expected 'host/namespace/name'" % repo
        )
    open_fn = opener or _urlopen

    def fetch(request: Request, what: str):
        try:
            with open_fn(request, timeout) as resp:
                return resp
        except DevDocsManifestError:
            raise
        except Exception as exc:
            raise DevDocsManifestError(
                "DevDocs registry metadata fetch failed (%s): %s: %s "
                "(offline? DNS? registry down?)" % (what, type(exc).__name__, exc)
            ) from None

    token_url = (
        "https://%s/token?service=%s&scope=repository:%s:pull"
        % (host, host, image_path)
    )
    token_req = Request(token_url, headers={"Accept": "application/json"})
    resp = fetch(token_req, "token")
    try:
        payload = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        raise DevDocsManifestError(
            "bad token response from %s: %s" % (host, exc)
        ) from None
    token = payload.get("token") or payload.get("access_token")
    if not token:
        raise DevDocsManifestError(
            "registry %s returned no token for repository:%s:pull" % (host, image_path)
        )

    manifest_url = "https://%s/v2/%s/manifests/latest" % (host, image_path)
    head_req = Request(
        manifest_url,
        method="HEAD",
        headers={
            "Authorization": "Bearer %s" % token,
            "Accept": "application/vnd.oci.image.index.v1+json",
        },
    )
    head_resp = fetch(head_req, "manifest HEAD")
    digest = head_resp.headers.get("docker-content-digest", "")
    digest = digest.strip()
    if digest.lower().startswith("sha256:"):
        digest = digest[len("sha256:") :]
    if not _SHA256_RE.match(digest.lower()):
        raise DevDocsManifestError(
            "registry %s returned an unusable digest for %s:latest: %r"
            % (host, image_path, digest)
        )
    digest = digest.lower()
    log.info("devdocs registry: %s:latest resolves to sha256:%s", repo, digest)
    return digest


# ---------------------------------------------------------------------------
# image staging (docker; production-only)
# ---------------------------------------------------------------------------


def image_docs_path() -> str:
    """Docset tree inside the DevDocs image (env-overridable assumption)."""
    return os.environ.get(DEVDOCS_IMAGE_DOCS_PATH_ENV, DEFAULT_IMAGE_DOCS_PATH)


def stage_image_docsets(
    image_ref: str,
    stage_root: str | Path,
    *,
    run=None,
    docs_path: str | None = None,
) -> Path:
    """Copy the docs tree out of a pulled image into ``stage_root/docs``.

    ``docker create`` + ``docker cp`` + ``docker rm`` (never ``docker
    run``): the container is never executed. ``run`` is an injectable
    ``subprocess.run``-compatible callable (tests fake it). Returns the
    staged docsets directory. Production-only — needs the docker socket.
    """
    run = run or subprocess.run
    docs_path = docs_path or image_docs_path()
    stage_root = Path(stage_root)
    stage_root.mkdir(parents=True, exist_ok=True)

    proc = run(
        ["docker", "create", image_ref],
        check=True,
        capture_output=True,
        text=True,
        timeout=300,
    )
    container_id = (proc.stdout or "").strip().splitlines()
    if not container_id or not container_id[0].strip():
        raise DevDocsManifestError(
            "'docker create %s' printed no container id" % image_ref
        )
    cid = container_id[0].strip()
    try:
        dest = stage_root / "docs"
        run(
            ["docker", "cp", "%s:%s" % (cid, docs_path), str(dest)],
            check=True,
            timeout=1800,
        )
    finally:
        run(["docker", "rm", cid], check=False, timeout=120)
    if not dest.is_dir():
        raise DevDocsManifestError(
            "docset tree %r not found in image %s (tried %s:%s); check %s"
            % (docs_path, image_ref, cid, docs_path, DEVDOCS_IMAGE_DOCS_PATH_ENV)
        )
    log.info("devdocs sync: staged image docsets at %s", dest)
    return dest


# ---------------------------------------------------------------------------
# {docset: version} from a staged tree
# ---------------------------------------------------------------------------


def docset_version_stamp(docset_dir: str | Path) -> str | None:
    """Version stamp for one staged docset directory.

    DevDocs' own ``mtime`` from ``db.json``/``index.json`` when present
    (DevDocs only re-scrapes changed docsets, so an unchanged docset keeps
    its stamp); otherwise a content hash of the index file. ``None`` when
    the directory has no page index at all.
    """
    docset_dir = Path(docset_dir)
    for name in ("db.json", "index.json"):
        index = docset_dir / name
        if not index.is_file():
            continue
        try:
            raw = index.read_bytes()
        except OSError:
            continue
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            continue
        if isinstance(data, dict) and data.get("mtime") is not None:
            return str(data["mtime"])
        return "sha256:" + hashlib.sha256(raw).hexdigest()[:16]
    return None


def docset_manifest_from_stage(docsets_root: str | Path) -> dict[str, str]:
    """Build ``{docset: version-stamp}`` for every staged docset.

    Directories without a page index are skipped with a warning (never
    fatal: a stray directory must not abort the whole monthly sync).
    """
    docsets_root = Path(docsets_root)
    if not docsets_root.is_dir():
        raise DevDocsManifestError(
            "staged docset root %s is not a directory" % docsets_root
        )
    manifest: dict[str, str] = {}
    for child in sorted(docsets_root.iterdir()):
        if not child.is_dir():
            continue
        stamp = docset_version_stamp(child)
        if stamp is None:
            log.warning(
                "devdocs sync: skipping %s: no index.json/db.json", child.name
            )
            continue
        manifest[child.name] = stamp
    if not manifest:
        raise DevDocsManifestError(
            "no docsets found under %s — refusing to sync an empty manifest "
            "(would mark every catalogued docset removed)" % docsets_root
        )
    log.info("devdocs sync: %d docset(s) in staged manifest", len(manifest))
    return manifest
