# Canonical application core

`terminal_mcp.application.TerminalApplication` is the composition-facing API.
MCP, native operator HTTP, Console and authenticated Mesh adapters translate
transport inputs and errors. Application capabilities own session resolution,
command and message admission, task operations and current Mesh control.

## Composition and actors

`app.create_app` composes one application with Access, Execution and Coordination
on every node. Topology does not select another runtime or tool catalog. The
application uses transport-independent request models; MCP envelopes and public
result encoding stay in the MCP adapter.

`ActorContext` holds verified principal, credential, scopes, node, endpoint role
and server trace information. Public agent arguments cannot supply that identity.
Logical-agent and work-session values come from native Access resolution. A
request's context is isolated from concurrent requests and cleared on exit,
including cancellation.

The common SessionGate checks current native slot and work-session state. It
rejects unresolved registry migration, a topology fence, old-scope bindings,
expiry and revoked authority. Intent, message obligations and operation-specific
permissions retain their own checks after identity resolution. Historical
session rows support attribution and reads; they do not authorize a second
execution path.

## Operator and peer boundaries

The paired application controls membership and primary selection. Topology
mutations and enrollment require an active paired OAuth device with the required
scopes. General static bearer access or a permissive auth mode does not confer
that pairing authority. A verified member may carry bounded internal control and
registry requests under the assigned topology. Old Mesh ingress authority cannot
enroll a node into another namespace.

Access through any member forwards authoritative registry operations to the
primary. Namespace, topology revision, trust and durable transition state bind
those operations. Source-registry handoff preserves every row before an outgoing
primary clears its local namespace.

## Transactions and recovery

Repository units of work use one SQLite connection and one transaction for their
local domain mutations. Context changes and successful activity attribution
commit together. Bound repositories cannot escape their transaction or be used
by a different async task. Cancellation during COMMIT may leave a committed
operation; stable operation IDs and reads resolve an ambiguous outcome.

There is no implicit transaction spanning runtime, auth, output, control and
remote databases. Topology uses prepare/completion receipts, local namespace
journals and admission fences to recover across those boundaries. Derived Console
rows are cleared atomically with their namespace metadata and rebuilt on demand;
old in-flight refreshes cannot repopulate the cache after Move.

## Verification

Current catalogs and request/output models own the public contract. Component
tests cover actor isolation, strict admission and SQLite transactions. Minimal
multi-router tests cover shared issuance, namespace isolation, registry handoff,
Move and interruption recovery. Full acceptance is tied to an immutable source
SHA, following [the test workflow](../testing.md).
