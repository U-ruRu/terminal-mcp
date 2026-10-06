# Canonical Application Core (Architecture A)

`terminal_mcp.application.TerminalApplication` is the composition-facing application
API. MCP, legacy Actions/Console, persistent operator HTTP, and authenticated Fleet
routes translate transport inputs and errors; they do not own session resolution,
command/message gates, task mutations, or authority routing.

## Dependency direction and composition

Inbound adapters depend on `application`; `application` never imports MCP, FastAPI,
Starlette, or SQL drivers. Existing `TerminalService`, persistent lifecycle/backend,
and Fleet domain services are explicit compatibility implementations. They retain
existing persisted data and established transactions during the incremental migration.
`app.create_app` composes one application object and shares its capabilities with all
routes; `get_application(service)` preserves callers of the previous factory signatures.
An already constructed application may be passed directly to the MCP adapter.

Capabilities are sessions, observations, tasks, messaging, commands, context, health,
operator control, persistent Mesh, Fleet replication/source/projection and Fleet control.
`application/requests.py` and `task_requests.py` own the transport-independent request
models. MCP-specific validation envelopes and wire result encoding stay in `mcp`.
Compatibility exports preserve previous model/projection imports without duplicate logic.

## Actor and admission

`ActorContext` is immutable. Its authenticated principal, credential identity, scopes,
auth generation, node, endpoint role/version and server trace identifier come from
verified transport state and server configuration, never public agent arguments. Provider
metadata may be absent when a legacy credential does not attest it; it is not guessed.
Logical agent, work session, epoch and authority are enriched only from a successful
session-authority resolution. Unverified partial session identities are rejected.

Every canonical capability checks its endpoint policy and binds the explicit actor for
one invocation. The compatibility binding resets in `finally`, including cancellation;
an anonymous actor clears rather than inherits ambient identity. Concurrent requests
cannot overwrite one another's context. Operator capabilities require the operator role;
Mesh capabilities require a verified peer identity and check its correspondence to the
requesting node before authority operations. Forwarded admission is accepted only inside
the authenticated Mesh capability, not as an agent-facing input.

`SessionGate` is the shared application entry to existing local/Mesh authority resolution.
The persistent lifecycle still owns expiry, epoch, scope, authority permits and fencing.
Command coordination uses one application gate: alerts block work except cancellation;
acknowledgement obligations block new run admission, not recovery/cancel semantics.
Existing domain checks remain defense in depth, not a replacement source of identity.

## Transactions and repository ports

`ApplicationUnitOfWorkPort.transaction()` exposes typed context, session, task and command
repositories. The SQLite adapter uses one connection and one `BEGIN IMMEDIATE` transaction
in the authoritative database. The application decides commit boundaries. Context changes
and resolved-actor activity are now committed together through this port; failed activity
writes and cancellation roll back the context change. The legacy context facade shares
the same validation and mutation implementation, preserving its existing admission rules.

Bound repositories cannot be used after their transaction closes or by another async task.
Nested transactions are rejected rather than silently joining or deadlocking. Opening,
rollback and closing retain ownership of their SQLite worker during cancellation. A
cancellation after submission of COMMIT may observe a completed durable transaction;
callers must not blindly retry an ambiguous mutation.

This is NOT a transaction spanning the authoritative DB, output cache, auth DB, Fleet
control DB or a remote node. Independent legacy store methods commit on their own
connections and must never be called inside a new unit-of-work scope. Network calls,
process execution and separate output storage remain outside it. No schema migration or
second authority store is introduced in Architecture A.

## Compatibility and subsequent phases

The seven existing MCP tools retain their exact published schemas, descriptions and
annotations. A fixture in `tests/fixtures/architecture_a_mcp_contract.json` checks their
canonical hashes. Existing HTTP paths and bodies remain supported. The in-process Linux
runtime and four numbered queues are unchanged; Architecture B introduces ExecutionPort,
C introduces privileged local IPC, and D owns deployment migration. Public compact output,
input limits, managed identity and role endpoints are separate tasks atop this boundary.

Boundary tests exercise concurrent actor isolation, cancellation cleanup, capability
rejection, MCP discovery parity, HTTP/Fleet adapter parity, real repository transactions,
and rollback through actual application entry points. Full-suite results are recorded
against immutable Git candidates rather than a moving implementation worktree.
