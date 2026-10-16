"""Platform defaults shared by settings and local credential readers."""

import os
import sys
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"
PROGRAM_DATA = Path(os.environ.get("ProgramData", "C:/ProgramData")) / "TerminalMCP"
CONFIG_DIR = PROGRAM_DATA / "config" if IS_WINDOWS else Path("/etc/terminal-mcp")
ENV_FILE = CONFIG_DIR / "terminal-mcp.env"
CREDENTIAL_DIR = CONFIG_DIR / "credentials"
