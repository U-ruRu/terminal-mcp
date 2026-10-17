# Native messages and namespace isolation

Messages are local to the physical server on which they are sent. Access grants
from the current namespace authorize local messaging, but membership does not
turn messages into a shared cross-server inbox. Call each server's Coordinator
for its local recipients, history, acknowledgements and replies.

Current message operations use the native Access binding and work-session gate.
They share one application implementation with bounded paging and structured
incoming obligations. The former role-session messaging backend and alternative
message argument spellings are removed with the old runtime.

A namespace transition removes old recipient authority, attachments, obligations
and delivery state on the moved node. Preserved historical message content is
archived with its former namespace and cannot become a new Mesh's message or
permission. Existing source-Mesh registrations remain unchanged on the other
members. Historical rows are never replayed into a remote message job.

Mesh registry, topology, trust and derived Console synchronization are separate
protocols with their own authenticated scope. Their existence does not permit
cross-namespace or cross-node message delivery. See the
[connector contract](connector-runtime-contract.md) and
[product contract](mesh-product-contract.md).
