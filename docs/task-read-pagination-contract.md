# Role task reads: selection and cursor contract

Applies to Executor/Coordinator `task_list` and Coordinator `task_get(detail="history")` in
Terminal MCP role endpoint v1. This describes the effective runtime behavior in the
task-history cursor correction candidate.

## `task_list`: visible task set

The declared defaults are `namespace=null`, `lane=null`, `state=null`,
`tags=null`, `limit=20` and `cursor=null`. The role adapter always uses
`show_archived=false`; there is no `show_archived` argument on the role tool.

| Input | Selected tasks |
| --- | --- |
| No explicit filters | All non-archived tasks **except** `state=done` |
| `state="done"` | Only **non-archived** completed tasks |
| Another explicit `state` | Only non-archived tasks matching that exact state |
| `namespace=null` | All namespaces in the local task registry |
| `namespace="X"` | Only tasks in namespace X |
| `lane="L"` | Only tasks in lane L |
| `tags=["a","b"]` | Tasks carrying **both** tags (AND) |

Explicit filters are cumulative. There is no role `task_list` option to include
archived records. A known archived task remains retrievable with Coordinator
`task_get(namespace=..., task_id=...)`.

The absence of a non-archived `done` task from an unfiltered list is **expected**:
the role adapter's default `show_done=false` deliberately focuses the work queue
on unfinished work. Previous one-sentence public descriptions did not explain
that exclusion; both role tool descriptions now state it.

`task_list` currently uses a query-scoped offset cursor. It is suitable for
traversing a stable result set. The cursor is not a database snapshot guarantee:
if tasks are inserted, removed or reordered while traversing, restart the list
from `cursor=null`. Mutation-resistant keyset/snapshot paging for the general
task list is separate from the history correction below.

## `task_get(detail="history")`: selected stream

`namespace` and `task_id` are required. `history_kind` selects exactly one
stream: `events` (default), `comments`, `reviews` or `output_states`.
The default page size is 20 and the permitted `limit` range is 1–100; an output
budget may yield a shorter page. Follow the returned opaque `next_cursor`
until it is absent/null; an empty or final page has no successor.

- `events` are ordered by descending durable event ID (newest first).
  `comments` are derived from that order and include `comment` and
  `review_feedback` events.
- `reviews` are ordered by review timestamp and dimension; `output_states`
  by ascending output-state ID.
- The cursor is bound to namespace, task ID, stream and a canonical fingerprint
  of the **selected stream's contents in its current order**. Unrelated task
  metadata edits do not invalidate a history page. Adding or changing an event
  in that stream does.
- Reusing a cursor with a changed stream, other task/namespace/stream, malformed
  encoding or an out-of-range offset returns the structured error
  `{"ok":false,"code":"invalid_cursor",...}`. Restart from `cursor=null`.
  Successful reads never silently repeat an already returned page after an
  invalidating history change.
- The history read path scans complete event history in durable ID batches
  (1,000 per storage fetch), removing the prior implicit newest-100 cutoff.
  The ordinary full/detail task view retains its bounded event preview.
- Task state changes preserve the **content revision** as documented in
  `connector-runtime-contract.md`. Cursor validity is intentionally independent
  of that revision: state changes can append audit events, which modify history.

Cursor consistency is evaluated between reads; concurrent writes while a single
multi-query full task projection is being assembled are not an atomic,
cross-stream snapshot. Direct history storage queries are ordered, but the
remaining race window requires explicit live concurrency QA before a global
snapshot guarantee can be claimed.

## Relevant implementation and regression tests

- `src/terminal_mcp/mcp/roles.py`: role defaults, selected-stream cursor fingerprint
- `src/terminal_mcp/application/observations.py`, `core/service.py`,
  `core/tasks.py`: Coordinator-only complete history read path
- `src/terminal_mcp/storage/tasks.py`: durable event order and batched `before_id`
- `tests/test_task_history_cursor_stability.py`: unchanged pages, new event,
  changed payload, wrong stream, empty/final pages, SQLite history >100,
  explicit role filters
