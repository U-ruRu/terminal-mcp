| Tool | Fields | Strict required | Description delta | Source validation | Description / runtime verdict |
|---|---:|---|---|---|---|
| `access.session` | 1 | action | Unchanged | PASS | PASS offline |
| `coordinator.agent_observe` | 0 | none | Unchanged | PASS | PASS offline |
| `coordinator.health` | 1 | none | Unchanged | PASS | PASS offline |
| `coordinator.message` | 13 | action | Unchanged | PASS | PASS offline |
| `coordinator.session` | 2 | session_number | Unchanged | PASS | PASS offline |
| `coordinator.task_get` | 6 | namespace, task_id | Unchanged | PASS | PASS offline |
| `coordinator.task_graph` | 6 | namespace, task_id | Unchanged | PASS | PASS offline |
| `coordinator.task_list` | 6 | none | Unchanged | PASS | PASS offline |
| `coordinator.task_manage` | 33 | action, namespace | Repaired | PASS | PASS offline |
| `executor.command_cancel` | 1 | cmd_hash | Unchanged | PASS | PASS offline |
| `executor.command_read` | 3 | none | Unchanged | PASS | PASS offline |
| `executor.command_recovery` | 1 | command | Unchanged | PASS | PASS offline |
| `executor.command_run` | 3 | command | Unchanged | PASS | PASS offline |
| `executor.message` | 13 | action | Unchanged | PASS | PASS offline |
| `executor.session` | 2 | session_number | Unchanged | PASS | PASS offline |
| `executor.task_claim` | 6 | action, namespace, task_id | Repaired | PASS | PASS offline |
| `executor.task_comment` | 5 | namespace, task_id | Repaired | PASS | PASS offline |
| `executor.task_list` | 6 | none | Unchanged | PASS | PASS offline |
| `executor.task_state` | 3 | namespace, state, task_id | Repaired | PASS | PASS offline |
