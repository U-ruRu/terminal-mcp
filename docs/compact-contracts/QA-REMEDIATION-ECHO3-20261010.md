# Terminal MCP — RKB V4 compact contracts, remediation B1–B13

**State: corrected offline candidate, pending independent acceptance. No deployment.**

Parent is QA SHA 02b8f36a8855949bfb5f67ad568fdc0e40b1b0a9, independently based on original generator d1caea24e2d8c2dff650568fac616bcee0efb99b. The original QA verdict was BLOCKING and remains archived unchanged in QA-BRAVO3-20261010.md. RKB algorithm source was e721d0bea8d47ef042d429f8651783cc5708efbe.

## Publication design

**Public tool inputSchema:** object transport envelope containing a property for every real argument name. Every property has empty schema {}, additionalProperties is true, and no required/type/enum/if/then/oneOf/bounds assertions remain. This is intended to pass malformed and unknown named inputs to the existing server-side RuntimeBoundary, model validators, canonical action models and handlers for **bounded structured errors**. A non-object JSON-RPC arguments payload can still be rejected at the MCP/protocol level. No server business validator or handler was removed/loosened.

**Strict reference:** exact model JSON Schemas for all 19 inputs and outputs remain in generated/strict-contracts.json. Their file SHA-256 is unchanged from Bravo-3 QA: 5bafb845096b00b43e2171024b467db8023808199ca5b27fa9f8642eabc7b694. Source digests and reproducible --check protect against drift.

**Agent instructions:** tool descriptions compile field/type/enum/null/default/bound info, safely shortened structural conditions, referenced types and explicit canonical action-specific allowed/required/forbidden lists. Runtime-only conditions use verified source path and anchor annotations. The resulting text is the client-facing business contract. The flat public outputSchema is unchanged; exact nested response/error models are in the strict sidecar.

## Findings and fixes

Each row retains the initial QA claim, underlying cause, changed source and regression. The original concrete counterexamples remain in QA-BRAVO3-20261010.md and qa-bravo3-action-matrix.json.

| ID | Original finding and cause | Source correction | Regression |
| --- | --- | --- | --- |
| B1 | update lacks required non-null expected_revision; permissive role union missed canonical requirement | compact_contracts.py action_contracts derives TaskUpdateRequest mandatory fields and emits update rules; compact_annotations.json names expected_revision | test_B1_update_expected_revision_required_non_null_and_described |
| B2 | nine task_manage actions accept 33-key union but canonical extra=forbid restricts fields | action_models/action_contracts generate per-action exact allowed/required/forbidden whitelists from roles._TASK_MANAGE_MODELS and fail on action-roster drift | test_B2_all_nine_allowed_forbidden_fields_match_canonical_source, nine cases |
| B3 | old prefix archive needs archive_note contradicts valid note alternative | mcp/role_contracts.py source description corrected; TaskArchiveRequest runtime annotation requires either non-null note or archive_note | test_B3_archive_accepts_either_non_null_note_without_contradiction |
| B4 | invented live-claim release_reason condition | compact_annotations.json references core/tasks.py fallback released_by_owner; reason stays optional even for claimed task | test_B4_release_reason_is_optional_even_with_live_claim |
| B5 | obsolete task_state own-claim restriction | mcp/role_contracts.py source description corrected to any admitted agent; runtime source _action_state and set_workflow_state anchors retained | test_B5_task_state_allows_admitted_nonowner_and_no_claim_requirement |
| B6 | claim_intent null and cross-action extras unclear | action_contracts generates exact TaskClaimRequest/TaskReleaseRequest whitelists, with claim_intent required non-null for claim and optional release_reason for release | test_B6_claim_and_release_field_whitelists_and_nulls |
| B7 | description forbade field presence even when explicit null is allowed | compact_annotations.json describes non-null prohibition, send/ack/reply/read/recipients null handling, local-only delivery and feature_disabled fleet scope | test_B7_message_null_is_presence_sensitive_for_both_roles |
| B8 | task_comment missing per-action null and forbidden-extra logic | canonical TaskCommentRequest/TaskCheckpointRequest allowlists and MeshTaskCommentInput source annotation | test_B8_comment_checkpoint_nullable_extra_and_expected_revision_rejected |
| B9 | top-level if/then checked presence, downstream requires non-null | dynamic canonical non-null required lists plus specific archive alternatives, done-result, checkpoint/state/review annotations | test_B9_nonnull_runtime_requirements_expressed, six cases |
| B10 | null-vs-empty array update effect omitted | annotate handler model_dump exclude_none/exclude_unset and core/tasks update: description:null is omitted, tags:[] explicitly replaces | test_B10_none_omits_update_but_empty_array_replaces |
| B11 | strict public inputSchema risks client-side blocking and huge schema dump | compact_contracts.py transport_input_schema publishes names only and forwards all object properties; exact strict sidecar unchanged | test_B11_all_19_transport_schemas_are_nonblocking_but_server_strict_is_preserved; Bravo-3 isolated 23-error replay |
| B12 | checkpoint description falsely offered expected_revision | mcp/roles.py mesh tool description repaired; field is server-forbidden, not advertised | test_B12_checkpoint_false_expected_revision_removed_at_source |
| B13 | relation_kind open string and review_of special runtime rules omitted | compact_annotations.json source-linked storage/tasks.py review_of lane=review, parent candidate_ref, unique parent, candidate agreement/binding; no invented enum | test_B13_relation_kind_open_ended_and_review_of_runtime_gate |

No manual edits to generated catalogues. Tests verify model-derived action policy, original source strings, source anchors, null/cross-action rejection and transport forwarding.

## Complete tools/list size

Entire serialized tools/list result (name, description, inputSchema, outputSchema, annotations), canonical deterministic sorted-key JSON UTF-8. Tokens: tiktoken 0.12.0 cl100k_base; not the ChatGPT model's measured billing.

| Connector | Bytes before | Bytes after | Change | Tokens before | Tokens after |
| --- | ---: | ---: | ---: | ---: | ---: |
| Access | 1120 | 2089 | +969 | 269 | 467 |
| Executor | 16009 | 28334 | +12325 | 3678 | 6406 |
| Coordinator | 40125 | 28356 | -11769 | 9826 | 6587 |
| **Total** | **57254** | **58779** | **+1525 (+2.66%)** | **13773** | **13460 (-2.27%)** |

**Full bytes increased; no byte saving is claimed.** Per-tool and component shares are in generated/metrics.json. Source descriptions are now more complete; Coordinator task_manage schema is much smaller. Token counts are for the named tokenizer alone.

## Isolated MCP evidence

The independent Bravo-3 test runner creates an actual isolated create_app instance, compares source baseline and strictly typed output models, then applies all 19 **proposed** tool descriptions and input schemas **in memory only**, verifies full proposed list equality, and sends real HTTP JSON-RPC calls. It samples 19 invalid + 19 valid-boundary tool calls and four stress errors.

Full HTTP responses: 1016–2219 bytes, complete ToolResult objects: 981–2183 bytes; structuredContent up to 981 bytes; nested error alone up to 659 bytes. **23** error responses measured in qa-echo3-wire-metrics.json. No full strict schema or stress-secret sentinel appeared. This local result **does not prove ChatGPT pre-tool UI behavior**.

Independent original Bravo-3 source/model tests (43 checks), newly added B1–B13 assertions (28 checks) and original schema/metadata tests are run together. The strict source SHA matches QA; no handler or validator changes.

## Limits and next gates

The public inputSchema must still be an object with an argument-name map and cannot force ChatGPT or other clients to avoid hidden validation or pre-tool schema echoes. MCP has no standard declarative way to make server business validation mandatory for every client. The text is necessarily an assistant-facing description; exact nested outputs remain in sidecar and flat output schema remains published.

Required after independent offline approval: controlled integration of these exact artifacts; fresh ChatGPT test connectors for Access/Executor/Coordinator; deliberately invalid requests showing absence of huge local schema dump before tools/call; valid authorized mutations and message operations; schema consistency after new accepted output-contract changes; rollback/latency/UX/token observations. Canonical, OAuth, Events and live fleet are untouched. This commit is not an authorization to deploy.
