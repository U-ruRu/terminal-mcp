"""Shared operator authorization check for paired application actions."""

from terminal_mcp.application.actor import ActorContext


class OperatorApplication:
    @staticmethod
    def _require_operator(actor: ActorContext) -> None:
        if actor.endpoint_role != "operator":
            raise PermissionError("operator_role_required")
