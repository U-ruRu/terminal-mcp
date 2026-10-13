# MCP identity normalization candidate: ChatGPT + Codex

Base: published `origin/integration/M3-functional-candidate` at `27327398a01b9d99897e5137aeb4c659e3d8c581`.
Scope: isolated candidate only; no authentication configuration or deployed runtime changes.

## Observed evidence

The cloud Python probe used a client-generated callId and did not establish a native Codex identity on TMCP. A subsequent **native Codex app-server** canary (Codex CLI 0.159.2) observed two tool calls with a stable `threadId` and numeric `progressToken`; neither supplied `sessionId`, `callId`, or `openai/*` fields. The `codex exec` experiment failed before contacting the canary. Hosted Codex Cloud plugin MCP request metadata remain **unverified**.

## Candidate behavior

- The request-scoped MCP transport adapter recognizes the existing `openai/subject` + `openai/session` family unchanged. A partial OpenAI profile remains invalid, including if Codex hints are also supplied. If no OpenAI identity fields exist, `threadId` selects the Codex profile.
- `ProviderIdentityRegistry` maps either recognized profile to the existing `ProviderIdentity(provider, subject, conversation)`. For Codex only, `subject` comes from **server-verified admission principal_id**, and `conversation` comes from the client-supplied `threadId`. The code neither trusts a caller-provided subject nor infers a user identity from `clientInfo`.
- Codex `sessionId` (shared root group), `callId` (tool invocation), and `progressToken` (progress correlation) are **optional hints**. They have no influence on agent bindings. No client call ID is promoted into a replay/idempotency key.
- Bindings continue through the existing authority, principal-scoped Access Mesh connection key, stable server-owned LogicalAgent, per-role attachment, and WorkSession lifecycle. Access, Coordinator, Executor and Events subscriber resolution use the same normalization and authorization boundaries. An MCP event subscriber must match an existing authorized connection.
- The OAuth principal in the current built-in issuer may be a registered **OAuth client identifier**, not the human account. Cross-client or cross-provider continuation uses explicit server-authorized binding (such as the Access session number), not blind identity merging. A caller providing only a root session, call ID or progress token cannot be assigned an agent.
- Existing third-party `MetadataIdentityAdapter.resolve(metadata)` implementations retain their original method signature. New principal-backed adapters opt into `principal_id` inside the registry.

## Deliberately deferred

1. Native hosted Codex Cloud plugin transport evidence and resulting metadata contract (including whether a stable thread field is emitted).
2. Durable group relationship from optional `sessionId`; no `session_epoch` changes.
3. General-purpose client-specific request ID replay deduplication: only a stable, authenticated, well-scoped request key with payload fingerprint and conflict behavior may be used.
4. Auto-enrollment of clients lacking both a stable external thread and explicit Access binding; one OAuth principal alone cannot distinguish concurrent agents.
5. Runtime deployment, Mesh rollout, cross-client OAuth re-enrollment migration, and release acceptance.

## Verification

Using a pre-existing disposable test Python environment and isolated `--basetemp`, with automatic temporary test directory removal:

```bash
PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 python -B -m pytest -q -p no:cacheprovider \
  tests/test_provider_identity_domain.py \
  tests/test_mcp_provider_evidence.py \
  tests/test_role_mcp_v1.py \
  tests/test_mcp_events_message_binding.py \
  tests/test_managed_identity_authority.py \
  tests/test_access_mesh_identity_history_qa.py \
  -k 'not test_access_issue_other_connector_attach_subscribe_and_local_delivery'
```

Result: **114 passed, 1 deselected**. The single deselected unrelated existing Events test expects `timestamp` under `data`, while the published compact Events contract puts the timestamp at event top level. Running it as part of the unfiltered selection produced `KeyError: timestamp`. It should be reconciled by the Events owner and is not a Codex identity contract change.

Ruff lint on touched files and `git diff --check` passed. No full regression, real hosted-plugin E2E, or deployment acceptance has been performed.
