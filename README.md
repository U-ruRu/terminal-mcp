# terminal-mcp

`terminal-mcp` — Python-сервис управляемого доступа к Linux-терминалу через MCP, HTTP Actions и Console.

Текущая версия приложения: **0.14.4**. В публичных контрактах Executor и Coordinator операция `session` поддерживает только `attach` с обязательным `session_number`. Основной контракт: **Distributed Multi-Issuer Access Mesh V2**.

## Репозиторий и область релиза

Репозиторий: `https://github.com/U-ruRu/terminal-mcp.git`. Разработка и интеграция выполняются на Secondary: `/workspace/terminal-mcp`, canonical worktree `/workspace/terminal-mcp-integration`, ветка `integration/M3-functional-candidate`. Python `>=3.11`; FastAPI, Uvicorn, MCP SDK, Pydantic и SQLite/aiosqlite. Версия определяется в `src/terminal_mcp/version.py`.

Текущие цели деплоя и живой приёмки — **FirstByte и BacLOUD**. На каждом сервере доступны три первичных коннектора: Access, Executor и Coordinator. Вместе это **шесть коннекторов**. Secondary используется для исходников, тестов и координации; развёрнутые Main и Tokyo сохраняются. Наличие версии в документации описывает контракт исходников, а подтверждение установленного релиза фиксируется отдельно после живой приёмки.

## Public MCP

Access endpoint: `/terminal-mcp/access/v1/mcp`.

Access catalog: `session`.

Executor endpoint: `/terminal-mcp/executor/v1/mcp`.

Executor catalog: `session`, `task_list`, `command_run`, `command_read`, `command_cancel`, `command_recovery`, `task_claim`, `task_state`, `task_comment`, `message`.

Coordinator endpoint: `/terminal-mcp/coordinator/v1/mcp`.

Coordinator catalog: `session`, `task_get`, `task_list`, `task_manage`, `task_graph`, `agent_observe`, `message`, `health`.

Access выдаёт или активирует слот своего issuer. Executor выполняет команды и ограниченные операции над задачами. Coordinator управляет задачами, графом, наблюдением и health. Каталоги остаются раздельными: операторское управление слотами выполняется через HTTP/Console, shell-команды — через Executor.

### Начало работы

На Access вызови `session(action="start")`: успешный ответ содержит только `ok: true` и четырёхзначный `session_number`. Сервер проверяет доступность номера в Mesh; при сетевом разделении выдача остаётся локально доступной. Завершение сессии — `session(action="end")`. Операторские persistent-слоты управляются через HTTP/Console.

На каждом нужном Executor/Coordinator один раз выполни `session(action="attach", session_number=...)`. Источник номера и текущая идентичность разрешаются сервером через Mesh. Повторное attach сохраняет привязку; подмена номера в существующей привязке возвращает `access_mesh_binding_conflict`.

**Role session = attach-only.** Управление началом и завершением issuer-сессии выполняется на Access. После attach доменные операции получают identity из доверенного контекста коннектора; Session Number повторно в команды, задачи и сообщения не передаётся. Чтение известного `cmd_hash`, в том числе созданного другим агентом, доступно без Session Number и предварительного attach. `command_read` без `cmd_hash` возвращает локальный журнал команд всех LogicalAgent с opaque `next_cursor`. Настроенная транспортная аутентификация сохраняется и для чтения без номера сессии.

Первичная последовательность: Access выдаёт слот → нужные роли выполняют attach → Executor получает задачу и делает claim → явный `task_state(state="in_progress", ...)` отмечает начало работы → команды и проверка вывода → checkpoint/result → явное завершение задачи. Завершение issuer-сессии передаётся исходному Access; отдельного уведомления каждого execution-сервера о завершении работы нет.

### Identity, время и владение

Slot kinds: `legacy`, `persistent`. Kind задаётся при создании и остаётся неизменным. Mobile/Console — операторские интерфейсы этих же двух видов слотов.

LogicalAgent и его `public_name` одинаковы на обоих серверах и в обеих рабочих ролях. Каждый execution-сервер имеет локальный WorkSession, epoch, часы и write gate. Grant, policy, deadline и события issuer реплицируются; локальная операция проверяет локальное состояние и не запрашивает разрешение issuer по сети на каждый вызов. При разделении сети уже полученные ограничения и deadlines продолжают действовать; новое изменение issuer становится известно peer после доставки или catchup.

Policy определяет duration, cooldown, rearm и warning/draining. Вход в новый цикл использует новый локальный WorkSession; повторный attach и обычный вызов не продлевают текущий deadline. Legacy claims освобождаются после end/expiry; persistent claims при end/expiry освобождаются при `release_on_end=true`, иначе сохраняют durable ownership. Suspend/delete отзывают доступ и запускают очистку. Сначала выполняется fencing/дренирование исполнения, затем освобождение claims; незавершённая очистка видна как `cleanup_pending`.

**Claim lifecycle сохраняет task state, checkpoint и result.** Claim, release и cleanup меняют владение. Изменение state выполняется явно через `task_state` или Coordinator `task_manage(action="state", ...)`; `task_manage(action="update", ...)` обновляет свойства и сохраняет state. Стартовое состояние задаёт create. Executor `task_comment` поддерживает `action="comment"` (по умолчанию) и `action="checkpoint"`; checkpoint также сохраняет состояние. Review и его audit фиксируются одной транзакцией; committed mutation receipts содержат записанные state/revision/result даже при последующем сбое чтения.

## Сообщения

Обе роли предоставляют `message`: `recipients`, `send`, `read`, `ack`, `reply`, `history`. Выбирай адресата по `public_name`; broadcast задаётся отсутствием target или `target="broadcast"`. Task addressing использует пару `namespace` + `task_id`.

`scope="local"` ограничивает доставку текущим сервером. `scope="fleet"` — значение по умолчанию: локальная доставка и durable outbox фиксируются до обращения к peer. Рассылка исключает отправителя, повторные LogicalAgent и истёкшие локальные сессии. Недоступный peer даёт честный committed receipt со state `queued`/`partial`, `pending_peers` и доступной диагностикой; worker повторяет доставку после восстановления. Потерянный ACK и рестарт обрабатываются идемпотентно.

Inbox, history и ACK используют локальные данные. Notify отмечается прочитанным при успешной выдаче страницы; mode `ack` требует подтверждения, mode `alert` требует ответа. ACK не заменяет reply для alert. Reply и снятие локального обязательства фиксируются атомарно; ответ направляется на сервер исходного сообщения. Команды учитывают эти обязательства через общий локальный gate.

## Runtime contract и диагностика

Inputs use **permissive planning schemas**. Обязательность, типы, bounds и action-specific правила проверяются runtime до мутации. Полные условия create/archive сохраняются в аннотациях task planning. Runtime output models остаются строгими.

Handled application errors: MCP `isError: false`, `structuredContent.ok: false`, объект `error` с `code`, `details`, `outcome`, `retry`. Текстовый fallback содержит тот же JSON. `ok=true` в health подтверждает сбор диагностики; здоровье описывают `healthy`, `status` и компоненты. Coordinator `health` доступен до attach. Секреты и raw provider identifiers исключены из публичной диагностики.

Нормализованный payload и доверенный caller/session/epoch/request ID формируют автоматический task replay key. Разные мутации при повторяющемся JSON-RPC ID различаются; точный retry возвращает сохранённый receipt. Для shell-команды повторяющийся ID `0` сам по себе не обеспечивает exactly-once: перед повторным запуском с побочными эффектами проверь receipt и `cmd_hash`.

## Архитектура и хранилища

```text
MCP / HTTP Actions / Console / authenticated Fleet
                  ↓ ActorContext
      transport-independent Application API
                  ↓
local SessionGate · tasks · messages · command scheduler
                  ↓
SQLite repositories · grant/event outbox · ExecutionPort
                  ↓
             in_process | Unix executor
```

Исходники: `application/` — use cases; `core/` — доменные правила; `mcp/`, `http/`, `fleet/` — адаптеры; `storage/` — транзакции; `terminal/` — исполнение. API управляет очередями, command metadata и admission. В split topology `terminal-mcp-executor.service` запускает shell через `/run/terminal-mcp/executor.sock`; `terminal-mcp.service` обслуживает API.

| Данные | Путь |
| --- | --- |
| Runtime, tasks, sessions, native messages, mesh replicas/outboxes | `/var/lib/terminal-mcp/terminal-mcp.sqlite3` |
| Auth principals, clients, credentials | `/var/lib/terminal-mcp/auth.sqlite3` |
| Fleet control | `/var/lib/terminal-mcp/fleet-control.sqlite3` |
| Ограниченный кэш вывода | `/var/cache/terminal-mcp/output.sqlite3` |
| Конфигурация | `/etc/terminal-mcp/terminal-mcp.env` |
| Релизы / резервные копии | `/opt/terminal-mcp` / `/var/backups/terminal-mcp` |

MCP/Actions поддерживают `none`, `bearer`, `oauth` согласно конфигурации. OAuth metadata: `/.well-known/oauth-authorization-server`, `/.well-known/oauth-protected-resource`. Диагностика и контракты: `/health/live`, `/openapi.json`; Console: `/admin`; privacy: `/privacy`.

## Deployment и development

Подготовку peer authentication, proof key, резервных копий, staging и шестиконнекторную приёмку выполняй по [Access Mesh deployment](docs/access-mesh-deployment.md). Installer `deploy/install.sh update` сам выполняет staging, compatibility checks, backup и activation; `stage` не является отдельной публичной командой installer.

```bash
sudo ./deploy/install.sh install
sudo ./deploy/install.sh update
sudo ./deploy/install.sh doctor
```

Выбирай нужную операцию отдельно после её проверок. Split topology имеет собственные render/check/activate/rollback: [split-service guide](docs/architecture-split-service-cutover.md). Topology rollback и восстановление durable/schema/security state — разные процедуры.

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[test]'
.venv/bin/ruff check src tests
.venv/bin/pytest -q
python3 scripts/check_repository_privacy.py
```

## Legacy compatibility

Endpoint: `/mcp`.

Legacy compatibility catalog: `session`, `observe`, `message`, `task`, `cmd`, `context`, `health`.

Legacy `/mcp` сохраняет совместимость со старыми клиентами, включая их session start/end/interrupt и Session Number binding. Этот контракт отделён от первичных Access/Executor/Coordinator V2 и не задаёт порядок работы новых коннекторов.

## References

- [Architecture](docs/ARCHITECTURE.md) и [runtime contract](docs/connector-runtime-contract.md).
- [Tool reference](skills/terminal-operations/references/tool-contract.md), source skill `skills/terminal-operations/SKILL.md`, архив `dist/terminal-operations.skill`.
- `src/terminal_mcp/mcp/access_mesh_schema_baselines_v2.json` — зафиксированные эффективные discovery schemas.
- `PROJECT_CONTEXT.md` — локальный операторский контекст, исключённый из Git.
