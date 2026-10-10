# Mesh transport and event-driven replication — 2026-10-10 QA

Task: TMCP-MESH-VPN-EVENT-DRIVEN-SYNC-001. Worktree branch:
`task/TMCP-MESH-VPN-EVENT-DRIVEN-SYNC-001-hotel22`.
This branch began at `5817c42` (release 0.14.2); current Firstbyte and BacLOUD
are **0.17.0** (`66e850f` is the verified 0.17.0 Firstbyte recovery source).
The feature MUST be adapted to the current live source before integrating;
its newer Mesh session-number and collision logic must be retained.

## Findings

Existing Access Mesh stores events atomically in SQLite alongside durable
outbox recipients, and peers ACK only after receipt. However, the transport
also polls issuer-authoritative snapshots every 30 seconds and runs an idle
delivery loop every second. This candidate adds post-commit, thread-safe
event notification to the transport; the normal idle loop waits for a real
event or five-minute anti-entropy deadline. Paginated snapshot catch-up and
full 20-event outbox page draining remain immediate. A degraded peer is
retried after ten seconds; its failure does not block a local write.

Source QA on the isolated worktree: 29/29 focused tests, 108/108 wider
Access Mesh/Fleet regression tests, and Ruff PASS. These are tests against
the **0.14.2 source**, not release certification for live 0.17.0.

## Real-host transport experiment

On Firstbyte and BacLOUD, isolated QA-only WireGuard interfaces were
created on a /30 overlay. Cryptographic handshakes succeeded. The first
trial UDP port 53147 exchanged handshakes but did not reliably pass data.
After switching only the QA interfaces to UDP 53148, bidirectional tunnel
packets passed; observed steady ICMP RTT was approximately 50–51 ms.

A temporary TCP proxy bound ONLY to Firstbyte's private QA WireGuard
address forwarded to the existing local application listener. Production
peer origins, service configuration, authorization and listeners were
**unchanged**. All QA interfaces, proxies and private keys were explicitly
removed from both hosts at 06:49 UTC. No test resources remain.

Bounded BacLOUD -> Firstbyte /health/live test, 16 alternate probes/path:
- Public HTTPS via Cloudflare: 11 success, 5 curl 3-second timeouts.
  Successful responses: 109 ms median and 460 ms maximum total latency.
- Direct WireGuard TCP through QA proxy: 16 success, 0 timeouts.
  Successful responses: 158 ms median and 250 ms maximum total latency.

The public edge was faster on successful requests; the VPN path was more
reliable in this small test. The VPN path bypasses Cloudflare-generated
HTTP 520 but cannot correct SQLite/application-origin timeouts.

Authenticated, **read-only** one-row Mesh snapshot over WireGuard returned
HTTP 200 in 3/3 requests (approximately 112–174 ms). The first public
HTTPS comparison returned HTTP 403 for default Python urllib User-Agent;
using the same User-Agent as production HTTPX yielded public HTTPS
HTTP 200 in 3/3 and VPN HTTP 200 in 3/3. Therefore, those 403s are not
evidence that the production HTTPX path is blocked.

## Deployment requirements and remaining acceptance

Recommendation: use WireGuard for internal Mesh, but production routing
requires an explicit, separately approved switch after current-release
porting and QA. Keep existing bearer/peer authentication; restrict UDP
ingress to the other server's address; accept internal HTTP/TLS only on
the WireGuard address; do not expose internal endpoints to the WAN.
Preserve hostname validation and SNI for HTTPS peers with a scoped
private DNS/transport configuration. Confirm a rollback to the prior
public peer origin with uninterrupted active sessions.

Before enabling in production: port and regression-test against deployed
0.17.0 (not 0.14.2), verify backup and formal SQLite P0 QA, check
Access.start/end, concurrent session numbers/collision, messages, offline
peer and reconnection, third-node catch-up, error rates and 30-second
reconciliation budget. Record before/after request counts in an idle
interval longer than five minutes. Validate live SHA on both hosts.
