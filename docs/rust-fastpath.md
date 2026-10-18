# Optional Rust Markdown fast path — New_sage

## Decision: worthwhile candidate after staging validation

New_sage's real crawler still uses Python `trafilatura` /
`readability-lxml` to extract the main HTML body. The PyO3
`sage_markdown_fastpath` extension accelerates **only subsequent
Markdown normalization and heading/fence section parsing**; it
does not replace HTML extraction quality, token-based chunk budgets,
MiniLM tokenization, embedding, Milvus, or network requests.

Python implementations remain the default and are used automatically
for unusual Unicode line separators even when Rust is enabled.
The original Python code paths are fully retained in
`services/ingestion/html_to_md.py` and
`services/ingestion/doc_chunker.py`.
The Rust source is `rust/markdown_fastpath/`.

### Benchmark (illustrative, not end-to-end)

GitHub Actions CPython 3.12, Linux x86_64 (Run
[39092645703](https://github.com/CodeMasterGohan/new_sage/actions/runs/39092645703)):

| Microbenchmark | Python median | Rust median | Gain |
| --- | ---: | ---: | ---: |
| Markdown section parser, ~1,600 sections | 11.038 ms | 3.701 ms | 2.98× |
| Markdown whitespace/blank-line normalization | 0.847 ms | 0.150 ms | 5.65× |

Native wheel compiled and passed all 18 sample/parity tests (headings,
fenced code, CRLF, Unicode, synthetic 250-section corpus). This is not a
promise of matching end-to-end ingestion improvement: crawling is limited
by remote HTTP latency, robots.txt and the intentional one-request/second
per-host throttle; embedding and persistence also take time.

### Build a wheel and opt in

Use the exact Python interpreter/architecture of the intended worker
image (Python 3.11 in production) and a compatible Rust compiler:

```bash
python3.11 -m pip install 'maturin>=1.7,<2'
maturin build --release --interpreter python3.11 \
  --manifest-path rust/markdown_fastpath/Cargo.toml --out dist/rust
python3.11 -m pip install dist/rust/sage_markdown_fastpath-*.whl
SAGE_RUST_FASTPATH=1 python3.11 -m pytest -q -s \
  services/ingestion/tests/test_rust_markdown_fastpath.py
```

To activate for the real crawler and ingestion workers, first install
the built wheel into **both relevant container images** (not merely onto
the Docker host), then set `SAGE_RUST_FASTPATH=1` in both workers'
environment. The default images do not contain the Rust wheel; flipping
the env variable without installing it will fail explicitly.
Keep wheel versions pinned and build in reproducible CI.

For example, a derived deployment Dockerfile for each worker can
`FROM` the original built image, `COPY` in the wheel, and
`RUN pip install` the wheel; build wheels inside a compatible
manylinux Python 3.11 environment before the derived image. Do not
modify the existing clean-checkout Docker build to fetch a Rust toolchain
or dependencies at *runtime*. Run the whole ingest/crawl suite in staging
before selecting the derived image.

### Release criteria

- Baseline output/section breadcrumbs/chunk IDs and content hashes must
  remain identical on a representative DevDocs/MDN/tldr corpus.
- Benchmark 1000s of real pages: ingestion documents/second, process RSS,
  CPU and end-to-end latency. Capture failed/redirected crawls separately.
- Preserve crawl `robots.txt` policy, SSRF checks, backoff, redirect
  validation and the 1 request/s host limit; Rust must not bypass them.
- Roll back by removing `SAGE_RUST_FASTPATH` or setting it to `0`.
- CI unit and wheel parity checks are necessary, **not** a live production
  corpus benchmark.
