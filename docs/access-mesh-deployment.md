# Access Mesh V2: deployment and acceptance

Release contract: **0.14.4**. This procedure targets **FirstByte and BacLOUD only**. Secondary hosts source, isolated tests and coordination. Main/Tokyo are outside this rollout. A passed source suite is preparation evidence, not proof of an installed release.

## 1. Freeze the candidate and prepare recovery

Freeze one immutable release SHA after integrating runtime, task ownership/receipts, messaging, metadata and documentation. Run the complete regression suite, Ruff and the repository privacy check on that SHA. Verify the packaged `dist/terminal-operations.skill` matches both source files and the effective schema manifest exposes Access1/Executor10/Coordinator8.

Record each target's current version/SHA, active release path, deployment mode, service units, config ownership, public origins and current health. Stage release files separately from the active release. Preserve permissions for `/etc/terminal-mcp/terminal-mcp.env` and all secret-bearing configuration; keep secrets out of command output and evidence attachments.

Use consistent SQLite backup/snapshot procedures for durable runtime, auth and Fleet control state. Record `/var/lib/terminal-mcp/terminal-mcp.sqlite3`, `/var/lib/terminal-mcp/auth.sqlite3`, `/var/lib/terminal-mcp/fleet-control.sqlite3`, the existing release/configuration, and their restoration policy. The output cache `/var/cache/terminal-mcp/output.sqlite3` is independently retained. An ordinary filesystem copy of a live SQLite file is not a sufficient substitute for a consistent backup.

Access revocation, code-rotation/tombstone and newer security state are rollback-excluded data. Plan recovery with that state intact. The installer performs schema/security compatibility checks; an older binary cannot be forced into service just because a topology rollback succeeds. The `--allow-downgrade` option is not permission to bypass durable compatibility checks.

## 2. Validate local identity and authenticated mesh configuration

Access Mesh uses the environment prefix `TERMINAL_MCP_`. For each target validate:

| Setting | Required meaning |
| --- | --- |
| `ACCESS_MESH_ENABLED` | Enable the V2 catalogs/runtime deliberately. Default is false. |
| `PERSISTENT_AGENTS_ENABLED` | Enable the native identity/session storage used by both slot kinds. |
| `FLEET_INSTANCE_ID` | A stable, unique local node id. |
| `ACCESS_MESH_PEERS_JSON` | Explicit unique remote peer ids; FirstByte lists BacLOUD, BacLOUD lists FirstByte, matching configured ids. |
| `FLEET_PEERS_JSON` | Authenticated Fleet definitions covering those ids. |
| `ACCESS_MESH_PROOF_KEY` | At least 32 bytes, provisioned securely and consistently for the trust configuration. Never print it. |
| `ACCESS_MESH_LEGACY_ENABLED` | Explicit policy for legacy slot issuance. |
| `ACCESS_MESH_DURATION_SEC`, `ACCESS_MESH_COOLDOWN_SEC` | Initial policy defaults. Runtime defaults are 1200 and 60 seconds. |
| `ACCESS_MESH_WARNING_SEC`, `ACCESS_MESH_DRAINING_SEC` | Initial thresholds, default 120 and 30 seconds; each smaller than duration. |

Peer ids must be distinct, exclude the local node and be present in authenticated Fleet configuration. Validate proof/trust configuration without placing keys in the repository. Per-slot and durable operator defaults can differ from initial environment defaults; read the operator defaults/slot views when validating effective policy.

Verify ingress and transport authentication for all six MCP URLs, plus the authenticated internal Fleet paths. Access session issuance is separate from role attach. A four-digit session number is sensitive binding material, not a replacement for OAuth/bearer transport security.

## 3. Install and activate under the release gate

Choose the installer path appropriate to the existing topology. Public installer commands are:

```bash
sudo ./deploy/install.sh update
sudo ./deploy/install.sh doctor
```

`update` stages the candidate, checks compatibility/downgrade policy, takes its configured backup and activates/probes the release. There is no standalone public `install.sh stage` command. Review candidate/configuration/backup evidence before issuing update. Use a separate source/build staging directory when preparation must occur without activation.

For an existing Unix executor split, follow [the split-service procedure](architecture-split-service-cutover.md) and its guardrails. The API remains `terminal-mcp.service`, executor `terminal-mcp-executor.service`, socket `/run/terminal-mcp/executor.sock`. Topology render/check and approved activation are explicit steps; do not mix unreviewed split migration with a feature rollout.

Activate FirstByte, check its actual version, local services, local storage and Access/Executor/Coordinator discovery, then activate BacLOUD and repeat. Temporary partial peer delivery while versions differ is a transition state, not a passed acceptance result. Confirm both servers run the exact intended SHA before testing cross-node behavior.

After both activations, verify issuer grant catchup, deadline/revocation propagation, native message outbox progression and health components. Monitor retries/errors and cleanup backlog. Health collection ok is distinct from overall healthy/status. Preserve the deployment journal and installer compatibility decisions.

## 4. Native acceptance matrix

Use independent trusted connector identities for independent agents. Use disposable test slots/tasks and isolated commands; record only public names, hashes, revisions, timestamps and redacted evidence. The minimum matrix is:

| Area | Native checks on FirstByte and BacLOUD |
| --- | --- |
| Discovery | Access1, Executor10, Coordinator8 at the exact three paths; permissive input planning, strict runtime repair, real annotations/action matrix and bounded outputs. |
| Both issuers | Create a legacy slot from each Access; operator-provision and activate persistent; attach the returned issuer/session number once to both roles/nodes. Same code digits from different issuers resolve independently. Wrong issuer, code or binding fails before effects. |
| Lifecycle | Duration/cooldown/rearm; warning/draining; deadline shortening/extension; end; suspend/resume; rotate/delete; new local epoch and exact execution/claim cleanup. Test a short disposable policy instead of modifying production agents. |
| Task ownership | Claim preserves ready; explicit state enters in_progress; release and expiry preserve each explicit state/checkpoint/result. Legacy releases at end; persistent release_on_end behavior is explicit. Stale revision/owner, repeated cleanup and successor-claim races leave correct ownership and atomic receipts. |
| Commands | Launch/read/cancel/recovery with correct task attribution; code-free reads of another agent's local hash; hashless all-agent journal, paging and retained-output behavior. Queue/admission/fencing remain valid. |
| First-contact messages | Send/receive/read/ACK/reply/history in both directions without a warmed route cache; Executor ↔ Coordinator. Test stable sender/public_name and reply returning to the parent origin node. |
| Broadcast and obligations | local sends no peer delivery; fleet delivers to unique active recipients, excluding global sender and expired/duplicate LA. Notify surfacing, explicit ACK, alert reply and command gate use the same local obligations. |
| Partition/restart | Pause only the disposable test peer path under an approved isolated test plan. Existing local writes/reads/ACK use local rules. Message queued/partial receipts survive lost ACK and restart; restore connectivity, catch up and deduplicate. A partition cannot make an unknown grant attach succeed. |
| Boundaries | Oversized full message/output and full Fleet envelope/proof cause truthful outcomes and no partial mutation. Test opaque cursor scopes, replay with reused ids/different payload, output fallbacks and operator revision/idempotency errors. |
| Mobile/operator | Read slots/defaults, create immutable kinds, per-slot policy, current-cycle deadline, end, suspend/resume/rotate/delete via authenticated `/actions/access/*`; no shell command surface. |

Primary paths on each target:

```text
/terminal-mcp/access/v1/mcp
/terminal-mcp/executor/v1/mcp
/terminal-mcp/coordinator/v1/mcp
```

Legacy `/mcp` compatibility is an additional check, not a substitute for these six connectors. A local unit harness proves its isolated scenario; real HTTP/MCP tests prove adapter/transport integration; deployment evidence proves the running target.

## 5. Completion and rollback decision

Record candidate SHA/version, source full-suite evidence, installed SHA on both nodes, role/schema digests, local and bidirectional acceptance results, health, catchup/outbox/cleanup state, and the handling of disposable resources. Preserve result/checkpoint data. Close dependent tasks only when their own acceptance criteria and this release gate are satisfied.

If activation fails, use the installer decision and captured deployment journal to choose a compatible recovery. Split topology rollback restores the captured units/configuration, not an earlier database/security history. Restore application data only through an explicit consistency/security recovery plan, preserving newer revocations and issuer provenance. Re-run health and native acceptance after recovery; a successful process start alone is not recovery acceptance.
