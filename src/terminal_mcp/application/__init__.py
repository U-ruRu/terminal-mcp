"""Canonical application boundary; transports depend on this package, not vice versa."""

from terminal_mcp.application.actor import ActorContext as ActorContext
from terminal_mcp.application.api import TerminalApplication as TerminalApplication
from terminal_mcp.application.api import get_application as get_application
