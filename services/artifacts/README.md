# artifacts — artifact manifest tooling (spec §14)

Controlled-staging tooling for new_sage's offline-first artifact policy.
Runs on the staging workstation only — never in production services.

## Manifests

`manifests/` holds one YAML per approved artifact:

| Manifest | What it pins |
|---|---|
| `embeddinggemma-2.yaml` | `google/embeddinggemma-2` — pinned revision + sha256, Apache-2.0. Copied **byte-identical** from new_DRUID's completed research draft. |
| `minilm-l6-v2.yaml` | `cross-encoder/ms-marco-MiniLM-L6-v2` (+ tokenizer) — pinned revision + sha256, Apache-2.0. Copied **byte-identical** from new_DRUID's completed research draft. |
| `seed-corpus.yaml` | P2 seed corpus (spec §5.1): DevDocs image digest, MDN content git SHA, tldr-pages git SHA, licenses, and the exact resolution command for each pin. |

The two model manifests still carry zeroed sha256 entries for their small
git-stored config files — their headers document that these MUST be recorded
at controlled staging before approval. The hygiene test
(`test_no_unflagged_placeholders`) knows about these via an explicit,
auditable registry in `tests/test_seed_corpus.py`; any *new* placeholder
without an explicit `defer_to_staging: true` + `staging_note` fails the suite.

## Where artifacts live

Staged artifacts live on the **`/artifacts`** volume on the deployment box
(spec §13: H200 box alongside new_DRUID):

```
/artifacts/
├── models/
│   ├── google/embeddinggemma-2/      # model.safetensors, tokenizer.*, configs
│   └── cross-encoder/ms-marco-MiniLM-L6-v2/
└── seed-corpus/
    ├── devdocs-image/digest.txt      # "sha256:<image digest>"
    ├── mdn-content/                  # git checkout of mdn/content at pinned SHA
    └── tldr/                         # git checkout of tldr-pages/tldr at pinned SHA
```

Model files may share new_DRUID's `/models` tree when the pins are identical
(same bytes, same manifest) — or live under `/artifacts/models`; the
manifest's `internal_location` is the source of truth for where `verify`
looks.

## How services fail closed without them

Production services never download. The compose stack mounts `/artifacts`
read-only, and each service asserts its required pins at startup:

- embedding service: `verify` checksums under the embeddinggemma-2 root —
  any missing file or mismatch aborts startup.
- retrieval/reranker: same for the MiniLM root.
- ingestion workers (seed path): `verify-seed` checks that `mdn-content/`
  and `tldr/` checkouts resolve to the pinned commit SHAs and that
  `devdocs-image/digest.txt` matches the pinned image digest — any drift
  aborts the seed job before a single chunk is embedded.

The tooling in this directory is the staging-side enforcement of that
contract. Service-side startup assertions land with the services themselves
(later build phases); until then, run the checks below before every deploy.

## Usage

```bash
# from the build root: ~/workspace/new_sage/build
cd ~/workspace/new_sage/build

# Schema + source-policy check (offline)
python -m services.artifacts.src.cli validate services/artifacts/manifests/seed-corpus.yaml
# (or via the Docker image: ENTRYPOINT is `python -m src.cli`)

# Fail if any pin is still a placeholder without an explicit staging deferral
python -m services.artifacts.src.cli unresolved services/artifacts/manifests/seed-corpus.yaml

# Also verify the redirect target host (needs network, staging only)
python -m services.artifacts.src.cli validate services/artifacts/manifests/embeddinggemma-2.yaml --check-redirects

# Verify staged model file checksums
python -m services.artifacts.src.cli verify services/artifacts/manifests/embeddinggemma-2.yaml \
  --root /artifacts/models/google/embeddinggemma-2

# Verify a staged seed corpus (git SHAs + image digest)
python -m services.artifacts.src.cli verify-seed services/artifacts/manifests/seed-corpus.yaml \
  --root /artifacts/seed-corpus
```

## Policy

- `upstream_source` / seed `upstream_url` must be `https` on an allowlisted host.
- Prohibited: `modelscope.cn` (+ subdomains), `hf-mirror.com`,
  `hf-mirror.aliyuncs.com`, and any host not on the allowlist. `ghcr.io` is
  allowlisted for the DevDocs image. Extend `DEFAULT_PROHIBITED_HOST_PATTERNS`
  per operator policy.
- `upstream_revision` must be an immutable pin — branch names (`main`,
  `latest`, …) warn loudly. Git-repo seed sources must pin a full 40-char
  commit SHA; docker-image seed sources must pin the 64-hex image digest.
- Placeholders (`0000…` sha256, `TODO-…` pins) are only acceptable with an
  explicit `defer_to_staging: true` + `staging_note`, or a registry entry in
  the hygiene test. Everything else fails `unresolved` and the test suite.

## Staging procedure (for James's box)

All three seed pins are already resolved (2026-10-08, metadata-only lookups —
no corpus downloaded). To stage on the H200 box:

1. **DevDocs image** — pull by digest (never `:latest` at runtime):
   ```bash
   DIGEST=sha256:8623d9b9c6c3ac12bafbd3e378711c6f2d61a0baf23350c9da6e9261b3d66a60
   docker pull "ghcr.io/freecodecamp/devdocs@${DIGEST}"
   mkdir -p /artifacts/seed-corpus/devdocs-image
   echo "sha256:8623d9b9c6c3ac12bafbd3e378711c6f2d61a0baf23350c9da6e9261b3d66a60" \
     > /artifacts/seed-corpus/devdocs-image/digest.txt
   ```
   The monthly DevDocs sync (spec §5.1.1) re-runs the manifest's
   `resolution_command` and updates the pin — the compose job does this, not you.
2. **MDN content** — clone at the pinned SHA (shallow is fine; the SHA is the pin):
   ```bash
   git clone https://github.com/mdn/content.git /artifacts/seed-corpus/mdn-content
   git -C /artifacts/seed-corpus/mdn-content checkout fd0b11ad5b5014a9333578adcc4bc98fb9024da3
   ```
3. **tldr-pages** — same pattern:
   ```bash
   git clone https://github.com/tldr-pages/tldr.git /artifacts/seed-corpus/tldr
   git -C /artifacts/seed-corpus/tldr checkout 6bbd7cc43d1c5bd12e35a1f2245eea9144fde1ba
   ```
4. **Models** — download from huggingface.co at the pinned revisions into the
   `internal_location` paths, then fill in the zeroed config-file sha256 in
   the two model manifests (their headers say which entries), and re-run:
   ```bash
   python -m services.artifacts.src.cli verify services/artifacts/manifests/embeddinggemma-2.yaml --root /artifacts/models/google/embeddinggemma-2
   python -m services.artifacts.src.cli verify services/artifacts/manifests/minilm-l6-v2.yaml --root /artifacts/models/cross-encoder/ms-marco-MiniLM-L6-v2
   python -m services.artifacts.src.cli verify-seed services/artifacts/manifests/seed-corpus.yaml --root /artifacts/seed-corpus
   python -m services.artifacts.src.cli unresolved services/artifacts/manifests/embeddinggemma-2.yaml
   ```
   `unresolved` must print OK before the manifest is considered complete.
   (Approval records are still pending your sign-off — same as new_DRUID.)

To advance a seed pin later (e.g. newer MDN snapshot), re-run the source's
`resolution_command`, update `revision` in `seed-corpus.yaml`, and re-run the
test suite — the pin assertions in `tests/test_seed_corpus.py` will tell you
exactly what changed.

## Tests

```bash
cd ~/workspace/new_sage/build
python -m pytest services/artifacts/tests/ -v
```
