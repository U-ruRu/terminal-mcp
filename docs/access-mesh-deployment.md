# Unified Mesh installation and upgrade

The normative architecture is [mesh-product-contract.md](mesh-product-contract.md).
Every installation provides Access, Execution and Coordination. A clean node is
Standalone: it has a local registration namespace and no Mesh membership.
Pairing lets the user application create a Mesh, join an existing Mesh, detach,
move a node, or appoint a different primary.

## Source acceptance and deployment are separate

First integrate and validate one immutable source commit. The supported source
gate is the complete Python test workflow, the complete Console test suite,
TypeScript/build, lint and generated-contract checks. Record the exact source
SHA and its full test report. A passed test fixture is evidence for its isolated
scenario; it does not assert that a live server has been upgraded.

Deployment is a separately authorized operation. The release workflow in
[deployment-release-workflow.md](deployment-release-workflow.md) builds an exact
commit, verifies the acceptance receipt, inventories the existing installation,
takes consistent offline snapshots and journals migration/activation. A failed
activation does not authorize restoring older credentials or security state.
Follow the recorded recovery phase and durable schema constraints.

## First installation and explicit clean deploy

The Linux installer distinguishes an empty installation from an explicit reset:

    sudo ./deploy/install.sh install
    sudo ./deploy/install.sh clean-deploy
    sudo ./deploy/install.sh doctor

Use install only for an empty managed state. It refuses retained installation
state instead of treating it as a fresh deployment. Provide the administrative
password through the installer environment, with normal secret handling.

Clean-deploy is an explicit destructive reset of the selected installation. It
first validates the managed path inventory and stages the replacement binary.
Only then does it stop the managed services and remove the previous runtime,
auth, slot and topology databases; pairing and connector bindings; local node
credentials; managed configuration and VPN sidecars; output/cache state; and
backups of that installation. It preserves unrelated paths and the separately
staged replacement. It has no automatic restore path to the discarded state.
External configured database files and their managed sidecars are included in
the inventory; arbitrary directory deletion is rejected.

The Windows installer uses the explicit CleanInstall option for the same reset
boundary. A normal update preserves current state. Native Windows process,
service and ACL acceptance must run on Windows.

The new process generates a durable independent node identity and local proof
material when they are absent. It exposes all three MCP endpoints immediately.
It does not create a Mesh, discover a former Mesh from backup, or require an
enable-Mesh switch. The user pairs the application again after a clean deploy.

## Hot update

A hot update preserves the installation identity, pairing, application history
and valid current registrations. It runs the one-time schema/runtime migrations
before admitting agent writes. Existing configuration does not select an older
agent runtime: former mode flags have no implementation behind them.

The migration converts old temporary-slot labels and encrypted issuance receipts
to the current representation. It materializes former local agent registrations
as native slots while preserving identity, session epoch, start time, deadline
and applicable policy. Old agent-session permissions are retired in the same
transaction and cannot be imported again after a future Mesh move. Active work
that cannot be attributed safely, an incomplete old session transition, or an
ambiguous old topology blocks activation with a specific error.

For an existing multi-issuer Mesh, the primary obtains bounded immutable exports
from every required old member before publishing the authoritative union.
Registration history, inactive slots and historical issuer identity survive.
An offline required member or conflicting active number blocks completion;
the primary does not silently choose one registration or issue an alternative
local registry. The migration resumes from durable receipts after interruption.

Activation requires /health/ready to return ready=true for the expected release.
A process with a pending registry union or topology fence returns HTTP 503 there;
/health/live remains a liveness check, including for pre-upgrade versions.

The offline release verifier compares all protected old application columns and
rows with the resulting stores. Only named, independently checked conversions
are permitted. Encryption is checked by authenticated plaintext comparison,
so re-encryption does not hide changed code, policy, identity or deadline.
Unrelated task, command, pairing and configuration changes fail preservation.

The update process must not be combined with a membership change. After the
migration has completed, ordinary app-authorized topology operations use their
own fences and journals. A retained local pairing is sufficient to request
those operations; manual trust lists or former Mesh names add no approval step.

## Endpoints and registration

Every node exposes the same paths and tool catalog:

| Role | Endpoint | Session actions |
| --- | --- | --- |
| Access | /terminal-mcp/access/v1/mcp | start, end |
| Execution | /terminal-mcp/executor/v1/mcp | attach, intent |
| Coordination | /terminal-mcp/coordinator/v1/mcp | attach, intent |

Access also exposes get_events. Execution has ten tools and Coordination eight.
Inputs and outputs use universal object schemas, with strict server validation
and structured errors. The retired /mcp route and old role-session actions are
not installation options.

An Access Connector on any Mesh member forwards registration to the current
primary. Its issued four-digit session number works on all members of that Mesh.
A Standalone node issues and checks its own local numbers without a peer.
Configured OAuth/bearer transport authentication remains required independently
of the number. Do not record live numbers, provider identifiers or secrets in
acceptance logs.

## Policies and topology acceptance

A Mesh has one common inherited slot policy. Any authenticated operator entry
point reaches the current primary; individual slots may explicitly override the
parent. Standalone uses the same model locally.

Before a membership change, verify absence of active slots and running agents in
the affected area, including authoritative source state and local execution.
When C leaves X, C clears its old local replicas, attachments and Mesh authority.
The full registry on the remaining X members stays unchanged. A Move obtains
fresh enrollment for C after detach and adopts Y's registry and policy. It
never imports X's registrations into Y.

When the primary leaves a nonempty Mesh, the application first appoints a
successor. The complete registry and common policy are transferred under a
durable fence and an authenticated, revision-bound handoff. Retry or restart
resumes that operation without creating two writable primaries. The departing
node retains independent identity and pairing.

## Minimum evidence

Use existing atomic tests and the small HTTP/real-store integration scenarios.
The acceptance record must identify evidence for these distinct responsibilities:

| Boundary | Required observation |
| --- | --- |
| Catalog | Same tools, inputs, outputs and errors for Standalone, one-member and multi-member Mesh |
| Registry | Issue via a nonprimary Access; attach on another member; independent Mesh rejection |
| Topology | App-authorized create/join/detach/move, active-work rejection, durable replay and recovery |
| Source preservation | Exact source registry rows survive C's departure, including rows issued through C |
| Primary handoff | All registry data and policy survive; stale handoff certificates cannot regain authority |
| Local isolation | Departed bindings and slots fail; destination registrations work; old message content has no routing authority |
| Policy | Common inheritance, per-slot override and destination policy after Join |
| Installation | Empty install, explicit managed reset, safe one-time hot conversion and refusal of ambiguous state |
| Load | No remote replication or periodic peer reconciliation for Standalone; bounded catchup and retries |

A source completion report records the published canonical SHA, functional
commits, test reports, preservation of concurrent changes and cleanup of owned
worktrees. Live rollout and its connector checks require their own authorization
and evidence.
