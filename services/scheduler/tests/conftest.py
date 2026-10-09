"""Make the build tree importable when pytest runs from any directory."""

import os
import sys

BUILD_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..")
)
if BUILD_ROOT not in sys.path:
    sys.path.insert(0, BUILD_ROOT)

SERVICES_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..")
)
if SERVICES_DIR not in sys.path:
    sys.path.insert(0, SERVICES_DIR)
