# Connector runtime contract

Executor and Coordinator publish argument names, defaults and compact usage hints. Their input schemas accept argument values and extra fields. Runtime models validate types, bounds, supported fields and action prerequisites before invoking application mutations.

Handled application failures use MCP `isError: false` and a structured result:

```json
{"ok":false,"error":{"code":"input_validation_failed","message":"Correct the indicated request field.","details":{"validation_errors":[{"error_class":"missing","path":"command","description":"Provide this required field."}]},"outcome":"not_committed","retry":"repair","reason":"missing_required","path":"command"}}
```

`structuredContent` contains the object. The text fallback contains the same object encoded once as JSON. Output schemas cover successful results and structured failures. The legacy adapter retains its flat error envelope.

`error.code`, `error.outcome` and `error.retry` describe the result and recovery. Session lifecycle failures select `start_session`; optimistic concurrency failures expose `revision_conflict` and `error.details.current_revision`. Uncertain failures select reconciliation. Validation issues contain bounded schema-owned paths and corrective descriptions. Private exception diagnostics remain in service logs.

Task record revisions track field, checkpoint and output-state changes. Claim, release, review and relation operations enforce a supplied `expected_revision` inside their write transaction. Their side-stream records retain independent event identities. Comments append to history and accept the comment text without a revision argument. A `review_of` relation may update the associated candidate and task revision.

Task checkpoint, result, history payload, review evidence and warning extensions contain native JSON values. Graph nodes share `namespace`, `task_id` and nullable `state`.

Infrastructure health is available before session creation. Its `ok` field confirms collection of the diagnostic result; `healthy`, `status` and component states describe infrastructure health. Extended role health includes bounded provider field names, identity fingerprints and resolution status. Successful resolution supplies the public agent name. Host paths, privilege details and custom health-command output remain outside the role health projection.

Provider bindings use the provider, subject and conversation identity. Identical evidence resolves consistently across endpoint roles and nodes. Distinct provider evidence remains distinct; verified authorization and ownership checks apply independently. Role session-start receipts include the same safe provider fingerprints. Extended health fingerprints support comparison through connected clients without publishing raw provider identifiers.
