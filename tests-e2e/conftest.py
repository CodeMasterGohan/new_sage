"""Make the build tree importable when pytest runs from ~/workspace/new_sage/build."""

import os
import sys

BUILD_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if BUILD_ROOT not in sys.path:
    sys.path.insert(0, BUILD_ROOT)

MCP_SRC = os.path.join(BUILD_ROOT, "services", "mcp_server", "src")
if MCP_SRC not in sys.path:
    sys.path.insert(0, MCP_SRC)
