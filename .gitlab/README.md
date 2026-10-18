# GitLab CI/CD deployment guide

The repository's `.gitlab-ci.yml` is a **GitLab pipeline configuration**.
It runs once the project is mirrored/imported to GitLab and GitLab runners
are enabled. Commits to GitHub alone do not execute GitLab CI.

## Pipeline responsibilities

1. **Validate:** Python source compilation, YAML/Compose interpolation,
   shell syntax and advisory Ruff static lint.
2. **Test:** service-specific offline contract/unit tests with JUnit
   reports; no model download, live database, GPU or external services.
3. **Security:** initial advisory dependency audit, Gitleaks secret scan
   and Trivy filesystem scan. Review findings; promote their
   `allow_failure` switches to blocking once a clean baseline is set.
4. **Rust:** Sage: build a CPython 3.11 PyO3 wheel with parity tests; DRUID has no Rust build job.
5. **Build:** Docker-in-Docker creates a distinct image
   `$CI_REGISTRY_IMAGE/<service>:$CI_COMMIT_SHA` for every owned service.
   Immutable commit-SHA tags are pushed on default-branch and tag
   pipelines. On merge requests, builds are optional/manual and do not push.
6. **Package:** retain a release-manifest artifact with exact commit/ref
   and hashes of Compose/image manifests; no secrets are bundled. Optional
   manual `image-scan-and-sbom` builds vulnerability reports and per-image
   CycloneDX SBOM artifacts from the released registry images.
7. **Deploy:** **manual only**, staging from the default branch and
   production from Git tags. Remote host has an existing secret .env and
   required staged models. Manual *migrate-* job precedes *deploy-* job
   for the exact image SHA. Migrations and Milvus setup are one-shot.
8. **Verify:** optional manual smoke check, using a privately reachable
   environment health URL; a separate *rollback-* job restores the last
   previous **container image tag only**.

## GitLab runner requirements

- A standard Linux/Docker executor runner to execute Python, YAML and
  shell tests (the images are supplied by the pipeline).
- A **dedicated isolated privileged** Docker executor runner tagged
  `docker-privileged` for Docker-in-Docker image builds. Change the tag
  if your runner uses another label. Do **not** run privileged DinD on a
  shared public runner. Docker service is `docker:27.5.1-dind`,
  job image `docker:27.5.1-cli`, using `DOCKER_HOST=tcp://docker:2375`
  with TLS disabled only on the isolated internal job service network.
  Adjust runner networking or move to TLS-enabled DinD / rootless BuildKit
  before deploying into a security-sensitive environment.
- The built-in GitLab Container Registry must be enabled. `CI_REGISTRY`,
  `CI_REGISTRY_IMAGE`, `CI_REGISTRY_USER`, `CI_REGISTRY_PASSWORD`,
  `CI_COMMIT_SHA`, `CI_DEFAULT_BRANCH` are standard GitLab variables.
- Registry image storage sufficient for one image per application service;
  enable registry retention/cleanup policies that **preserve deployed
  and previous image SHA tags** for rollback.
- Sufficient disk/CPU for Torch and native dependency images; ideally
  configure local registry/build-layer caches and concurrency limits.

## Variables to fill in later

Create per-environment **protected** CI/CD variables in GitLab Settings →
CI/CD → Variables. Use GitLab **File** type for the SSH key and
known_hosts variables. Mask where supported. Never commit secrets or
rely on an insecure `StrictHostKeyChecking=no` fallback.

| GitLab variable | Purpose |
| --- | --- |
| `DEPLOY_SSH_KEY_FILE` | File variable containing authorized deployment-only SSH private key |
| `DEPLOY_KNOWN_HOSTS_FILE` | File variable containing pinned trusted SSH server host public keys, verified out of band |
| `STAGING_DEPLOY_HOST` | Staging host resolvable from deployment CI runner |
| `STAGING_DEPLOY_USER` | Non-root staging deploy account with controlled Docker permissions |
| `STAGING_DEPLOY_PATH` | Absolute pre-provisioned app directory (e.g. `/opt/sage-staging`) |
| `PRODUCTION_DEPLOY_HOST` | Production host (only protected tag pipelines) |
| `PRODUCTION_DEPLOY_USER` | Production deploy account |
| `PRODUCTION_DEPLOY_PATH` | Absolute production app directory |
| `PRODUCTION_BACKUP_CONFIRMED` | Set to literal `YES` **for that production migration run only** after validating backups and an actual restore plan |
| `STAGING_HEALTHCHECK_URL` | Runner-accessible private staging health endpoint; smoke job is manual |
| `PRODUCTION_HEALTHCHECK_URL` | Runner-accessible private production health endpoint |
| `DOCKER_RUNNER_TAG` | Optional future override; currently the YAML uses the literal runner tag `docker-privileged` |

Environment deploy jobs are configured with `resource_group` to avoid
concurrent deployments to the same environment. Set protected GitLab
branches/tags and use **protected environments** and deployment approvals
when supported by your GitLab edition.

## Host prerequisites (must be done out of band)

- Docker Engine and Docker Compose V2 on each target; the SSH account
  must be authorized to run Docker. The host must have the network,
  persistent volume directories, correctly owned files, firewall, and
  offline AI model artifacts in place. Do not give deploy accounts
  unrestricted Docker permissions on shared sensitive hosts.
- Configure the remote host's Docker credential helper or read-only
  GitLab deploy-token registry authentication. The CI job transfers **no
  long-lived registry credentials**. Remote `docker compose pull` needs
  registry access. Store credentials securely on host.
- Staging and production directories must be separate and must not
  accidentally point to the same databases/volumes.
- No CI job copies the real `.env`, Postgres backups, Keycloak secrets,
  JWT signing keys, model weights, or SSH private keys into Docker images.
- `ssh-deploy.sh` only copies the versioned Compose files and image
  overlay. `remote-deploy.sh` verifies the existing local env file,
  pulls immutable images, and deploys **only long-running services**.
  One-shot schema services are invoked explicitly by the migration job.

## Normal release sequence

1. Create merge request; review offline tests/security findings.
2. Merge to GitLab's configured default branch after review. Wait for
   every normal image job to push the commit-SHA images.
3. Trigger `migrate-staging` manually. This starts infrastructure
   dependencies and runs schema + Milvus initialization for the **exact SHA**.
   Verify backup state before schema changes.
4. Trigger `deploy-staging` manually. It refuses deployment without
   the same-SHA migration-approval stamp. Run `smoke-staging`.
5. Create a protected Git tag for the approved revision. Wait for tag
   pipeline's image builds. Back up production and test restore.
6. Supply the per-run backup confirmation, trigger `migrate-production`
   manually, then `deploy-production`, then `smoke-production`.
7. For a *container-only* regression, use `rollback-staging` or
   `rollback-production`. **This does not reverse Alembic migrations,
   Milvus changes, object storage updates or document operations.**
   Validate schema backward compatibility before rollback.

## Security and operational boundaries

- Keep `.env` files only on deployment hosts (never in GitLab artifacts).
- Project credentials, Keycloak, Postgres and RustFS remain provisioned
  independently. The pipeline does not manage account authorization.
- There is no zero-downtime assurance: Compose's normal rolling behavior
  and health checks vary by service. Introduce blue/green deployments
  later if required, with traffic-drain and DB compatibility tests.
- Manual staging/production jobs use the exact SHA from the pipeline.
  Prefer protected Git tags over mutable `:latest` images.
- An image-only rollback cannot repair irreversible destructive migrations.
- A healthcheck URL should be reachable **only by the appropriate private
  runner**, not a new public endpoint created to satisfy CI.
- Configure notifications, retention, alerts, backups, and incident
  response in your GitLab/observability environment; no placeholders
  silently enable these integrations.

## Additional supply-chain controls to enable after setup

- Run the manual `image-scan-and-sbom` per-service jobs on candidate
  releases. Review vulnerability findings and preserve SBOM artifacts
  for your inventory/compliance process. The source/dependency scans
  and image scans are advisory initially, **not a security clearance**.
- Configure image-signing/attestations in your registry (e.g., Cosign)
  after you have selected your actual signing identity and trust policy.
  No default signing credential is invented by this template.
- Add SAST, policy-as-code, code-coverage thresholds, license policy,
  retention and runner security hardening as your environment requires.
- For tagged releases, retain old SHA-tagged container images and a
  tested database backup long enough to support a real rollback.

## GitLab CI lint

After you mirror/import the repository and configure the runner, open
**CI/CD → Editor → Validate** (or use the GitLab CI Lint API) to check
GitLab-specific YAML semantics against **your GitLab version**. The
GitHub workflow checks YAML parsing and Compose but is not a replacement
for the real instance's CI Lint validator.


## Sage-specific optional Rust acceleration

- GitLab's `rust-parity-and-wheel` job builds a CPython **3.11**
  PyO3/maturin wheel and runs Python/Rust parity tests. It retains the
  wheel for 14 days as a GitLab job artifact.
- The default published `ingestion:$SHA` and `crawler:$SHA` images
  still use Python. The separate `build-rust-workers` job is **manual**
  and builds `ingestion:$SHA-rust` and `crawler:$SHA-rust` after
  installing the vetted wheel into their Python 3.11 base images.
- To deploy the opt-in variants, set `RUST_WORKER_TAG_SUFFIX=-rust`
  in that Sage deployment host's `deploy/.env` and run a new
  migration/deploy job for a SHA for which **both** Rust worker images
  exist. The Compose overlay only switches the two workers; all other
  services retain standard images.
- Keep the default suffix blank for production until a real DevDocs,
  MDN and tldr corpus demonstrates identical chunk IDs and improved
  end-to-end ingestion. If the Rust worker fails, remove the suffix
  from the host `.env` and redeploy the normal images.
- Sage's MCP server has **no application authentication**. The default
  host binding is loopback-only and must remain behind a private
  firewall or trusted network boundary. **Do not** turn public
  internet exposure on as part of a deployment pipeline.
