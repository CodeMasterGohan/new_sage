"""Live black-box retrieval evaluation against New_sage's POST /retrieve API.

No fake embeddings or model stubs are used when this runner is executed against
a running service. The example dataset is a smoke checklist, NOT a calibrated
gold-standard benchmark. All scores are observational; don't claim answer
faithfulness or hallucination rates from retrieval metrics.
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit


def valid_citation_url(value: str) -> bool:
    """Absolute web citations only; no userinfo, placeholder or local file."""
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    return bool(
        parsed.scheme in ("https", "http")
        and parsed.hostname
        and not parsed.username
        and not parsed.password
        and parsed.hostname not in ("example.invalid", "localhost")
    )


def load_cases(path: str | Path) -> list[dict]:
    rows = []
    for line_no, text in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if not text.strip():
            continue
        row = json.loads(text)
        if not isinstance(row, dict):
            raise ValueError(f"line {line_no}: expected JSON object")
        for key in ("library_id", "version", "topic"):
            if not isinstance(row.get(key), str) or not row[key].strip():
                raise ValueError(f"line {line_no}: missing nonblank {key}")
        rows.append(row)
    if not rows:
        raise ValueError("evaluation file is empty")
    return rows


def percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    values = sorted(values)
    idx = math.ceil(pct * len(values)) - 1
    return round(values[max(0, min(idx, len(values) - 1))], 2)


def evaluate(cases: list[dict], retrieve, *, k: int = 5) -> dict:
    """Run a real retrieval client or an injected contract fake for tests."""
    if k < 1:
        raise ValueError("k must be >= 1")
    n = 0
    exact_versions = 0
    total_citations = 0
    valid_citations = 0
    symbol_total = 0
    symbol_hit = 0
    source_total = 0
    source_hit = 0
    latency_ms: list[float] = []
    failures: list[dict] = []
    for row in cases:
        n += 1
        before = time.perf_counter()
        try:
            resp = retrieve({
                "library_id": row["library_id"],
                "version": row["version"],
                "topic": row["topic"],
                "top_k": max(1, k),
                "token_budget": 8000,
            })
            if not isinstance(resp, dict):
                raise TypeError("retrieve returned a non-object")
            latency_ms.append((time.perf_counter() - before) * 1000)
        except Exception as exc:
            failures.append({"case": row.get("id", n), "error": str(exc)[:300]})
            continue
        if resp.get("version") == row["version"] and not resp.get("version_fallback"):
            exact_versions += 1
        passages = resp.get("passages") or []
        if not isinstance(passages, list):
            failures.append({"case": row.get("id", n), "error": "passages is not a list"})
            continue
        passages = passages[:k]
        for passage in passages:
            total_citations += 1
            if valid_citation_url(str(passage.get("url") or "")):
                valid_citations += 1
        symbol = row.get("expected_symbol")
        if symbol:
            symbol_total += 1
            if any(symbol.casefold() in str(p.get("text") or "").casefold() for p in passages):
                symbol_hit += 1
        url = row.get("expected_source_url")
        if url:
            source_total += 1
            if any(p.get("url") == url for p in passages):
                source_hit += 1
    return {
        "evaluation_type": "live_retrieval_only",
        "total_cases": n,
        "request_failures": failures,
        "version_accuracy": round(exact_versions / n, 4),
        "citation_url_validity": (
            round(valid_citations / total_citations, 4) if total_citations else None
        ),
        "citation_count": total_citations,
        "expected_symbol_hit_at_k": (
            round(symbol_hit / symbol_total, 4) if symbol_total else None
        ),
        "expected_symbol_cases": symbol_total,
        "expected_source_hit_at_k": (
            round(source_hit / source_total, 4) if source_total else None
        ),
        "expected_source_cases": source_total,
        "latency_ms": {
            "p50": round(statistics.median(latency_ms), 2) if latency_ms else None,
            "p95": percentile(latency_ms, 0.95),
        },
    }


def http_client(base_url: str, timeout: float):
    if timeout <= 0:
        raise ValueError("timeout must be positive")
    endpoint = base_url.rstrip("/") + "/retrieve"

    def retrieve(request: dict) -> dict:
        body = json.dumps(request).encode("utf-8")
        req = urllib.request.Request(
            endpoint, data=body, method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.load(resp)

    return retrieve


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", default="http://127.0.0.1:8001")
    parser.add_argument("--data", default=str(Path(__file__).parent / "corpus" / "live_smoke_queries.jsonl"))
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--k", type=int, default=5)
    parser.add_argument("--report-out")
    args = parser.parse_args(argv)
    report = evaluate(load_cases(args.data), http_client(args.api, args.timeout), k=args.k)
    data = json.dumps(report, indent=2, sort_keys=True)
    print(data)
    if args.report_out:
        Path(args.report_out).write_text(data + "\n", encoding="utf-8")
    return 1 if report["request_failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
