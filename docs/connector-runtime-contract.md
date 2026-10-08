# Connector runtime contract

Executor and Coordinator publish argument names, defaults and compact usage hints. Their input schemas accept argument values and extra fields. Runtime models validate types, bounds, supported fields and action prerequisites before invoking application mutations.

Handled application failures use MCP `isError: false` and a structured result:

```json
{"ok":false,"error":{"code":"input_validation_failed","message":"Correct the indicated request field.","details":{"validation_errors":[{"error_class":"missing","path":"command","description":"Provide this required field."}]},"outcome":"not_committed","retry":"repair","reason":"missing_required","path":"command"}}
```

`structuredContent` contains the object. The text fallback contains the same object encoded once as JSON. Output planning schemas are flat top-level objects covering success fields, `ok` and `error`. Nested records are coarse; strict success/error models still validate runtime responses. The published planning projection has no schema references or composition; each tool has a tested byte baseline. The legacy adapter retains its flat error envelope.

`error.code`, `error.outcome` and `error.retry` describe the result and recovery. Session lifecycle failures select `start_session`; optimistic concurrency failures expose `revision_conflict` and `error.details.current_revision`. Uncertain failures select reconciliation. Validation issues contain bounded schema-owned paths and corrective descriptions. Private exception diagnostics remain in service logs.

Task record revisions track field, checkpoint and output-state changes. Claim, release, review and relation operations enforce a supplied `expected_revision` inside their write transaction. Their side-stream records retain independent event identities. Comments append to history and accept the comment text without a revision argument. A `review_of` relation may update the associated candidate and task revision.

Task checkpoint, result, history payload, review evidence and warning extensions contain native JSON values. Graph nodes share `namespace`, `task_id` and nullable `state`.

Infrastructure health is available before session creation. Its `ok` field confirms collection of the diagnostic result; `healthy`, `status` and component states describe infrastructure health. Extended role health includes bounded provider field names, identity fingerprints and resolution status. Successful resolution supplies the public agent name. Host paths, privilege details and custom health-command output remain outside the role health projection.

Provider bindings use the provider, subject and conversation identity. Identical evidence resolves consistently across endpoint roles and nodes. Distinct provider evidence remains distinct; verified authorization and ownership checks apply independently. Role session-start receipts include the same safe provider fingerprints. Extended health fingerprints support comparison through connected clients without publishing raw provider identifiers.

An active WorkSession retains the role, contract version and authenticated principal that opened it. Another role uses its own provider identity or starts after the original session ends. `session_contract_conflict` describes a role/version mismatch; `session_principal_mismatch` describes another connection's session. Claims record LogicalAgent attribution and the ownership lifetime of the slot/session. Coordinator mutations respect another agent's live claim. Public failure receipts include safe provider fingerprints when request metadata is present.

Explicit role handoff uses `session.end` on the original endpoint followed by `session.start` on the next endpoint with the same provider evidence and authorized principal. The LogicalAgent survives the handoff. Bootstrap-managed and temporary-session claims are released when the original session ends; the new session claims available work again. Explicitly provisioned persistent slots follow their durable ownership policy. The next WorkSession records the new role within the current work-window budget.

Task mutations and command launches derive replay keys from the server-observed MCP request ID and WorkSession. `run` and `recovery` return their stored command receipt on replay after rechecking the session and execution fence. A payload mismatch reports `idempotency_conflict`. An uncertain launch retains its reservation and reports an in-progress/reconciliation state on retry, preserving at-most-once launch behavior. A confirmed pre-commit rejection releases its reservation.

Observed stateless ChatGPT connector calls reuse JSON-RPC ID `0`. Task replay keys for this client include the normalized mutation fingerprint, so distinct mutations remain distinct and an exact task retry reuses its receipt. Command calls with this non-unique ID execute as fresh requests; command replay protection applies to non-zero per-operation request IDs. A constant transport ID alone cannot distinguish an intentional repeated command from a delivery retry. The client supplies no additional public tool argument.

## Fleet session authority and claim leases (runtime schema 21)

A provider-bound identity is resolved once and routed to its home authority for
managed session start, admission, end and interrupt. Authenticated Fleet forwarding
preserves the original OAuth principal, provider evidence and endpoint role. The
home authority rechecks the binding. A conflicting active principal or role keeps
its typed runtime error. Execution on another node uses a short-lived authority
permit; the execution node stores receipts and audit records for the global
identity without creating a local authority slot.

Claims acquired by automatic managed sessions and temporary compatibility slots
carry an exact `(LogicalAgent, WorkSession, session_epoch)` lease. A leased claim
moves a ready task to `in_progress` atomically. End, interrupt and hard expiry first
fence command execution, then release that session's claims. Releasing the last
leased owner returns a task to `ready` only when the lease automatically advanced
its activity state. Explicit state assignments, including a same-state manual
override, clear that provenance. Checkpoint, result, history and other owners survive. Explicit durable ownership
retains its separately controlled workflow state and lifetime.

Remote revocation writes a durable session fence even when no claim exists yet.
This serializes against late claim admission. Pending execution blockers retain
ownership until drainage succeeds. The Fleet recovery sweep also considers
expired task-only leases and persisted revoke fences, so restart and a missed peer
notification cannot leave the claim permanently assigned. A cleanup retry for an
old epoch leaves successor-session claims intact. Health reports stale leases
while recovery is pending.

Schema 21 retains pending and completed execution receipts, audit records, indexes
and audit sequence numbers while removing their inappropriate local-slot foreign
key. Authority-owned tables keep their foreign keys. Existing managed claims are
leased only when exact claim-event and WorkSession evidence exists; completed old
sessions then release those claims transactionally. Upgrade from schema 20 needs
a runtime database backup. A binary-only rollback to schema 20 is blocked after
the durable schema upgrade; recovery uses a compatible binary or the coordinated
database restore procedure.
