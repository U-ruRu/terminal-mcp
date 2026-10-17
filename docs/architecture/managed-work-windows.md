# Native work sessions and slot policy

All installations use the Access registration and work-session implementation.
Standalone, a one-member Mesh and a Mesh with several members expose the same
Access start/end and Execution/Coordination attach/intent operations. The
[normative Mesh contract](../mesh-product-contract.md) defines the boundaries.

## Registration and local execution

The current namespace owns a slot and its Access cycle. The Mesh primary owns the
complete shared registry; every member Access Connector can forward issuance and
end to that authority. Standalone uses the same registry locally. A valid number
can attach both agent roles on any current member of its namespace.

Work executes on the selected server. The native logical-agent identity, local
work-session ID, epoch, slot binding, captured policy and exact deadline determine
admission. A repeated attach retains its existing binding and intent. Intent
refresh neither renews an Access cycle nor extends the deadline. Commands, queues,
processes and results remain local resources.

Public tools resolve the binding against the current registry scope. An incomplete
registry upgrade or namespace transition denies work, including requests carrying
a binding saved before restart. A request admitted in an earlier scope must still
pass the namespace fence at the SQLite commit boundary.

## Policy and lifecycle

The Mesh primary provides one inherited policy. A slot may have an individual
override; a member cannot replace the Mesh parent with a local parent. Standalone
uses the same policy and override model. Policy writes use revision checks and
idempotency keys. Operator receipts describe whether a change applies to the
current cycle or a later cycle; clients re-read committed state after an ambiguous
transport result.

Deadline, draining, cleanup and cooldown are durable lifecycle state. Reads and
safe cleanup retain their explicit lifecycle rules. An expired or revoked cycle
cannot gain authority by repeating attach or refreshing intent. End revokes work
authority before cancellation; unsuccessful process cleanup remains pending and
is recovered without issuing a replacement cycle prematurely.

## Namespace changes

Join, Detach and Move fence admission before changing the local namespace. They
check authoritative source activity and unfinished local execution. The departing
node clears old slot replicas, bindings and authority. The complete source Mesh
registry remains unchanged. Primary departure first transfers the complete
registry to the app-appointed successor. Main-database task and command history
is retained; the derived Console cache is rebuilt for the new membership.

## One-time upgrade

The unified runtime migrator preserves native identities and exact existing
session timestamps and deadlines. It converts supported historical registrations
once, records a durable migration receipt and retires old executable session
state. It does not silently extend a work cycle or rerun historical import after
a later Move. Unresolved active work or ambiguous topology stops migration.

Historical rows retained for attribution are read-only data. The former managed
and persistent runtime adapters, provider-to-slot registration path, legacy role
start/end/interrupt, and alternative catalogs are removed. See
[upgrade and clean installation](../access-mesh-deployment.md) for the supported
installation boundary and preservation checks.
