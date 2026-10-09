"""CLI for artifact manifest staging checks (spec §14).

Staging-workstation only. Usage:

    python -m src.cli validate manifests/embeddinggemma-2.yaml
    python -m src.cli verify manifests/embeddinggemma-2.yaml --root /models/google/embeddinggemma-2
    python -m src.cli validate manifests/seed-corpus.yaml
    python -m src.cli unresolved manifests/seed-corpus.yaml
    python -m src.cli verify-seed manifests/seed-corpus.yaml --root /artifacts/seed-corpus
    python -m src.cli validate manifests/x.yaml --check-redirects
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .artifact_manifest import (
    ManifestError,
    check_redirects,
    find_unresolved_pins,
    load_manifest,
    validate_manifest,
    verify_checksums,
    verify_seed_corpus,
)

log = logging.getLogger("new_sage.artifacts")


def _cmd_validate(args: argparse.Namespace) -> int:
    log.info("validate: %s", args.manifest)
    try:
        manifest = load_manifest(args.manifest)
        warnings = validate_manifest(manifest)
    except ManifestError as exc:
        log.error("validate: %s: %s", args.manifest, exc)
        print(f"INVALID: {exc}", file=sys.stderr)
        return 1
    for warning in warnings:
        print(f"WARNING: {warning}")
    if args.check_redirects:
        for warning in check_redirects(manifest):
            print(f"WARNING: {warning}")
    log.info("validate: %s OK", manifest.artifact_name)
    print(f"OK: {manifest.artifact_name} manifest is valid")
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    log.info("verify: %s (root=%s)", args.manifest, args.root)
    try:
        manifest = load_manifest(args.manifest)
        validate_manifest(manifest)
    except ManifestError as exc:
        log.error("verify: %s: %s", args.manifest, exc)
        print(f"INVALID: {exc}", file=sys.stderr)
        return 1
    errors = verify_checksums(manifest, args.root)
    if errors:
        log.error("verify: %s: %d checksum failure(s)", args.manifest, len(errors))
        for error in errors:
            print(f"FAILED: {error}", file=sys.stderr)
        return 1
    log.info(
        "verify: %s OK (%d files)", manifest.artifact_name, len(manifest.files)
    )
    print(f"OK: all {len(manifest.files)} staged files verified for {manifest.artifact_name}")
    return 0


def _cmd_unresolved(args: argparse.Namespace) -> int:
    log.info("unresolved: %s", args.manifest)
    try:
        manifest = load_manifest(args.manifest)
        validate_manifest(manifest)
    except ManifestError as exc:
        log.error("unresolved: %s: %s", args.manifest, exc)
        print(f"INVALID: {exc}", file=sys.stderr)
        return 1
    unresolved = find_unresolved_pins(manifest)
    if unresolved:
        log.error(
            "unresolved: %s has %d unresolved pin(s)", args.manifest, len(unresolved)
        )
        for pin in unresolved:
            print(f"UNRESOLVED [{pin.kind}] {pin.ref}: {pin.detail}", file=sys.stderr)
        print(
            f"FAILED: {len(unresolved)} unresolved pin(s) in {manifest.artifact_name} "
            "(resolve them, or flag defer_to_staging with a staging_note)",
            file=sys.stderr,
        )
        return 1
    log.info("unresolved: %s OK (no unresolved pins)", manifest.artifact_name)
    print(f"OK: no unresolved pins in {manifest.artifact_name}")
    return 0


def _cmd_verify_seed(args: argparse.Namespace) -> int:
    log.info("verify-seed: %s (root=%s)", args.manifest, args.root)
    try:
        manifest = load_manifest(args.manifest)
        validate_manifest(manifest)
    except ManifestError as exc:
        log.error("verify-seed: %s: %s", args.manifest, exc)
        print(f"INVALID: {exc}", file=sys.stderr)
        return 1
    if not manifest.seed_sources:
        log.error("verify-seed: %s has no seed_sources", manifest.artifact_name)
        print(f"INVALID: {manifest.artifact_name} has no seed_sources", file=sys.stderr)
        return 1
    errors = verify_seed_corpus(manifest, args.root)
    if errors:
        log.error(
            "verify-seed: %s: %d seed source failure(s)", args.manifest, len(errors)
        )
        for error in errors:
            print(f"FAILED: {error}", file=sys.stderr)
        return 1
    log.info(
        "verify-seed: %s OK (%d sources)",
        manifest.artifact_name,
        len(manifest.seed_sources),
    )
    print(
        f"OK: staged seed corpus at {args.root} matches "
        f"all {len(manifest.seed_sources)} pinned sources"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(description="new_sage artifact manifest tooling")
    sub = parser.add_subparsers(dest="command", required=True)

    p_validate = sub.add_parser("validate", help="Validate manifest schema and source policy")
    p_validate.add_argument("manifest", type=Path)
    p_validate.add_argument(
        "--check-redirects",
        action="store_true",
        help="Resolve upstream URL and check the redirect target (needs network)",
    )
    p_validate.set_defaults(func=_cmd_validate)

    p_verify = sub.add_parser("verify", help="Verify sha256 checksums of staged files")
    p_verify.add_argument("manifest", type=Path)
    p_verify.add_argument("--root", type=Path, required=True, help="Staged artifact root")
    p_verify.set_defaults(func=_cmd_verify)

    p_unresolved = sub.add_parser(
        "unresolved",
        help="Fail if any pin is still a placeholder without an explicit staging deferral",
    )
    p_unresolved.add_argument("manifest", type=Path)
    p_unresolved.set_defaults(func=_cmd_unresolved)

    p_verify_seed = sub.add_parser(
        "verify-seed",
        help="Verify a staged seed corpus (git checkouts + image digest) against pinned seed sources",
    )
    p_verify_seed.add_argument("manifest", type=Path)
    p_verify_seed.add_argument(
        "--root", type=Path, required=True, help="Staged seed corpus root"
    )
    p_verify_seed.set_defaults(func=_cmd_verify_seed)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
