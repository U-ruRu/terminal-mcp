# Terminal MCP architecture

The [Mesh product contract](mesh-product-contract.md) governs topology, registration and session behavior. This document describes the implementation boundaries; deployment evidence belongs to a separately approved release.

## Public boundary

Every installation exposes Access, Execution and Coordination. Standalone has no Mesh membership and does not manufacture a one-node Mesh. A one-node Mesh is created explicitly by the paired application. The same tools, universal object schemas, strict validation, errors and transport authorization apply in every topology.

| Surface | Endpoint | Session actions |
| --- | --- | --- |
| Access | `/terminal-mcp/access/v1/mcp` | `start`, `end` |
| Execution | `/terminal-mcp/executor/v1/mcp` | `attach`, `intent` |
| Coordination | `/terminal-mcp/coordinator/v1/mcp` | `attach`, `intent` |

Access catalog: `session`, `get_events`.

Executor catalog: `session`, `task_list`, `command_run`, `command_read`, `command_cancel`, `command_recovery`, `task_claim`, `task_state`, `task_comment`, `message`.

`command_read` without `cmd_hash` reads the local command journal newest-first.
Its opaque cursor continues strictly before the oldest sequence returned on that page;
new commands do not shift older pages. Restart journal reading without a cursor after
an `invalid_cursor` from the previous ascending-journal contract. With `cmd_hash`,
command output and its forward line cursor retain their existing contract.

Coordinator catalog: `session`, `task_get`, `task_list`, `task_manage`, `task_graph`, `agent_observe`, `message`, `health`.

Operator slot and topology operations are authenticated HTTP use cases. They do not add shell execution to the application. Agent roles have no alternate start/end/interrupt implementation or alternate discovery catalog.

## Registry ownership and authorization

Each registration scope has a durable namespace and exactly one registry authority. In Standalone that authority is the local node. In Mesh it is the app-appointed primary. Any member ACS can forward an authenticated registration to the primary; a follower does not issue a conflicting local registration when the primary is unavailable.

The registry holds slot records, number claims, lifecycle events, defaults, individual overrides and issuance receipts. Historical issuer identity is provenance, including after that issuer leaves or a new primary is appointed. It does not grant a former issuer continuing mutation authority. Authenticated registry transfer preserves the complete authoritative data and rewraps protected receipts for the new primary.

Four-digit session numbers resolve within the current registration scope. A role binding includes trusted connector/provider/principal context. New attach verifies the current primary's grant; ordinary admitted operations use local state and local deadlines. Namespace checks extend into transaction commit so a request authenticated before Move cannot write into the new scope.

Node identity, app pairing, slot identity and local execution ownership are distinct. `node-identity.json` persists independent node credentials beside the main database. Moving a node keeps that identity and its app pairing, and removes authority obtained solely through the previous Mesh.

## Local execution and lifecycle

A slot has immutable kind `temporary` or `persistent`. A local work session records its identity, epoch, start, deadline and execution ownership. Attach and intent do not extend the deadline. Policy controls duration, cooldown, rearm, warning/draining and claim release. Pending execution cleanup fences subsequent writes until old work has been drained.

Temporary ownership is released at end or expiry. Persistent ownership follows `release_on_end`. Cleanup acts on the exact old claim/session so it cannot remove a successor's ownership. Policy changes for future sessions are separate from explicit current-deadline mutations.

The session gate obtains identity from native Access and performs local lifecycle, intent and operation checks. Read-only operations permitted before attach retain transport authentication. Command queues, processes and retained output remain local to the node handling the call.

## Topology and recovery

The paired application owns Create, Join, Detach, Move and primary selection. The server verifies authorization, revisions and the consistency of the proposed operation. Slots, active sessions, queued or running commands and cleanup do not block membership changes on the moving node, source Mesh or destination Mesh. Local work may run until its existing deadline or stop when its local session is invalidated; no agent-stop handshake is required. Historical namespace labels, prior controllers and manual peer approval lists are not admission rules.

Membership changes use a durable control journal and an ACS admission fence. The commit-time namespace check prevents a request authorized in the previous namespace from writing after the transition. On C's departure, only C loses its previous slots, replicas and bindings. The source registry remains complete and unchanged.

Primary handoff fences the affected participants, transfers the full registry, checks its digest and publishes a signed authority transition. Replays are resolved by operation identity before revision comparison. An incomplete handoff remains fenced and resumable. Startup recovery reconciles control and ACS before policy application and admission.

Move is a recoverable detach-and-join flow. The application stores public destination intent, confirms autonomy and obtains fresh enrollment credentials before Join. An unavailable destination can leave the node safely autonomous. Recovery never imports the source Mesh's slots into the destination.

## Policy

Mesh common defaults come from the primary's control policy and are applied by all members. A slot can override that policy or return to inheritance. There is no independent per-member override of Mesh common defaults. Standalone uses the same inheritance model with local defaults.

Policy revisions and slot revisions guard concurrent updates. Future policy changes preserve the current session schedule; shortening or extending an active deadline is explicit. Moving into a Mesh replaces the previous namespace's defaults with the destination policy.

## Application and storage layers

| Layer | Responsibility |
| --- | --- |
| `mcp/`, `http/` | Wire validation, authenticated actor construction, bounded responses |
| `application/` | Transport-independent use cases and admission |
| `core/` | Slot, task, ownership and execution rules |
| `storage/` | SQLite transactions, durable fences, receipts and migration markers |
| `fleet/` | Internal authenticated Mesh transport and control coordination |
| `terminal/` | ExecutionPort, local scheduler and process control |

| Store | Durable contents |
| --- | --- |
| Main runtime database | Tasks, history, commands, local work sessions and native Access registry |
| Auth database | Principals, OAuth clients/credentials and pairing |
| Control database | Mesh topology, common policy, peer trust and transition journals |
| Source/projection databases | Namespaced read projections and delivery progress |
| Output cache | Bounded retained process output |

Transactions capture task receipts with the mutation. Claim/release affects ownership; explicit state, checkpoint and result survive cleanup. Automatic task replay keys include normalized payload and trusted identity/session context. Shell launch requires its own durable reservation and outcome reconciliation.

## Messages and events

Messages are local to the working connector's node. Recipient discovery uses active local native attachments. Message acceptance and obligations commit together. Notify read has surfacing effects; ack requires acknowledgement and alert requires reply. Replies and obligation release are atomic.

Message history does not cross registration namespaces. On departure, retained application content is separated from active recipient/routing state. Old transport credentials, queues and bindings cannot deliver it into the new Mesh. Access lifecycle events and grant replication have separate authorization and retention rules.

## Installation, migration and background work

Clean install starts the universal runtime in Standalone. Explicit clean deploy first validates and stages the candidate, then resets the previous installation's managed data, secrets, registrations, pairing, membership and backups. It does not restore old state automatically.

Hot upgrade preserves applicable data through versioned, one-time migrations. Native slots and current deadlines are retained. Convertible old records become native records without an executable old-session fallback. Unconvertible active work blocks activation. A completed migration receipt identifies the narrowly permitted transformations; unrelated rows and protected auth remain subject to preservation checks.

An autonomous node has no remote ACS delivery worker or periodic topology polling without pending recovery. Mesh work is started from assigned membership, uses bounded pages and retries, and stops when membership disappears. Local command execution and local lifecycle maintenance remain available.

Historical design and acceptance records describe their original source versions. They do not override the current product contract or authorize a deployment.
