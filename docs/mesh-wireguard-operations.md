# Terminal MCP — WireGuard Mesh transport and enrollment

WireGuard is the preferred **inter-server** transport after enrollment. It
protects the channel and reduces Cloudflare dependency. Fleet's existing
Ed25519 identity, authenticated internal HTTP endpoints, bearer tokens,
Access issuer checks and durable message/outbox contracts remain in force.
Each **outgoing peer** independently selects `https` or `wireguard`; mixed
mode is supported. Neither transport is an automatic trust substitute.

## Driver choice (per server)

`auto` tries Linux kernel WireGuard and falls back to installed
`wireguard-go` if kernel interface creation fails. `kernel` and
`userspace` explicitly require the selected implementation. The same
`wg` control utility configures both; overlay addresses, keys and
cryptographic wire protocol are unchanged across backends.

Install `wireguard-tools`; install `wireguard-go` when userspace mode is
desired (Debian: `apt install wireguard-go`). Kernel mode requires kernel
WireGuard capability, typically provided by `wireguard` module. Userspace
needs `/dev/net/tun`. The operator must provision OS firewall rules
restricting WireGuard's UDP listener to expected underlay peer addresses
and the internal HTTP proxy to the WireGuard interface. Do not run arbitrary
default-route or host-routing changes as part of this feature.

## Identity and first-time registration

A WireGuard tunnel cannot bootstrap without exchanging peers' public keys
and endpoints. Bootstrap over the *existing* trusted HTTPS Fleet membership
or an operator-controlled out-of-band channel. For a **new** Fleet node, the
operator must first approve the signing-key fingerprint and provision the
normal Fleet peer signing public key, HTTPS origin and bearer-token trust.
WireGuard enrollment intentionally cannot silently create Fleet trust.

On each node run these commands using the installed `terminal-mcp` and
its existing local `/etc/terminal-mcp/terminal-mcp.env` Fleet identity.
Choose unique private overlay addresses and public UDP endpoints:

```bash
# Firstbyte (example underlay address, no private keys transferred)
sudo terminal-mcp mesh-vpn init --backend auto --interface tmcpwg \
  --overlay-ip 10.244.12.1 --endpoint 185.244.172.75:42063 --listen-port 42063
sudo terminal-mcp mesh-vpn offer > /tmp/firstbyte-offer.json

# BacLOUD
sudo terminal-mcp mesh-vpn init --backend auto --interface tmcpwg \
  --overlay-ip 10.244.12.2 --endpoint 88.119.171.230:53148
sudo terminal-mcp mesh-vpn offer > /tmp/bacloud-offer.json
```

Each signed offer contains only public information: WireGuard public key,
underlay endpoint, overlay IP, issuer identity, Fleet signing public key,
creation/expiry (10-minute validity), random nonce and signature.
The WireGuard private key stays in `/etc/terminal-mcp/mesh-vpn/private.key`
with owner-only permission. Exchanging offers does **not** exchange secrets.

Exchange offers using an authenticated administration path. On Firstbyte
register the BacLOUD offer, and vice versa:

```bash
sudo terminal-mcp mesh-vpn join --peer bacloud --manifest /tmp/bacloud-offer.json
sudo terminal-mcp mesh-vpn join --peer firstbyte --manifest /tmp/firstbyte-offer.json
```

For existing Fleet peers, each offer signature is verified against the
**already pinned** Ed25519 signing public key, including node name and
expiration. When the peer is new, explicitly confirm its SHA-256 signing
key fingerprint independently using `--approve-fingerprint`; then enroll
normal Fleet bearer/identity trust before routing application traffic.
A changed peer WireGuard public key, overlay route or endpoint requires an
operator-managed rotation; silently replacing it is prohibited.

## Bring-up and persistent services

```bash
sudo terminal-mcp mesh-vpn up --backend kernel
sudo terminal-mcp mesh-vpn status

# To run userspace explicitly, first stop and install wireguard-go:
sudo terminal-mcp mesh-vpn down
sudo terminal-mcp mesh-vpn backend --mode userspace
sudo terminal-mcp mesh-vpn up

# Generate opt-in systemd units; inspect them before enabling:
sudo terminal-mcp mesh-vpn install-service
sudo systemctl daemon-reload
sudo systemctl enable --now terminal-mcp-mesh-vpn.service \
  terminal-mcp-mesh-vpn-proxy.service
```

The tunnel's AllowedIPs and routes are exclusively the enrolled private
peer /32 addresses, never a default route. The proxy listens only on the
local **WireGuard overlay IP** at port 18080 and forwards to
`127.0.0.1:8080`. The original HTTPS public endpoint and TLS
configuration stay intact. The two systemd units start on reboot, with
the proxy dependent on the tunnel. Restarting the main app after changing
Fleet origin settings is an explicit operator action.

## Per-peer switch, health and rollback

Confirm a current WireGuard handshake and successful private
`/health/live` response before switching:

```bash
sudo terminal-mcp mesh-vpn switch --peer firstbyte --mode wireguard
# Writes /etc/terminal-mcp/fleet-peer-transports.json atomically.
# Then restart terminal-mcp.service in a controlled maintenance window.
sudo systemctl restart terminal-mcp.service
```

This affects *only* the named outgoing Fleet peer. The generated override
uses an HTTP address solely inside the private encrypted WG overlay.
The service's normal Mesh authentication remains mandatory. The public
HTTPS origin is retained in the original `FLEET_PEERS_JSON` for bootstrap
and rollback. There is no silent automatic fallback from WireGuard to
Cloudflare.

Rollback, including a failed tunnel or kernel/userspace transition:

```bash
sudo terminal-mcp mesh-vpn switch --peer firstbyte --mode https
sudo systemctl restart terminal-mcp.service
# When no peer uses the tunnel:
sudo systemctl stop terminal-mcp-mesh-vpn-proxy.service \
  terminal-mcp-mesh-vpn.service
```

Verify current deployed SHA and backed-up config/storage before production
switches. Perform one node at a time. Accept only after independent checks
for Access.start/end, concurrent session-number reservation, collision,
messaging ACK, peer partition/recovery, third-node catch-up, a quiet idle
period > 5 minutes and a quick switch back to HTTPS.

## Scope and limitations

The CLI enrolls and provisions existing trusted Fleet peers; it does not
write Fleet bearer tokens to logs or auto-authorize unknown nodes. The
private HTTP proxy is an application adapter, not a general VPN gateway.
Kernel and userspace are both Linux choices in this implementation.
Userspace transport requires a separately installed `wireguard-go`.
Actual production enablement remains a release operation requiring
working database, passing integration tests and an immutable SHA.

## Real-host QA on 2026-10-10

Signed Fleet-bound enrollment through the new CLI passed on both Firstbyte
and BacLOUD. Firstbyte used kernel WireGuard; BacLOUD used a temporary
extracted wireguard-go binary (its /run filesystem is noexec).
Cross-backend handshakes worked. UDP 53149 showed packet loss; changing
QA interfaces to 53148 gave 5/5 BacLOUD -> Firstbyte ICMP packets at
50-52 ms, but the reverse direction still had losses. Thus this is not
yet a bidirectional production PASS. Capture UDP/NAT/firewall behavior,
run authenticated internal HTTP and partition recovery QA before cutover.
Both temporary interfaces, private keys and userspace staging binary were
removed. Existing Terminal MCP services returned HTTP 200 and retained
the original public HTTPS Mesh route. No production switch was made.

## Android Console — Fleet Control integration

The mobile Console's **Connections → Mesh → server card → Private Mesh tunnel**
section is an operator-facing control plane for the *same* Fleet membership
and Ed25519 trust authority already used by add/move/detach.

1. Add or move both servers into the same managed Mesh in Connections.
   Ensure the topology has converged and the mobile application can still
   reach both public HTTPS endpoints independently.
2. Expand Private Mesh tunnel on each server; enter the unique private overlay
   IP and its public UDP endpoint. Select `auto`, `kernel`, or `userspace`
   and press Prepare identity. A new private key stays local; the signed
   public offer becomes available over the paired operator API.
3. Choose the other server and **Exchange signed keys**. The Console reads
   *fresh* signed offers from both paired nodes and submits each to the other.
   Each server verifies its peer against the currently authoritative managed
   Mesh signing key, identity, signature and ten-minute expiry. A local
   HTTP request cannot silently create new Mesh membership/trust.
4. Start the WireGuard tunnel independently on both nodes. This requires
   the service operator to have permission to install and start the two
   systemd units; restricted installations return a failure receipt rather
   than expanding permissions. Userspace mode requires wireguard-go installed.
5. Inspect live tunnel status and, after handshake and private application
   health checks pass, switch the desired peer to WireGuard. The server
   enforces the **expected Mesh topology revision** before applying the
   route. Fleet and Mesh runtime config updates immediately from the
   atomic override without replacing OAuth/Access identity. The original
   HTTPS route remains paired and is an explicit rollback action.
6. To stop using the private route, switch the peer back to HTTPS. Removing
   a server from the managed Mesh is an authority mutation: its peers'
   local reconciliation removes obsolete WireGuard keys and origin overrides.
   On disconnected peers this completes when the latest topology is applied,
   and the app reports synchronization state separately.

New local operator routes are `GET /actions/fleet/control/transport` and
`POST /actions/fleet/control/transport/{prepare,enroll,activate,backend,switch,revoke}`.
They are registered only when managed Fleet Control is enabled, use the
existing paired/operator authorization, prohibit arbitrary shell commands,
and return operation outcomes without private keys or Fleet bearer tokens.
Android Console refreshes VPN state only when its panel is opened or after
an operator action; it does not introduce additional continuous polling.

The private WireGuard tunnel is not a mobile device VPN. The mobile
application acts as the trusted **control plane**, while WireGuard remains
server-to-server data transport. Failed tunnel activation never silently
promotes an unverified route; HTTPS remains usable for recovery.

## 2026-10-10 follow-up: UDP port-pair root cause and resolution

Network-layer forensics on the real Firstbyte/BacLOUD hosts isolated the
reverse-path failure to specific **underlay UDP port pairs**, rather than
a WireGuard cryptographic incompatibility. Ordinary host-to-host ICMP
had 10/10 success both directions at ~48 ms. The original symmetric
port 53148 and test port 53151->53148 lost many packets, even when a
raw Python UDP sender bypassed WireGuard entirely: 4/4 ordinary UDP
datagrams with source port 53151 and destination 53148 were lost before
BacLOUD's eth0 capture. In the same port matrix, 4/4 datagrams for each
other tested combination (sources 42063, 45063, 61063 and ephemeral)
and destinations 42065/53148/53150 reached BacLOUD, except the blocked
53151->53148 pair. Thus the fault belongs to the path-specific UDP
filtering/middlebox behavior and is independent of kernel/userspace driver
choice; the responsible upstream provider/filter has not been proven.

**Verified working pair on these hosts:**
- Firstbyte: kernel WireGuard, UDP listener 42063, private overlay /32.
- BacLOUD: userspace wireguard-go, UDP listener 53148, private overlay /32.
- Reestablished WireGuard handshake; ICMP BacLOUD->Firstbyte 20/20,
  Firstbyte->BacLOUD 20/20, RTT about 50 ms in both directions.
- Temporary HTTPS app proxy inside WireGuard: 50/50 HTTP 200
  Firstbyte->BacLOUD, 50/50 HTTP 200 BacLOUD->Firstbyte, avg around
  121/132 ms (successful samples). Traffic uses encrypted private
  overlay and original service upstream 127.0.0.1:8080.

Port selection is a real deployment dimension; mobile prepare supports
individual UDP listen ports and external endpoint ports. As a cutover
gate, the `switch` operation requires a fresh handshake **and eight
consecutive health HTTP 200 checks** across the private tunnel, rejecting
intermittently broken routes. Complete extended Mesh acceptance
(partition/reconnect, durable events, enrollment from the shipped mobile
app) remains a separate release check. Preserve HTTPS rollback.

## 2026-10-10 link-flap recovery confirmation

Under the proven asymmetric UDP ports 42063/53148, a sustained ICMP
test achieved **100/100** in each direction (RTT ~49–50 ms), plus
50/50 HTTP 200 responses in each direction through an isolated
private tunnel-to-localhost TCP proxy.

A simulated userspace interface DOWN/UP caused a *different* one-way
failure. Captures on BacLOUD showed Firstbyte's encrypted UDP packets
arriving and decrypting into ICMP echo requests, but BacLOUD did not
produce replies. The kernel route lookup revealed that Linux had
**removed the manually installed peer /32 route** on interface DOWN,
falling back to eth0 as the reverse path. Replacing the route with
`ip route replace 10.253.241.1/32 dev tmcpwgdiag` restored 20/20
ping replies in both directions, without changing crypto keys.

This is an interface lifecycle routing problem, independent from the
initial path-selective UDP blackhole. The installer now includes a
`terminal-mcp-mesh-vpn-routes.service` netlink event watcher. It
reinstalls peer /32 routes when the managed WireGuard interface is
brought UP, and verifies that the interface's public key matches
the local persisted WireGuard identity before touching routing.
It does not poll or create Internet/default routes. Normal `mesh-vpn up`
already recreates routes at startup. Always validate the route
watcher with a separate staged link-flap before production activation.

## 2026-10-10 final independent VPN QA session

Source candidate ref: feature worktree on current canonical
`integration/M3-functional-candidate` (base `ce8dbf8`). All observations below
were performed on temporary **QA-only** interfaces, private keys and
userspace packages; production Mesh peer origins and Terminal MCP
services were not modified.

### UDP underlay is flow/port-sensitive and can fail after idle

On Firstbyte (kernel) and BacLOUD (wireguard-go), tested six actual
underlay port combinations in addition to the previous 42063/53148
pair, which had temporarily become fully unresponsive after previously
passing 100/100 ICMP plus 50/50 HTTP.

- FB/Bac UDP 51820/51820: 0/7 packet delivery both directions.
- 45063/53150, 61063/53150, 42063/53150, 42063/42065,
  61063/42065: 7/7 packets delivered *each direction* in the initial
  short test for each pair.
- 61063/42065: 100/100 ICMP in each direction, and 50/50 successful
  HTTP `/health/live` in each direction. Mean HTTP response ~128 ms
  Firstbyte to BacLOUD, ~124 ms BacLOUD to Firstbyte.
- Later, without changing peer keys or production configuration,
  61063/42065 lost connectivity across the private tunnel while
  both Linux routes remained present, both wireguard processes stayed
  alive, and the latest WG handshake became stale. Read-only Mesh
  snapshot via the private route timed out; a comparable read-only
  public HTTPS snapshot also showed an HTTPX ReadTimeout in that
  observation window. Local `/health/live` on both production hosts
  remained HTTP 200.

These measurements confirm kernel/userspace wire-protocol compatibility,
but **do not establish stable VPN service under production idle/rekey
conditions**. Short-term 100% delivery must not be used as the sole
criterion for deploying a primary Mesh transport. A middlebox or upstream
network filter is a plausible source of port-specific failures, but the
responsible network device/provider has not been conclusively identified.
The separate public HTTPS timeout confirms that application/SQLite stalls
must also be investigated independently.

### Event-driven link route recovery — live PASS

The new `mesh-vpn watch` process ran directly on both real hosts.
After `ip link set <QA_IFACE> down`, the Linux peer /32 route
disappeared and route lookup fell back to the physical interface.
After the interface was set UP, the `ip monitor link` watcher
automatically reinserted the /32 peer route with no manual route
command. Both watchers remained alive. The route watcher uses
link netlink events, not rapid periodic polling.

### Dynamic Fleet Control integration

Managed Fleet Control updates now reach Access Mesh Replication as well
as the other runtime components. Only the transport origin and active
membership are carried across to Access Mesh; its existing pinned
bootstrap public-key/token trust is **not** replaced with unreviewed
managed peer keys. New managed members do not acquire Access Mesh
issuer trust merely by selecting a WireGuard route. The sync worker
invalidates the anti-entropy pass on an origin change and wakes for
prompt state reconciliation.

### Release gate

1. Diagnose WG underlay idle/rekey behavior with packet captures at
   both external NICs, testing a full idle window exceeding five
   minutes, rekey, and multi-minute authenticated Mesh API traffic.
2. Accept both directions of authenticated snapshot, number
   reservation/start/end/conflict, messaging, offline outbox and third
   node catch-up with verified latency and zero errors.
3. Finish separate Firstbyte SQLite incident QA; public HTTPS
   ReadTimeout is not automatically fixed by VPN.
4. Merge the opt-in feature linearly only after regression approval,
   reconcile source-vs-live versions (canonical source's `version.py`
   still reads 0.14.4 while both deployed connectors report 0.17.0),
   bump version according to the release contract, and deploy
   one server at a time with immediate HTTPS rollback.
5. Do not remove public recovery access during the transport cutover.

## 2026-10-10 — IPv6 underlay compatibility and long-idle QA

After capturing a kernel-to-kernel failure over IPv4 UDP, we proved that
encryption implementation was not the fault: Firstbyte's external NIC
captured outgoing encrypted UDP packets to BacLOUD, while those packets
were completely absent from BacLOUD's external `eth0` capture.

**IPv6 underlay** offers an alternative path while preserving internal
IPv4 overlay /32 addressing. Not all addresses on a multi-homed node
are equivalently reachable:

- Firstbyte reachable external IPv6: `2a04:5200:fff5::344a`
  (BacLOUD to this address: 8/8 ICMP, while an alternate Firstbyte
  IPv6 address failed 0/8).
- BacLOUD external IPv6: `2a04:2181:c011:1::bafb:f78e`
  (Firstbyte to this address: 8/8 ICMP).
- Set the **underlay** endpoint to `[IPv6]:UDP-port` in Mesh VPN
  init/mobile enrollment; both the signed enrollment offer and the
  `wg set peer endpoint` use the canonical bracketed IPv6 literal.
- The inside-the-tunnel Mesh host remains private IPv4.
- The IPv6 endpoint parser rejects DNS names, zone-qualified addresses,
  non-global IPv6, multicast, link-local, loopback and invalid UDP ports.

### Kernel ↔ kernel, IPv6

With UDP 61063 on Firstbyte and 42065 on BacLOUD, the IPv6 encrypted
tunnel achieved 25/25 ICMP both directions initially and continued
updating the handshake after more than ten minutes online. After an
extended quiet window, 80/80 ICMP passed in each direction (RTT about
47 ms), and authenticated, read-only Access Mesh snapshot completed
3/3 in each direction, medians 79 and 64 ms.

A temporary private TCP-to-127.0.0.1:8080 proxy returned 30/30
healthy requests BacLOUD→Firstbyte. Firstbyte→BacLOUD initially yielded
5/30 due to empty HTTP replies, although 80/80 ICMP passed; local
application and local proxy retests on both servers returned 30/30
HTTP 200. Record this as a **transient application/proxy QA concern**,
not an unexplained WireGuard datagram loss.

### Kernel ↔ wireguard-go, IPv6

A separate userspace instance on BacLOUD first suffered severe packet
loss on an independently chosen UDP pair 61064/42066. The same instance
was switched to the proven IPv6 pair 61063/42065 (after releasing the
kernel-kernel QA sockets) and achieved:

- 50/50 ICMP Firstbyte kernel → BacLOUD wireguard-go, RTT 50.0 ms
- 50/50 ICMP BacLOUD wireguard-go → Firstbyte kernel, RTT 49.8 ms
- 30/30 private HTTP health checks in each direction, means 126/122 ms
- 5/5 authenticated read-only Mesh snapshots in each direction,
  medians 98/63 ms

This is a successful **cross-backend interoperability and live Mesh
application test**, not yet proof of a five-minute userspace idle/rekey
window. Ensure this final extended test, rollback and versioned
production acceptance before routing all production Mesh traffic
through the new VPN. QA interfaces, keys, and helper binaries are
temporary and must be cleaned up before the work session ends.
