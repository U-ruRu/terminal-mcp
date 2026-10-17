# Mesh transport and optional WireGuard

Mesh membership and trust are assigned by the paired application. WireGuard is
an optional transport for those assigned peers; it does not create a second
membership model or grant an unknown node access to slots. The
[Mesh contract](mesh-product-contract.md) and
[application topology behavior](authorized-mesh-namespace.md) remain authoritative.

## Membership and credentials

A paired application creates or selects a Mesh, enrolls a server using its
short-lived app enrollment descriptor and assigns the primary. The participants
establish the necessary peer trust from that topology. No manual list of prior
Mesh names, issuer identities or operator-approved peer keys is an additional
condition for an authorized Join or Move.

The one-use application enrollment descriptor is separate from shared peer
ingress. Existing Mesh credentials cannot enroll a node into another Mesh.
Departure rotates the local ingress authority and clears old slot bindings and
Mesh-only permissions while preserving the node identity and application pairing.

## Transport operations

The Console transport API remains under the existing transport paths:

- GET /actions/fleet/control/transport
- POST /actions/fleet/control/transport/prepare
- POST /actions/fleet/control/transport/enroll
- POST /actions/fleet/control/transport/activate
- POST /actions/fleet/control/transport/backend
- POST /actions/fleet/control/transport/switch
- POST /actions/fleet/control/transport/revoke

These are internal protocol names retained by the current Mesh API. The same
pairing boundary protects control mutations. Responses expose transport outcome
and status without returning private keys or reusable peer credentials. Runtime
configuration follows the committed topology; no separate Mesh-enable flag is
required.

WireGuard preparation and transport switching affect how a known peer is reached.
They must retain current peer authentication, namespace checks and operation
receipts. Native messages remain node-local and are not queued for remote delivery
through either HTTPS or WireGuard. Access registry and topology synchronization
continue to use their own bounded, authenticated protocols.

## Recovery and diagnostics

Re-read GET /actions/fleet/control/transport after a lost response and confirm
the desired transport before retrying. Transport requests do not accept the
topology operation_id field; switch supports expected_topology_revision. A detached or
moved node must not resume traffic authorized only by its previous membership.
Primary transfer and namespace movement are control operations; they are not
implemented by editing a transport override file.

The supported transport CLI can inspect or provision an already assigned peer.
Configuration paths such as fleet-peer-transports.json are implementation details,
not an independent product mode. Preserve immutable node identity and pairing
when recovering a hot installation. An explicit clean deploy resets the selected
installation's managed secrets, configuration and local backups.

Standalone has no peer synchronization worker. Its local Console projection is
refreshed on demand. Mesh workers stop or change peers as committed membership
changes; projection data and old in-flight replies cannot cross that boundary.

No live VPN change or server deployment is performed by this source update.
