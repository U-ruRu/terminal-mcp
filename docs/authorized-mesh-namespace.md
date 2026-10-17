# Authorized Mesh namespace transitions

Fleet ID is public namespace metadata. It does not authenticate a node or operator.
An autonomous node may join a Mesh with a different Fleet ID through the existing
Console/APK operator enrollment and authenticated controller replication flow.
Matching Fleet IDs do not authorize enrollment either.

The receiving node verifies the management peer and its existing enrollment ingress
credential before accepting a new controller. A controller with active members or
an attached follower cannot be silently rehomed. Node IDs, signing identity, OAuth
storage and the node-local Access bootstrap key remain unchanged.

The accepted snapshot and a pending namespace intent commit in the control database.
The server then updates its env file and source/projection namespace metadata.
Startup and reconcile finish interrupted writes idempotently. Detach restores the
autonomous namespace saved on first enrollment; the existing detach flow rotates
the ingress credential, so a subsequent Join retrieves a fresh enrollment descriptor.
Source stream generation and stored session/domain data are preserved.

APK reads `namespace_transition_supported` from the receiving server. An older
server without that capability is rejected before a different-namespace transition
can detach the node. Controllers can retain the existing enrollment/snapshot wire
contract; only the receiver needs the new namespace transition support.

Switch between independent controllers is an explicit Detach followed by Join.
APK stores only the public destination intent before Detach. It confirms autonomy
by a direct target read before Join. An uncertain result or interrupted application
keeps that intent for the user to resume with Apply. It never stores enrollment
tokens in the intent, automatically replays an ambiguous Detach, or claims that
the two controllers committed an atomic transaction. Fresh reads verify completion.
If the destination is unavailable after Detach, the node remains autonomous until
the user resumes the requested Join.
