"""Reuse repository HTTP/MCP acceptance helpers on the Windows host."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tests"))
