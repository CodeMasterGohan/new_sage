"""Artifact manifest validation and verification (spec §14).

Every approved model/dependency carries a manifest with a pinned upstream
revision, sha256 checksums, and a reviewed license. The P2 seed corpus
(DevDocs image, MDN content repo, tldr-pages repo — spec §5.1) is pinned the
same way via ``seed_sources``: each source records its pin (docker image
digest or full git commit SHA), its license, and the exact command used to
resolve the pin, so re-pinning is reproducible.

This module:

- parses and structurally validates manifests,
- enforces the source allowlist (prohibits modelscope.cn, Chinese mirrors,
  and unapproved endpoints),
- verifies sha256 checksums of staged files,
- reports unresolved pins (zeroed sha256 / TODO placeholders) that are not
  explicitly deferred to controlled staging.

Staging-time only. Nothing here runs in production services.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath
from urllib.parse import urlparse

import yaml

MANIFEST_VERSION = "1"

# Hosts permitted in ``upstream_source`` / seed ``upstream_url``. Staging
# operators extend this list; production never downloads, so this only gates
# the staging workstation.
# NOTE (new_sage): ghcr.io is added for the DevDocs monthly image
# (spec §5.1.1, ghcr.io/freecodecamp/devdocs).
DEFAULT_ALLOWED_HOSTS: tuple[str, ...] = (
    "huggingface.co",
    "cdn-lfs.huggingface.co",
    "cdn-lfs.hf.co",
    "github.com",
    "raw.githubusercontent.com",
    "ghcr.io",
    "pypi.org",
    "files.pythonhosted.org",
)

# Prohibited host patterns (matched against the URL host and its parent
# domains). This list is the floor, not the ceiling — operators extend it.
# modelscope.cn and Chinese mirror repositories are prohibited (same policy
# as new_DRUID).
DEFAULT_PROHIBITED_HOST_PATTERNS: tuple[str, ...] = (
    "modelscope.cn",
    "hf-mirror.com",
    "hf-mirror.aliyuncs.com",
)

# Revision values that are NOT immutable pins and must be rejected.
_MUTABLE_REVISIONS = frozenset(
    {"main", "master", "latest", "stable", "dev", "develop", "head"}
)

_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_FULL_GIT_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")

# sha256 value meaning "not yet recorded — fill in at controlled staging".
ZEROED_SHA256 = "0" * 64

# Placeholder markers that are only acceptable when the entry is explicitly
# deferred to staging (defer_to_staging: true + a staging_note).
_PLACEHOLDER_RES = (
    re.compile(r"TODO", re.IGNORECASE),
    re.compile(r"^\s*<[^>]+>\s*$"),
)

_SEED_SOURCE_KINDS = frozenset({"docker-image", "git-repo"})


class ManifestError(ValueError):
    """Raised when a manifest is structurally invalid or violates policy."""


def _is_placeholder(value: str) -> bool:
    return any(rx.search(value) for rx in _PLACEHOLDER_RES)


@dataclass(frozen=True)
class ManifestFile:
    path: str
    sha256: str
    # Per-entry upstream revision for pins that are not file bytes, e.g. a
    # full git commit SHA for a checked-out repo. Optional.
    revision: str | None = None
    # Explicit deferral: the pin/checksum is recorded at controlled staging.
    # Only valid together with a non-empty staging_note — a bare flag is
    # rejected at load time so exceptions stay auditable.
    defer_to_staging: bool = False
    staging_note: str | None = None


@dataclass(frozen=True)
class SeedSource:
    """One pinned P2 seed-corpus source (spec §5.1)."""

    name: str
    kind: str  # "docker-image" | "git-repo"
    upstream_url: str
    # The pin itself: image digest reference ("sha256:<hex> ...") or full
    # git commit SHA, or a TODO placeholder when explicitly deferred.
    revision: str
    # 64-hex image digest for kind == "docker-image" (accepts "sha256:<hex>"
    # on input, normalized to bare hex); empty for git repos, where the
    # commit SHA in ``revision`` is the pin.
    digest: str = ""
    license: str = ""
    license_note: str = ""
    # Exact command that resolves (or re-resolves) the pin. Always recorded,
    # even when the pin is already resolved, so the monthly DevDocs sync and
    # future seed refreshes are reproducible.
    resolution_command: str = ""
    # Local directory name under the staging root, e.g. "mdn-content".
    stage_dir: str = ""
    defer_to_staging: bool = False
    staging_note: str | None = None


@dataclass(frozen=True)
class ArtifactManifest:
    artifact_name: str
    upstream_source: str
    upstream_revision: str
    internal_location: str
    license: str
    files: tuple[ManifestFile, ...]
    approval_record: str
    seed_sources: tuple[SeedSource, ...] = ()


@dataclass(frozen=True)
class UnresolvedPin:
    """A pin that is still a placeholder and not explicitly deferred."""

    kind: str  # "file" | "seed-source"
    ref: str  # file path or seed source name
    detail: str


def _require_str(mapping: dict, key: str, where: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ManifestError(f"{where}: '{key}' must be a non-empty string")
    return value.strip()


def _optional_str(mapping: dict, key: str, where: str) -> str | None:
    value = mapping.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ManifestError(f"{where}: '{key}' must be a non-empty string if present")
    return value.strip()


def _load_deferral(entry: dict, where: str) -> tuple[bool, str | None]:
    defer = entry.get("defer_to_staging", False)
    if not isinstance(defer, bool):
        raise ManifestError(f"{where}: 'defer_to_staging' must be a boolean")
    note = _optional_str(entry, "staging_note", where)
    if defer and not note:
        raise ManifestError(
            f"{where}: 'defer_to_staging: true' requires a non-empty "
            "'staging_note' explaining what staging must do"
        )
    return defer, note


def _is_safe_relative_path(p: str) -> bool:
    p_posix = PurePosixPath(p)
    p_win = PureWindowsPath(p)
    if (
        p_posix.is_absolute()
        or p_win.is_absolute()
        or p.startswith(("/", "\\"))
        or ".." in p_posix.parts
        or ".." in p_win.parts
    ):
        return False
    return True


def _load_manifest_file(entry: dict, i: int, path: Path) -> ManifestFile:
    where = f"{path}: files[{i}]"
    if not isinstance(entry, dict):
        raise ManifestError(f"{where} must be a mapping")
    file_path = _require_str(entry, "path", where)
    sha256 = _require_str(entry, "sha256", where)
    if not _SHA256_RE.match(sha256):
        raise ManifestError(f"{where}.sha256 must be a 64-char hex digest")
    if not _is_safe_relative_path(file_path):
        raise ManifestError(f"{where}.path must be a relative path without '..'")
    revision = _optional_str(entry, "revision", where)
    defer, note = _load_deferral(entry, where)
    return ManifestFile(
        path=file_path,
        sha256=sha256.lower(),
        revision=revision,
        defer_to_staging=defer,
        staging_note=note,
    )


def _normalize_digest(digest: str, where: str) -> str:
    digest = digest.strip()
    if digest.lower().startswith("sha256:"):
        digest = digest[len("sha256:") :]
    if not _SHA256_RE.match(digest):
        raise ManifestError(
            f"{where}: 'digest' must be a 64-char hex image digest "
            "(optionally 'sha256:'-prefixed)"
        )
    return digest.lower()


def _load_seed_source(entry: dict, i: int, path: Path) -> SeedSource:
    where = f"{path}: seed_sources[{i}]"
    if not isinstance(entry, dict):
        raise ManifestError(f"{where} must be a mapping")
    name = _require_str(entry, "name", where)
    kind = _require_str(entry, "kind", where)
    if kind not in _SEED_SOURCE_KINDS:
        raise ManifestError(
            f"{where}: 'kind' must be one of {sorted(_SEED_SOURCE_KINDS)}"
        )
    upstream_url = _require_str(entry, "upstream_url", where)
    revision = _require_str(entry, "revision", where)
    digest_raw = entry.get("digest", "")
    if not isinstance(digest_raw, str):
        raise ManifestError(f"{where}: 'digest' must be a string")
    digest = ""
    if digest_raw.strip():
        if _is_placeholder(digest_raw):
            digest = digest_raw.strip()  # placeholder: validated by deferral rules
        else:
            digest = _normalize_digest(digest_raw, where)
    if kind == "docker-image" and not digest:
        raise ManifestError(
            f"{where}: kind 'docker-image' requires a 'digest' "
            "(or an explicitly deferred TODO placeholder)"
        )
    if kind == "git-repo" and not _FULL_GIT_SHA_RE.match(revision) and not _is_placeholder(revision):
        raise ManifestError(
            f"{where}: kind 'git-repo' requires 'revision' to be a full "
            "40-char commit SHA (or an explicitly deferred TODO placeholder)"
        )
    license_ = _require_str(entry, "license", where)
    license_note = entry.get("license_note", "")
    if not isinstance(license_note, str):
        raise ManifestError(f"{where}: 'license_note' must be a string")
    resolution_command = _require_str(entry, "resolution_command", where)
    stage_dir = _require_str(entry, "stage_dir", where)
    if not _is_safe_relative_path(stage_dir):
        raise ManifestError(f"{where}: 'stage_dir' must be a relative path without '..'")
    defer, note = _load_deferral(entry, where)
    return SeedSource(
        name=name,
        kind=kind,
        upstream_url=upstream_url,
        revision=revision,
        digest=digest,
        license=license_,
        license_note=license_note.strip(),
        resolution_command=resolution_command,
        stage_dir=stage_dir,
        defer_to_staging=defer,
        staging_note=note,
    )


def load_manifest(path: str | Path) -> ArtifactManifest:
    """Parse a manifest YAML file and structurally validate it."""
    path = Path(path)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ManifestError(f"{path}: invalid YAML: {exc}") from exc
    if not isinstance(raw, dict) or "artifact_manifest" not in raw:
        raise ManifestError(f"{path}: top-level 'artifact_manifest' mapping required")
    doc = raw["artifact_manifest"]
    if not isinstance(doc, dict):
        raise ManifestError(f"{path}: 'artifact_manifest' must be a mapping")

    seed_sources: list[SeedSource] = []
    seed_raw = doc.get("seed_sources", [])
    if not isinstance(seed_raw, list):
        raise ManifestError(f"{path}: 'seed_sources' must be a list")
    for i, entry in enumerate(seed_raw):
        seed_sources.append(_load_seed_source(entry, i, path))

    files_raw = doc.get("files")
    if files_raw is None:
        files_raw = []
    if not isinstance(files_raw, list):
        raise ManifestError(f"{path}: 'files' must be a list")
    if not files_raw and not seed_sources:
        raise ManifestError(
            f"{path}: 'files' must be a non-empty list "
            "(seed-corpus manifests may use an empty list when 'seed_sources' is set)"
        )
    files = [_load_manifest_file(entry, i, path) for i, entry in enumerate(files_raw)]

    return ArtifactManifest(
        artifact_name=_require_str(doc, "artifact_name", str(path)),
        upstream_source=_require_str(doc, "upstream_source", str(path)),
        upstream_revision=_require_str(doc, "upstream_revision", str(path)),
        internal_location=_require_str(doc, "internal_location", str(path)),
        license=_require_str(doc, "license", str(path)),
        files=tuple(files),
        approval_record=_require_str(doc, "approval_record", str(path)),
        seed_sources=tuple(seed_sources),
    )


def _host_matches(host: str, pattern: str) -> bool:
    host = host.lower()
    pattern = pattern.lower()
    return host == pattern or host.endswith("." + pattern)


def check_url_policy(
    url: str,
    *,
    allowed_hosts: tuple[str, ...] = DEFAULT_ALLOWED_HOSTS,
    prohibited_patterns: tuple[str, ...] = DEFAULT_PROHIBITED_HOST_PATTERNS,
) -> None:
    """Enforce the staging source policy on a URL. Raises ManifestError."""
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise ManifestError(f"upstream URL must be https, got: {url!r}")
    host = parsed.hostname or ""
    if not host:
        raise ManifestError(f"upstream URL has no host: {url!r}")
    for pattern in prohibited_patterns:
        if _host_matches(host, pattern):
            raise ManifestError(
                f"upstream URL host {host!r} is prohibited "
                f"(matches prohibited pattern {pattern!r})"
            )
    if not any(_host_matches(host, allowed) for allowed in allowed_hosts):
        raise ManifestError(f"upstream URL host {host!r} is not on the staging allowlist")


def check_source_policy(
    manifest: ArtifactManifest,
    *,
    allowed_hosts: tuple[str, ...] = DEFAULT_ALLOWED_HOSTS,
    prohibited_patterns: tuple[str, ...] = DEFAULT_PROHIBITED_HOST_PATTERNS,
) -> None:
    """Enforce the staging source policy. Raises ManifestError on violation."""
    check_url_policy(
        manifest.upstream_source,
        allowed_hosts=allowed_hosts,
        prohibited_patterns=prohibited_patterns,
    )


def check_revision_pinned(manifest: ArtifactManifest) -> list[str]:
    """Return warnings for revisions that are not immutable pins."""
    warnings: list[str] = []

    def _check(label: str, rev: str) -> None:
        if rev.lower() in _MUTABLE_REVISIONS:
            warnings.append(f"{label} {rev!r} is a mutable ref, not an immutable pin")
        if rev.startswith("<") and rev.endswith(">"):
            warnings.append(f"{label} {rev!r} looks like an unfilled template placeholder")

    _check("upstream_revision", manifest.upstream_revision)
    for src in manifest.seed_sources:
        if not src.defer_to_staging:
            _check(f"seed source {src.name!r} revision", src.revision)
    return warnings


def find_unresolved_pins(manifest: ArtifactManifest) -> list[UnresolvedPin]:
    """List pins that are still placeholders and NOT explicitly deferred.

    A zeroed sha256, a TODO revision, or a TODO digest fails here unless the
    entry carries ``defer_to_staging: true`` with a ``staging_note`` — the
    documented exception. Use this in CI/hygiene checks so placeholder pins
    can never slip through silently.
    """
    unresolved: list[UnresolvedPin] = []
    for entry in manifest.files:
        if entry.sha256 == ZEROED_SHA256 and not entry.defer_to_staging:
            unresolved.append(
                UnresolvedPin(
                    kind="file",
                    ref=entry.path,
                    detail="sha256 is zeroed and not flagged for staging",
                )
            )
        if entry.revision and _is_placeholder(entry.revision) and not entry.defer_to_staging:
            unresolved.append(
                UnresolvedPin(
                    kind="file",
                    ref=entry.path,
                    detail=f"revision {entry.revision!r} is a placeholder and not flagged for staging",
                )
            )
    for src in manifest.seed_sources:
        if src.defer_to_staging:
            continue
        if _is_placeholder(src.revision):
            unresolved.append(
                UnresolvedPin(
                    kind="seed-source",
                    ref=src.name,
                    detail=f"revision {src.revision!r} is a placeholder and not flagged for staging",
                )
            )
        if src.kind == "docker-image" and _is_placeholder(src.digest):
            unresolved.append(
                UnresolvedPin(
                    kind="seed-source",
                    ref=src.name,
                    detail=f"digest {src.digest!r} is a placeholder and not flagged for staging",
                )
            )
    return unresolved


def validate_manifest(
    manifest: ArtifactManifest,
    *,
    allowed_hosts: tuple[str, ...] = DEFAULT_ALLOWED_HOSTS,
    prohibited_patterns: tuple[str, ...] = DEFAULT_PROHIBITED_HOST_PATTERNS,
) -> list[str]:
    """Full policy validation. Raises ManifestError on violation; returns warnings."""
    if manifest.seed_sources:
        # Multi-source manifest: the top-level upstream_source is a bundle
        # marker ("multiple — see seed_sources"); policy is enforced per
        # source URL instead.
        for src in manifest.seed_sources:
            check_url_policy(
                src.upstream_url,
                allowed_hosts=allowed_hosts,
                prohibited_patterns=prohibited_patterns,
            )
    else:
        check_source_policy(
            manifest, allowed_hosts=allowed_hosts, prohibited_patterns=prohibited_patterns
        )
    return check_revision_pinned(manifest)


def verify_checksums(manifest: ArtifactManifest, root: str | Path) -> list[str]:
    """Verify sha256 of every staged file under ``root``.

    Returns a list of error strings; empty means all files verified.
    Entries explicitly deferred to staging are skipped here — their
    checksums are recorded (and verified) at staging time.
    """
    root = Path(root)
    errors: list[str] = []
    seen: set[str] = set()
    for entry in manifest.files:
        if entry.sha256 == ZEROED_SHA256 and entry.defer_to_staging:
            continue  # recorded at controlled staging; nothing to verify yet
        if entry.path in seen:
            errors.append(f"duplicate file entry: {entry.path}")
            continue
        seen.add(entry.path)
        target = root / entry.path
        if not target.is_file():
            errors.append(f"missing staged file: {entry.path}")
            continue
        digest = hashlib.sha256()
        with target.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                digest.update(chunk)
        actual = digest.hexdigest()
        if actual != entry.sha256:
            errors.append(
                f"checksum mismatch: {entry.path} "
                f"(expected {entry.sha256[:12]}…, got {actual[:12]}…)"
            )
    return errors


def _resolve_git_head(git_dir: Path) -> str | None:
    """Resolve .git/HEAD to a full commit SHA. Returns None if unreadable."""
    head = git_dir / "HEAD"
    try:
        content = head.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if content.startswith("ref:"):
        ref_path = git_dir / content[len("ref:") :].strip()
        try:
            return ref_path.read_text(encoding="utf-8").strip().lower()
        except OSError:
            return None
    if _FULL_GIT_SHA_RE.match(content):
        return content.lower()
    return None


def verify_seed_corpus(manifest: ArtifactManifest, root: str | Path) -> list[str]:
    """Verify a staged seed corpus against the pinned seed sources.

    Expected layout under ``root`` (see README staging procedure):

        <root>/<stage_dir>/.git/HEAD          for kind == "git-repo"
        <root>/<stage_dir>/digest.txt         for kind == "docker-image"
            (file content: "sha256:<64-hex digest>")

    Returns a list of error strings; empty means the staged corpus matches
    every pin. Sources explicitly deferred to staging are skipped.
    """
    root = Path(root)
    errors: list[str] = []
    for src in manifest.seed_sources:
        if src.defer_to_staging:
            continue
        stage = root / src.stage_dir
        if src.kind == "git-repo":
            if not (stage / ".git").is_dir():
                errors.append(f"{src.name}: missing git checkout at {stage}")
                continue
            actual = _resolve_git_head(stage / ".git")
            if actual is None:
                errors.append(f"{src.name}: could not resolve .git/HEAD at {stage}")
            elif actual != src.revision.lower():
                errors.append(
                    f"{src.name}: checkout is at {actual}, "
                    f"manifest pins {src.revision}"
                )
        elif src.kind == "docker-image":
            marker = stage / "digest.txt"
            try:
                actual = marker.read_text(encoding="utf-8").strip()
            except OSError:
                errors.append(f"{src.name}: missing digest marker at {marker}")
                continue
            want = f"sha256:{src.digest.lower()}"
            if actual.lower() != want:
                errors.append(
                    f"{src.name}: staged digest {actual!r} != pinned {want!r}"
                )
    return errors


def check_redirects(manifest: ArtifactManifest, timeout: float = 10.0) -> list[str]:
    """Staging-time check: resolve the upstream URL and verify the final host
    is still policy-compliant after redirects. Requires network — run on the
    staging workstation only, never in production."""
    import urllib.request

    warnings: list[str] = []
    request = urllib.request.Request(
        manifest.upstream_source, method="HEAD", headers={"User-Agent": "sage-staging/1"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            final_host = urlparse(response.geturl()).hostname or ""
    except Exception as exc:  # staging-time diagnostic, not a policy failure
        warnings.append(f"could not resolve upstream URL: {exc}")
        return warnings
    try:
        check_url_policy(f"https://{final_host}/")
    except ManifestError as exc:
        warnings.append(f"redirect target violates source policy: {exc}")
    return warnings
