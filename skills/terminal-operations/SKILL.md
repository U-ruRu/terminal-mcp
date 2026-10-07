---
name: terminal-operations
description: Управляет и диагностирует Linux-сервер через Terminal MCP: health, sessions, managed tasks, messaging, commands, context и recovery.
compatibility: Terminal MCP 0.13.1; MCP tools session, observe, message, task, cmd, context, health.
metadata:
  author: U-ruRu
  version: "2.0.0"
  language: ru
---

# Terminal operations

## Цель

Выполняй серверные задачи через текущий семиинструментный Terminal MCP контракт и фактический контекст сервера.

## Базовый цикл

1. Вызови `health`.
2. Открой WorkSession через `session(action="start", ...)`.
3. Прочитай релевантный instance context через `context(action="list")`.
4. Прочитай managed work через `observe(subject="tasks", ...)`.
5. Возьми карточку через `task(request={action:"claim", ...})` при managed workflow.
6. Выполняй команды через `cmd(request={action:"run", ...})`.
7. Дочитывай queued/running command через `cmd(request={action:"read", ...})`.
8. Фиксируй findings через `task(... action="comment" ...)` или checkpoint/state mutation.
9. Обрабатывай inbox через `message`.
10. Заверши WorkSession через `session(action="end", ...)`.

## Session

Provider-managed connector использует server-resolved provider identity. Persistent Access slot поддерживает initial compatibility binding. `mode="legacy"` создаёт временный Access slot.

Следи за `hard_expires_at`, session warnings, alerts и текущим `session_epoch`. Завершай рабочий этап в безопасной точке до hard expiry.

## Observe

`observe` читает:

- `sessions`;
- `tasks`;
- `namespaces`.

Используй `detail="summary"` для ориентации. Используй `detail="full"` для точечного текущего состояния. Коллекции читай через `limit` и opaque `cursor`.

## Managed tasks

Task actions:

- `create`;
- `claim`;
- `release`;
- `update`;
- `checkpoint`;
- `comment`;
- `relate`;
- `unrelate`;
- `state`;
- `done`;
- `archive`;
- `review`.

Состояния: `ready`, `in_progress`, `blocked`, `deferred`, `done`.

Primary claim создаёт ownership и атомарно переводит claimable ready work в `in_progress`. `claim_intent`, `result`, `blocker_reason`, `release_reason` и review evidence сохраняют durable handoff context.

Dependency override использует `force=true` вместе с содержательным `force_reason` и создаёт audit evidence.

Командную provenance связывай через `task_scope`: `none`, `all` или конкретный `namespace/task_id` из текущих `task_scope_options`.

## Commands

`cmd` actions:

- `run` — numbered FIFO execution;
- `read` — bounded output page и command status;
- `cancel` — cancellation конкретной команды;
- `recovery` — emergency execution path.

`run` может вернуть завершённый результат вместе с первой bounded output page. Queued/running результат продолжай через `read`.

Используй ограниченный вывод и точечные команды. Проверяй итоговый status и exit code.

## Messaging

`message` поддерживает inbox, history, send, ACK, reply и alert.

Recipient lifecycle: `delivered → seen → read → replied`.

Task-addressed message использует `namespace + task_id` и сохраняет durable task history reference.

## Context

`context` actions: `list`, `create`, `update`, `delete`.

Primary entries содержат основной instance context. Additional entries содержат вспомогательный operational context.

## Health

`health` показывает application version, storage, auth mode, terminal runtime, scheduler, queues, command activity, output-cache и workflow summary.

Используй `health` как первый и финальный operational check.

## Repository work

1. Проверь `git status` и worktrees.
2. Определи canonical branch из project context.
3. Создай отдельную task branch и worktree.
4. Сохрани чужие рабочие изменения.
5. Выполни focused tests и lint.
6. Подлей свежий canonical в task branch.
7. Реши конфликты в task branch.
8. Повтори проверки.
9. Интегрируй reviewed candidate в canonical.

## Safety

- Сохраняй секреты вне вывода команд и отчётов.
- Используй штатные service/deployment entry points.
- Сохраняй durable state при rollout/rollback.
- Проверяй права, владельцев, health и журналы после инфраструктурных изменений.
- Ограничивай destructive operations прямой целью текущей задачи.

Полный контракт: [references/tool-contract.md](references/tool-contract.md).
