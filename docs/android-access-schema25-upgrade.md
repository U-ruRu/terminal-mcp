# Upgrading existing Android Access data to unified Mesh

The former schema-25 rollout is historical. Current Console and Android sources
use the same native Access management contract and Mesh topology API. There is no
optional Access activation, legacy-admission switch or alternate slot response.
The authoritative requirements are in [the Mesh product contract](mesh-product-contract.md).

## Preserved state

Hot upgrade keeps existing native slot identities, registrations, individual
policy overrides, session timestamps and exact deadlines. Supported old session
records are converted once; the migration receipt prevents a later namespace
change from importing them again. Historical issuer provenance remains available
when the complete old Mesh registry is assembled on its primary.

The inherited Mesh policy comes from the primary. A member-local historical
parent is not a second Mesh policy. The registry upgrade assembles all required
member exports before applying the common policy; offline or conflicting inputs
keep the registry unavailable. Existing individual slot overrides are retained.

The offline release check compares protected old rows and permits only named,
independently verified conversions. It authenticates encrypted issuance receipts
before comparing their payloads. A changed issued number, namespace, policy,
credential fingerprint or unrelated application field fails preservation.

## Client and activation behavior

Console uses temporary and persistent native slot kinds, current policy revision
checks and stable mutation keys. It keeps an operation pending after a lost reply
and resolves the committed outcome by reading current state. Every paired member
can serve as the application's Mesh management entry.

Readiness is separate from process liveness. Candidate activation requires the
registry to finish migration and any topology transition to leave its fenced
state. A live process with an incomplete registry is not an accepted update.
Clean deploy instead clears the selected installation's managed state, including
pairing, identity secrets and local backups, and creates a fresh Standalone node.

See [the current deployment contract](access-mesh-deployment.md). These source
changes and isolated tests do not deploy to running servers.
