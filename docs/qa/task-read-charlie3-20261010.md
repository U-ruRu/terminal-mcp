# QA: task_list defaults and task_get history cursor — Charlie-3

Date: 2026-10-10 UTC. Environment: isolated Secondary worktree, base
`integration/M3-functional-candidate` SHA
`bde0dec8bd0679c3cbda768104672de57956b6cf`.
No live writes, cleanup or deployment.

## Observation classification

| LIVE-021-20261010-A observation | Verdict | Contract / evidence |
| --- | --- | --- |
| FB-B did not see a non-archived done task in unfiltered `task_list` | **Expected default; public description ambiguous** | Role `_task_list` passes `show_done=(state=="done")` and `show_archived=False`. Storage excludes done when show_done is false and archived always. Explicit `state="done"` returns the non-archived done subset. |
| Four events repeated after changing a task between `task_get(history)` pages | **Confirmed server defect** | Previous cursor was a query-scoped absolute offset, while `work_events` is sorted `id DESC`. A new event prepended to the stream shifts offsets and repeats previously returned IDs. |
| History older than last 100 events | **Additional confirmed read-path defect** | `TaskCoordinator._decorate` used `list_events(limit=100)` even for paginated history, causing unreachable older records. |

## Change and exact semantics

- `task_list` logic unchanged; descriptions for both role tools now expose the
  default and explicit filters. `state`, `namespace`, `lane`, `tags` combine
  conjunctively; only non-archived tasks are visible. `state="done"` selects
  non-archived completed tasks.
- `task_get(detail="history")` now binds cursors to the canonical fingerprint
  of the selected stream, including ordering and content. Any addition/edit to
  that stream invalidates an issued cursor and returns `ok:false`,
  `code:"invalid_cursor"`; the caller must restart without a cursor.
- Coordinator history reads can retrieve the complete event stream in batches
  of up to 1,000 from SQLite. Ordinary detail previews retain their existing
  100-event limit.
- Content-revision/state ownership rules were not changed; explicit status
  changes remain possible for any admitted agent without a content revision
  increment.

## Validation

- New `tests/test_task_history_cursor_stability.py`: 12 tests pass. Stable
  cursor with unique ordered IDs, append and payload edit invalidation,
  recovery by restarting, wrong stream, invalid offset, empty/final pages,
  metadata-only/other-stream stability, review mutations, and SQLite 115-event
  full traversal plus state/archived/namespace/lane/tag selection.
- `test_role_task_get_bounded_review.py`, `test_task_mcp_contract.py`,
  `test_task_snapshot_contract.py`, `test_task_storage.py`,
  `test_task_projection_core.py`: **122 passed**.
- `test_role_mcp_v1.py`, `test_role_output_planning.py`,
  `test_role_tool_annotations_review.py`, `test_role_wire_runtime.py`,
  `test_public_operation_metadata.py`, `test_mcp_output_contracts.py`,
  `test_documentation_contract.py`: **124 passed**.
- Ruff check and format checks for touched Python modules pass.
- Combined wider targeted run: **282 passed, 1 failed**, where
  `test_all_seven_legacy_discovery_contracts_preserve_baseline` detected a
  `session` discovery hash mismatch only after other tests. The exact test
  **passes in isolation** on this candidate. Treat as test-order/dependency
  investigation, not a clean full-suite pass.
- Full pytest suite was started but deliberately cancelled after about 3%
  progress because it cannot finish within the bounded session.

## Boundaries and remaining QA

- No FirstByte/BacLOUD live mutation, cleanup or deployment.
- Live receipts TASK-7D3B5EC33410DCE4ABED08E5 were supplied by FB-B;
  the exact original ChatGPT conversation transcript was not independently
  fetched from the supplied link.
- No concurrent live writer was used for the SQLite test. Simulated between-read
  inserts and edits are deterministic; cross-process writes between the
  selected-stream query and projection, and cross-stream atomicity, remain
  untested.
- `task_list` retains query-scoped offset pagination, not immutable snapshots.
  Task-list reordering during paging still requires a restart; keyset/snapshot
  paging for this separate list is not part of the verified history fix.
- Each history read materializes the whole selected stream to compute its
  fingerprint. Extremely long histories need a follow-up scalability review
  (streaming snapshot/keyset implementation) before claiming bounded memory.
- The complete fleet acceptance and any deployment require integration and
  separate live QA.

See `docs/task-read-pagination-contract.md` for the public selection and
cursor rules.
