"""stdio adapter tests: no identity is required (no auth on this server);
the adapter only needs the retrieval URL and the resolver table."""

from __future__ import annotations

from mcp_server.stdio_adapter import parse_args


def test_parse_defaults():
    args = parse_args([])
    assert args.retrieval_url == "http://retrieval:8001"
    assert args.resolver_table.endswith("libraries_seed.json")
    assert args.dev_fake_clients is False


def test_parse_overrides():
    args = parse_args(
        [
            "--retrieval-url",
            "http://localhost:8001",
            "--resolver-table",
            "/tmp/custom.json",
            "--dev-fake-clients",
        ]
    )
    assert args.retrieval_url == "http://localhost:8001"
    assert args.resolver_table == "/tmp/custom.json"
    assert args.dev_fake_clients is True


def test_env_var_overrides(monkeypatch):
    monkeypatch.setenv("RETRIEVAL_URL", "http://env:9000")
    args = parse_args([])
    assert args.retrieval_url == "http://env:9000"
