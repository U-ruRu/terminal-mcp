# Android native Access slots continuation — 2026-10-12

Base: canonical `26ae8395bc6909de9313291272ff153483e8818e` (0.25.1), confirmed by dot via Sierra-4.
Task branch only. No deployment, canonical update, production access or policy changes.

## Operator contract matrix

| User action | Existing endpoint | Authorization | State transition | Confirmation |
| --- | --- | --- | --- | --- |
| Read native slots | GET `/actions/access/slots`, GET `/actions/access/slots/{slot_id}` | operator, `terminal:read` | none; complete paginated list, explicit errors | strict projection with issuer/slot identity and independent sample timestamp |
| Create native slot | POST `/actions/access/mutate`, `action=create` | operator, `terminal:execute` | one issuer slot and durable request receipt | reread selected slot; code is separate from slot ID |
| Rotate code | same, `action=rotate` | operator, `terminal:execute`; expected slot revision | code generation increments; durable idempotency key | reread generation, public MCP attach verified separately |
| Mesh policy | POST `/actions/fleet/control/policy` | existing operator scope and existing Mesh control authority | original Fleet policy store, forwarding and replication, then native inheritance projection | reread existing control revision and native defaults |
| Server defaults | POST `/actions/access/mutate`, `action=defaults` | operator, `terminal:execute`; expected defaults revision | explicit local server override | reread defaults; inherited slots updated atomically |
| Slot override | same, `action=policy`, `policy={...}` | operator, `terminal:execute`; expected slot revision | only selected slot switches to override | reread selected slot |
| Reset server/slot override | same `defaults`/`policy`, `inherit_policy=true`; no policy object | same scope/CAS | explicit return to parent inheritance | reread effective and inherited policy |
| Change current deadline | same, `action=deadline` | same operator scope/CAS | explicit separate current-window operation | reread actual window |

Mesh uses only existing fields: duration, warning-after, alert-after, rearm-after and legacy admission.
Native translation: warning-before = duration - warning-after; draining-before = duration - alert-after;
cooldown = rearm-after; rearm_enabled = true; release_on_end remains null unless locally overridden.
There is no second Mesh policy database or independent control node.

## Persistence and compatibility

A new slot without an explicit policy inherits local defaults. Existing stored slots and existing stored
server defaults migrate as explicit overrides, preserving their behavior until an explicit reset.
A parent update changes effective policy and revisions of inherited slots in the same native SQLite
transaction as the defaults write and durable receipt. Explicit overrides are left unchanged.
The current window stores its old policy, original deadline and cooldown. A future policy does not
extend the current session. An explicit deadline mutation is separate.

The native projection declares `inheritance_supported`, `mesh_policy_supported`, effective/future
policy, inherited policy, override, source, and current-window policy. Missing new fields on a server
claiming the capability fail strict client decoding. Old servers remain readable but cannot promise
next-session-only policy editing. Full functionality needs this backend candidate on participating
issuers/replicas; 0.25.1 is the compatibility baseline, not a claim that its old backend gains new APIs
from installing the APK alone. New native event/snapshot metadata requires compatible replicas.

Replica Mesh defaults are explicitly `last_known`; only the local authority claims `authoritative`.
An unavailable authority does not create a local substitute policy. A lost mutation response is
`control_mutation_unconfirmed`, distinct from a connection failure.

## Verification artifacts

Artifacts: `/workspace/terminal-mcp-artifacts/android-slots-victor4-20261012/`.
Original worktree HEAD/status/diff and untracked sources are in `recovery/`.
The real HTTP window defect has RED/GREEN receipts. UI re-pairing has an independent B implementation
and A rerun. Generated TypeScript fixtures contain only isolated HTTP GET projections, never code
receipts. Public MCP create/rotate/deadline/end tests are additionally required before final acceptance.
Exact final counts and candidate SHA belong to HANDOFF.md, not this evolving implementation note.


## Public-path bridge and management protocol 2

`management_contract_version: 2` on native defaults and slot GET projections is the explicit
mutation capability. A v1/unknown server remains read-only in the new APK: its code-generation
receipts, rotated public connection and current-window mutation paths are not safe to assume.
This is intentionally independent of the server release label, which may describe an unmerged
candidate. The client also requires fresh context-scoped reads, scopes, strict metadata and CAS.

Code rotation reserves a globally confirmed free code through the existing peer negotiation and
replicates the existing hashed-code event. It never changes the immutable numeric recipient claim,
logical agent, subscriptions, already-authorized bindings or current deadline. Public attach checks
the rotated issuer-authenticated tag and existing claim, then uses the ordinary admission path.
Ambiguous or partially known conflicting numeric claims are denied. Existing subscription holds
continue to reject rotation explicitly; this guard is not removed by the APK/API repair.

The operator reads the same number-backed working window as public MCP, and captures that actual
window for next-policy edits. Explicit end records the existing replicated number-end event.
Explicit deadline updates support extension of the merged maximum window; shortening is rejected
rather than acknowledged without actually changing admission. Expired windows stay expired until
an existing authorized explicit start. Cooldown metadata is not an automatic new authorization.


A slot reset additionally requires `expected_parent_revision`, taken from the immediate server
parent shown by the slot GET. An explicit slot override does not inherit parent revision changes;
therefore checking only the slot revision is insufficient. A stale parent reset returns the same
`revision_conflict` without changing the override. Defaults resets already use the defaults CAS.
