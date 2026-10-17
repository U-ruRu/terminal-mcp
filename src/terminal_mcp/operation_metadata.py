"""Public metadata describes side effects independently of authorization."""

READ_ONLY_ACTIONS = frozenset(
    {
        "listActiveAgents",
        "listTasks",
        "readTerminal",
        "getTerminalHealth",
        "getConsoleSnapshot",
        "getManagedFleetControl",
        "listAccessMeshSlots",
        "getAccessMeshSlot",
        "getAccessMeshDefaults",
    }
)
MUTATING_ACTIONS = frozenset(
    {
        "mutateAccessMesh",
        "manageInstanceContext",
        "runCommand",
        "recoveryCommand",
        "cancelCommand",
        "adoptManagedFleetControl",
        "deleteManagedMesh",
        "renameManagedMesh",
        "upsertManagedFleetNode",
        "detachManagedFleetNode",
        "moveManagedFleetNode",
        "updateManagedAccessPolicy",
        "resetManagedAccessPolicy",
        "rotateManagedFleetTrust",
        "reconcileManagedFleetControl",
        "updatePersistentPolicy",
    }
)


def action_is_consequential(operation_id: str) -> bool:
    # Unclassified future operations fail safe until explicitly reviewed.
    return operation_id not in READ_ONLY_ACTIONS


def effects(*, read=False, destructive=False, idempotent=False, external=False):
    return {
        "readOnlyHint": read,
        "destructiveHint": destructive,
        "idempotentHint": read or idempotent,
        "openWorldHint": external,
        "x-openai-isConsequential": not read,
    }


READ_EFFECTS = effects(read=True)
APPEND_EFFECTS = effects()
REPLACE_EFFECTS = effects(destructive=True)
DELETE_EFFECTS = effects(destructive=True, idempotent=True)
SHELL_EFFECTS = effects(destructive=True, external=True)
TASK_ACTION_EFFECTS = {
    "create": APPEND_EFFECTS,
    "update": REPLACE_EFFECTS,
    "claim": effects(idempotent=True),
    "release": DELETE_EFFECTS,
    "checkpoint": REPLACE_EFFECTS,
    "comment": APPEND_EFFECTS,
    "state": REPLACE_EFFECTS,
    "done": REPLACE_EFFECTS,
    "archive": DELETE_EFFECTS,
    "relate": effects(idempotent=True),
    "unrelate": DELETE_EFFECTS,
    "review": APPEND_EFFECTS,
}
MESSAGE_ACTION_EFFECTS = {
    "read": READ_EFFECTS,
    "history": READ_EFFECTS,
    "recipients": READ_EFFECTS,
    "send": APPEND_EFFECTS,
    "ack": DELETE_EFFECTS,
    "reply": REPLACE_EFFECTS,
}


def tool_action_effects(role, name):
    if role == "access":
        if name == "get_events":
            # Reading automatically marks returned events as consumed.
            return {"consume": effects()}
        return {"start": APPEND_EFFECTS, "end": DELETE_EFFECTS}
    if name == "session":
        return {"attach": effects(idempotent=True), "intent": effects()}
    if name == "message":
        return MESSAGE_ACTION_EFFECTS
    if name == "task_claim":
        return {key: TASK_ACTION_EFFECTS[key] for key in ("claim", "release")}
    if name == "task_manage":
        return {
            key: value
            for key, value in TASK_ACTION_EFFECTS.items()
            if key not in {"claim", "release"}
        }
    if name == "task_comment":
        return {"comment": APPEND_EFFECTS, "checkpoint": REPLACE_EFFECTS}
    if name == "task_state":
        return {"state": TASK_ACTION_EFFECTS["state"]}
    if name in {"command_run", "command_recovery"}:
        return {name.removeprefix("command_"): SHELL_EFFECTS}
    if name == "command_cancel":
        return {"cancel": DELETE_EFFECTS}
    if name == "command_read":
        return {"output": READ_EFFECTS, "journal": READ_EFFECTS}
    return {"read": READ_EFFECTS}


def install_action_metadata(mcp, role):
    from mcp.types import ToolAnnotations

    for tool in mcp._tool_manager.list_tools():
        matrix = tool_action_effects(role, tool.name)
        tool.parameters["x-terminal-mcp-action-matrix"] = matrix
        tool.annotations = ToolAnnotations(
            readOnlyHint=all(row["readOnlyHint"] for row in matrix.values()),
            destructiveHint=any(row["destructiveHint"] for row in matrix.values()),
            idempotentHint=all(row["idempotentHint"] for row in matrix.values()),
            openWorldHint=any(row["openWorldHint"] for row in matrix.values()),
        )


def openapi_effects(operation_id):
    if operation_id in READ_ONLY_ACTIONS:
        return READ_EFFECTS
    if operation_id in {"runCommand", "recoveryCommand", "runPersistentCommand"}:
        return SHELL_EFFECTS
    if operation_id.startswith(
        ("delete", "cancel", "end", "finish", "suspend", "release", "detach")
    ):
        return DELETE_EFFECTS
    if operation_id in {"mutateAccessMesh"}:
        return DELETE_EFFECTS
    if operation_id.startswith(("create", "start")):
        return APPEND_EFFECTS
    return REPLACE_EFFECTS


def openapi_action_matrix(operation_id):
    if operation_id in {"mutateTask", "mutatePersistentTask"}:
        return TASK_ACTION_EFFECTS
    if operation_id == "messageAgent":
        return MESSAGE_ACTION_EFFECTS
    if operation_id == "manageInstanceContext":
        return {
            "list": READ_EFFECTS,
            "create": APPEND_EFFECTS,
            "update": REPLACE_EFFECTS,
            "delete": DELETE_EFFECTS,
        }
    if operation_id == "mutateAccessMesh":
        return {
            name: effects(
                destructive=name
                in {"delete", "end", "suspend", "rotate", "policy", "deadline", "defaults"},
                idempotent=True,
            )
            for name in (
                "create",
                "defaults",
                "policy",
                "suspend",
                "resume",
                "delete",
                "rotate",
                "deadline",
                "end",
            )
        }
    return {operation_id: openapi_effects(operation_id)}
