| Tool | Fields | Strict required | Input model | Output model | Text fields & required | Transport | Flat output |
|---|---:|---|---|---|---|---|---|
| access.session | 1 | action | PASS | PASS | PASS | PASS | PASS |
| executor.session | 2 | session_number | PASS | PASS | PASS | PASS | PASS |
| executor.task_list | 6 | none | PASS | PASS | PASS | PASS | PASS |
| executor.command_run | 3 | command | PASS | PASS | PASS | PASS | PASS |
| executor.command_read | 3 | none | PASS | PASS | PASS | PASS | PASS |
| executor.command_cancel | 1 | cmd_hash | PASS | PASS | PASS | PASS | PASS |
| executor.command_recovery | 1 | command | PASS | PASS | PASS | PASS | PASS |
| executor.task_claim | 6 | action, namespace, task_id | PASS | PASS | PASS | PASS | PASS |
| executor.task_state | 3 | namespace, state, task_id | PASS | PASS | PASS | PASS | PASS |
| executor.task_comment | 5 | namespace, task_id | PASS | PASS | PASS | PASS | PASS |
| executor.message | 13 | action | PASS | PASS | PASS | PASS | PASS |
| coordinator.session | 2 | session_number | PASS | PASS | PASS | PASS | PASS |
| coordinator.task_get | 6 | namespace, task_id | PASS | PASS | PASS | PASS | PASS |
| coordinator.task_list | 6 | none | PASS | PASS | PASS | PASS | PASS |
| coordinator.task_manage | 33 | action, namespace | PASS | PASS | PASS | PASS | PASS |
| coordinator.task_graph | 6 | namespace, task_id | PASS | PASS | PASS | PASS | PASS |
| coordinator.agent_observe | 0 | none | PASS | PASS | PASS | PASS | PASS |
| coordinator.message | 13 | action | PASS | PASS | PASS | PASS | PASS |
| coordinator.health | 1 | none | PASS | PASS | PASS | PASS | PASS |
