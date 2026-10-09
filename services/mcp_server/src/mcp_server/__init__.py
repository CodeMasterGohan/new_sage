"""New_sage MCP server package (spec §10).

Local-first Context7 alternative: version-aware docs retrieval over MCP.
No authentication of any kind (spec §13) — single user, binds localhost /
Tailscale at deploy time.
"""

__all__ = ["models", "resolver", "clients", "tools", "server", "stdio_adapter"]
