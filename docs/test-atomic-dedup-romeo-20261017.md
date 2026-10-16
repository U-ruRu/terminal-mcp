# Atomic test de-duplication: next tranche (2026-10-17)

## Scope and numbers

This tranche was created in an isolated worktree from canonical
'integration/M3-functional-candidate' at
'4334540871f9f20649fd6e42dc35d407f80ee817'.

- Original acceptance baseline: **2,973 collected pytest cases**, including parameters.
- Canonical before this tranche: **2,167** cases in **264** files.
- This tranche: **2,164** cases. Net **3 removed**, with no production changes.
- Acceptance threshold: **at most 1,486** cases. **678 net removals** remain.
- Full collected suite includes about **1,663 explicit test functions**, with
  **496 extra parameter instances** beyond single cases. Thus reducing all
  parameter repetitions cannot by itself meet the 50% target.
- Static AST inventory found **43 test functions over 80 lines** and no literal
  identical test function bodies; name similarity identified candidate pairs
  which require manual semantic review.

Exact node ID diff and trace inventory:
'/workspace/tmcp-test-audit-romeo-20261017/' (local evidence, not part of release).

## Changes with independently retained property owners

1. Removed
   'test_managed_identity_authority::test_fleet_provider_registry_resolves_identity_before_local_replica'.
   'test_fleet_provider_identity_resolves_without_local_slot_replica' exercises
   the same current ManagedProviderResolver.resolve remote registry route,
   and additionally verifies that missing local slot state cannot authorize
   an operation. The Fleet route path does not consult the local slot.
   'test_remote_provider_binding_uses_control_authority_without_local_replica'
   retains the separate bind route.

2. Reduced
   'test_public_errors::test_nontransient_error_policy_is_not_overridden_by_retry_safety'
   by its 'internal_error/reconcile' parameter. The retained
   'revision_conflict/reconcile' case hits the same
   retry_decision non-retry path and returns the same action. The
   'internal_error' classification and default recovery policy stay covered by
   'test_representative_registered_classes_are_bounded_and_serializable'.

3. Reduced
   'test_public_errors::test_read_named_operation_requires_actual_retry_safety'
   initially to one spelling, 'cmd.read'. A follow-up review corrected this:
   'command_read' is the current Execution tool, while 'cmd.read' is the
   compatibility operation. Both spellings are now retained in a short
   unknown-outcome regression. Explicit proof of idempotency and pre-commit
   rejection remain checked by 'test_ambiguous_mutation_is_never_blindly_retried'.
   This restoration adds one collected case relative to the historical count
   recorded above; the follow-up ledger records the aggregate current count.

4. Replaced a redundant single-case parametrize decorator on
   'test_work_window_rejection_preserves_machine_error' with its literal
   input. This has no count effect.

## Retained cases deliberately not removed

Near-name pairs about queued vs running commands, idle vs hard expiry,
migration forward vs reverse, local-message delivery with vs without a
recipient, or different invalid-input categories are distinct contract
properties, even if recorded code-path traces overlap. The current MCP
'legacy' session mode and data migrations remain supported.

Coverage/path equality is a candidate search tool, not proof that two
assertions are redundant. Protect current authorization, security,
persistence, migration, Mesh and MCP boundaries.

## Verification

- Ruff check and Ruff format check: pass on changed modules.
- Focused tests: **69 passed** (public errors and managed identity).
- Neighboring regression modules: **93 passed** (also persistent fleet,
  managed fleet routing and public error boundaries).
- pytest --collect-only -q: **2,164 collected cases**.
- A full suite was started but cancelled to avoid a prolonged high-CPU
  run during this narrow first audit. A complete green run of the
  changed 2,164-case suite has not yet been established.

No changes have been deployed to live Terminal MCP nodes.
