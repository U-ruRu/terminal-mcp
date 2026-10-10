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
  --overlay-ip 10.244.12.1 --endpoint 185.244.172.75:53148
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
