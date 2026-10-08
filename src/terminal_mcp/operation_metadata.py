"""Public metadata describes side effects independently of authorization."""

READ_ONLY_ACTIONS = frozenset(
    {
        "listActiveAgents",
        "listTasks",
        "readTerminal",
        "getTerminalHealth",
        "getConsoleSnapshot",
        "getManagedFleetControl",
        "listPersistentSlots",
        "getPersistentSlot",
        "getManagedWorkSessionStatus",
    }
)
MUTATING_ACTIONS = frozenset(
    {
        "startAgentSession",
        "coordinateAgent",
        "messageAgent",
        "finishAgentSession",
        "manageInstanceContext",
        "mutateTask",
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
        "createPersistentSlot",
        "renamePersistentSlot",
        "rotatePersistentSlotSelector",
        "migratePersistentSlotAccess",
        "rotatePersistentSlotAccessCode",
        "playPersistentSlot",
        "suspendPersistentSlot",
        "deletePersistentSlot",
        "startPersistentSession",
        "endPersistentSession",
        "runPersistentCommand",
        "cancelPersistentCommand",
        "mutatePersistentTask",
        "releasePersistentClaim",
        "reassignPersistentClaim",
        "updateManagedSlotSessionPolicy",
        "updateManagedWorkWindow",
        "endManagedWorkSession",
    }
)


def action_is_consequential(operation_id: str) -> bool:
    # Unclassified future operations fail safe until explicitly reviewed.
    return operation_id not in READ_ONLY_ACTIONS
