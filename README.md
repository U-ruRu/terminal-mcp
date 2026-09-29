# terminal-mcp

Application release: **0.10.1**. Live health responses publish the runtime application version, so operational checks do not need to infer it from historical context.

`terminal-mcp` предоставляет MCP и OpenAPI-интерфейсы для управления Linux-терминалом.

Готовый skill для установки: [`dist/terminal-operations.skill`](dist/terminal-operations.skill).

## Runtime

- Пользователь процесса: `root`.
- Пользователь терминала: `root`.
- Рабочая директория: `/`.
- Планировщик: numbered FIFO lanes, SQLite-backed.
- Параллелизм: configurable workers, по одному процессу на lane.
- Durable state: SQLite. Terminal output: отдельный disposable SQLite cache.
- ASGI workers: `1`.

Команда передаётся `/bin/bash -s` через stdin. Размер команды определяется лимитами HTTP-сервера и reverse proxy.

### Проверенный размер команды

Полный путь MCP client → `terminal-mcp` → Bash проверен с payload от 32 КиБ до 8 МиБ. Каждый тест сверял точную длину строки и контрольную метку в последних восьми символах. Максимальная проверенная команда содержала 8 388 750 символов, завершилась с `exit_code=0` и сохранила конечную метку без обрезания.

Проверенные размеры payload:

- 32, 64, 128, 256 и 512 КиБ;
- 1, 2, 4 и 8 МиБ.

Верхний лимит длины на уровне модели `run` отсутствует. Вход требует непустую строку и передаётся процессу через stdin. Результат проверки устанавливает практическую нижнюю границу активного deployment в 8 388 750 символов. Фактический верхний предел определяется доступной памятью, хранилищем и ограничениями внешнего HTTP/MCP-транспорта.

В репозитории также выполняется регрессионный тест команды размером 1,1 млн символов.

## MCP

Endpoint: `/mcp`. Набор tools остаётся компактным: `agent_start`, `coordinate`, `message`, `agents`, `agent_finish`, `context`, `tasks`, `task`, `health`, `run`, `read`, `cancel`, `recovery`.

Agent Session по умолчанию имеет idle TTL 300 секунд, task-context TTL 180 секунд и жёсткий lifetime 1500 секунд. Канонические public fields — `task_context_ttl_seconds` и `task_context_age_seconds`; `task_lease_seconds`, `task_age_seconds` и `max_task_age_seconds` сохранены как compatibility aliases. На 1200-й секунде каждый agent-bound ответ начинает содержать non-blocking warning. На 1380-й секунде Terminal MCP создаёт системный `ALERT`: он блокирует normal work surface до reply, после reply снимается и при продолжающейся сессии появляется снова через 60 секунд. Все пороги, repeat interval, включение ALERT и его текст задаются конфигурацией. Terminal commands живут независимо от Agent Session.

`message` хранит состояния `delivered -> seen -> read -> replied`. Показ сообщения отмечает только `seen`: это факт surfacing, а не acknowledgement. До явного `message(agent_id, message_hash=...)` сообщение остаётся `ACK REQUIRED`; этот вызов переводит receipt в `read`. Отправка поддерживает direct target, broadcast как отсутствующий `target` или явный alias `target="broadcast"`, и task target через `namespace + task_id`: task message snapshot-доставляется текущим live claimants и одновременно сохраняется в durable task history, поэтому остаётся видимым после смены исполнителя. После нормального `agent_finish` точный прежний `agent_id` ещё 300 секунд может только ACK/reply уже доставленного ему `message_hash`; grace не восстанавливает сессию и не открывает новые work/send операции. Sender-side inspection хранит receipt state отдельно от availability и возвращает завершившихся получателей в `inactive_recipients`. `agent_finish` при наличии обязательств остаётся разрешён и может вернуть `pending_communication` с `unacknowledged`, `reply_required` и `alerts`. Unacknowledged message продолжает показываться полным минимум три минуты и минимум пять ответов. `require_reply=true` удерживает `run` до связанного ответа. `alert=true` канонически подразумевает `require_reply=true` и дополнительно блокирует normal work surface до reply, сохраняя `message`, `health`, `cancel`, `recovery` и `agent_finish`. Автоматический late-session ALERT использует ту же state machine и повторяется по configured interval, если после снятия ALERT сессия всё ещё используется.

`run(agent_id, cmd, task_scope, queue_id?)` использует numbered FIFO lanes. `task_scope` обязателен для каждой normal run и проверяется до enqueue. Без live task claims допустим только `none`. При live claims допустимы `none`, `all` и одна собственная live claimed `namespace/task_id`; текущие legal values публикуются в `task_scope_options`. `none` выполняет команду без task event, `all` пишет command event во все текущие live claims, concrete scope — только в выбранную task. Первый run без номера выбирает least-loaded queue и запоминает affinity; следующие используют preferred queue. Явный `queue_id` выбирает lane и обновляет affinity. SQLite является источником queue state; workers используют atomic `queued -> running` claim и guarded terminal transitions.

`read` принимает `agent_id` и `cmd_hash` независимо. Обычный scoped read возвращает requested output и компактную command/queue metadata; session context добавляется при значимом warning/message/alert. Global stream имеет вид `HH:MM:SS <name> <hash> qN <output>`. Одна логическая строка output ограничена 4 MiB, весь persisted output одной команды — 8 MiB.

`agents()` работает без регистрации как observer и по умолчанию возвращает компактную картину fleet/session state. `target` выбирает одну session. `show_details`, `show_intents`, `show_commands`, `command_hash` и `since_minutes` раскрывают план, journals и исходную команду только по запросу.

`context(action=...)` хранит instance-local эксплуатационный контекст в durable DB. Запись имеет стабильный integer `id`, `summary` до 100 символов, `content` и boolean `primary`. Поддерживаются `list/create/update/delete`; delete физический. Обычный `list` возвращает `primary`/`additional` с `id + summary`, `show_details=true` добавляет `content`. Каждый успешный `agent_start` автоматически возвращает все primary entries полностью в `primary_context`; additional остаются доступными только через `context`. Health этот контекст не включает. Schema v11 добавляет таблицу `instance_context` поверх v10.

Подробный контракт: [`skills/terminal-operations/references/tool-contract.md`](skills/terminal-operations/references/tool-contract.md).

### Managed tasks

Managed task workflow является опциональным слоем поверх обычных Agent Sessions: ad-hoc terminal work продолжает работать без task card. Каждая managed task имеет обязательный `namespace`, стабильный `task_id`, одну fixed lane (`implementation`, `review`, `release`, `integration`, `general`), workflow state `ready`, `blocked`, `deferred` или `done` и явный `isolation_hint` до 160 символов. `isolation_hint` задаётся создателем только при create; `none` означает отсутствие требований к изоляции. Terminal MCP сохраняет и показывает hint без интерпретации, Git/worktree detection, рекомендаций или execution gates. `done` означает, что цель task и acceptance criteria достигнуты; завершение Agent Session само по себе task не завершает. Незаконченная работа с препятствием переводится в `blocked`.

Archive является отдельной lifecycle dimension, а не workflow state. `archive` требует непустой `archive_note`, сохраняет `archived_at` + audit evidence, освобождает live claims, исключает task из обычного backlog/pressure/recommendation и сохраняет исходный workflow state. Archived `done` остаётся completed; archived `ready|blocked|deferred` остаётся незавершённой. `show_archived=true` и direct lookup показывают lifecycle metadata. Schema v10 добавляет durable `isolation_hint` с `none` для legacy rows поверх v9; v9 детерминированно мигрирует production-style v8 `state=archived` rows, используя имеющуюся event history для восстановления предыдущего workflow state и сохраняя migration provenance при fallback.

`tasks()` — read-only observation surface. Без selector он показывает active backlog, `claimable_count`, weighted pressure и recommended next task; priority является первым ключом, внутри priority используется oldest `ready_since`. Persisted `state=ready` с open/missing dependencies проецируется как `operational_status=blocked`, получает `blocking_dependencies` и не считается claimable; live forced claim остаётся `in_progress`. Summary также показывает `oldest_claimable_ready_since` / age и `missing_dependency_count`. `namespace`/`task_id` сужают выборку, `show_details=true` раскрывает description, resources, dependencies, relations, claims, comments/history и legacy review records. `show_done=true` включает completed tasks, `show_archived=true` — lifecycle-archived tasks. `tag_counts` показывает фактически используемый custom tag vocabulary; tag filter имеет AND semantics.

Claims описывают ownership и участников. У task максимум один live owner — самый ранний live claim. `cooperative` управляет concurrent participation, а не видимостью task. При `cooperative=false` разрешён один live claim; при `cooperative=true` первый live claimant остаётся owner, остальные видны в `participants`. После release/finish/expire owner детерминированно переходит к самому раннему оставшемуся claim. Первичный `claim` требует непустой `claim_intent` до 160 символов; повторный claim того же агента обновляет intent без reclaim. Task/agent observation показывает `claimed_at`, `claim_age_seconds`, `claim_intent` и `role`.

Workflow-changing mutations existing task требуют current live owner; unclaimed task сначала необходимо claim. Owner выполняет: checkpoint, state transition, blocked/done, dependencies и cooperative changes, влияющие на ownership invariants. Participant может добавлять comments, выполнять команды через собственный `task_scope` и менять только safe metadata, определённую общим domain layer. Переход `cooperative=true -> false` при нескольких live claims отклоняется. `done` требует meaningful `result` в text или structured JSON-like форме; claimed `blocked` требует `blocker_reason`; release active claim требует `release_reason`. `release_reason` сохраняется как durable handoff history entry. Эти причины остаются в append-only history.

Универсальный `action=comment` + `comment_text` добавляет durable append-only task comment для findings, blockers, handoff, review feedback и решений. Description остаётся текущим описанием работы, comments/history — хронологией. Generic relations управляются `action=relate` / `unrelate` через `relation_kind`, `related_namespace`, `related_task_id`. Review использует kind `review_of`: review-task остаётся обычной `lane=review` task, success — `done(result=...)`, blocking findings — comments + `blocked(blocker_reason=...)`. Review completion/blocking feedback автоматически отражается событием `review_feedback` в history reviewed task с review reference, author/timestamp, outcome, findings/result и `candidate_ref` при наличии.

Dependencies образуют validated directed prerequisite graph. Self-dependency и cycles отклоняются validation error до persistent mutation. Dependency на ещё не существующую task разрешена, отображается status `missing` и остаётся hard blocking dependency. Dependency удовлетворена только normal completion semantics; archived done dependency satisfied, archived unfinished blocking. Open/missing dependency блокирует claim и любой переход в `done` с `dependency_open`. `force=true` с непустым `force_reason` является сознательным emergency override только dependency gate для claim или terminal completion, durably audited и не обходит ownership или другие safety constraints.

Task содержит generic custom `tags`; durable `state_changed_at` меняется только при workflow state transition, `ready_since` устанавливается при входе в READY и очищается при выходе. Pressure/recommendation исключают archived, done, blocked/deferred, open/missing-dependency и owned non-cooperative work; cooperative task с live participants может оставаться claimable. Explicit `run.task_scope` определяет command provenance независимо от claim intent/ownership. MCP и OpenAPI Actions используют один service/domain layer.

### Output cache и retention

Terminal output не хранится в durable database. Он пишется batch-транзакциями в отдельный disposable cache (`/var/cache/terminal-mcp/output.sqlite3` в production) и не входит в штатный backup durable state. Лимиты output-cache retention не распространяются на managed tasks, task events, claims, reviews или coordination messages: они находятся в `/var/lib/terminal-mcp/terminal-mcp.sqlite3` и входят в durable backup.

Defaults:

- максимум одной строки: 4 MiB;
- максимум persisted output одной команды: 8 MiB;
- retention target: 192 MiB logical output;
- hard retention ceiling: 256 MiB logical output;
- дополнительный ceiling: 1 000 000 строк.

При byte ceiling output самых старых завершённых команд удаляется целиком до byte target. При line ceiling около 1 000 000 строк pruning создаёт отдельный headroom примерно 100 000 строк (целевой уровень около 900 000), сохраняя целые command-output chunks; на малых test limits batch пропорционально масштабируется. Queued/running команды eviction не затрагивает. `health.terminal.output_cache` показывает logical/allocated bytes, строки, retained/truncated commands и время последнего prune. Schema v5 переносит legacy `lines` из durable DB в cache, удаляет исторический duplicate index и один раз compact'ит durable DB через `VACUUM`.

## OpenAPI Actions

Schema: `/openapi.json`. Actions используют тот же service layer и те же Agent Session semantics:

- `POST /actions/agent/start`
- `POST /actions/coordinate`
- `POST /actions/message`
- `POST /actions/agents`
- `POST /actions/agent/finish`
- `POST /actions/context` с тем же instance-local Context контрактом
- task observation/mutation Actions с теми же контрактами, что `tasks`/`task` в MCP
- `POST /actions/run`
- `POST /actions/recovery`
- `POST /actions/read`
- `POST /actions/cancel`
- `GET /actions/health`

Все published Actions содержат `x-openai-isConsequential: false`. Все MCP tools намеренно публикуют `readOnlyHint=true`, `destructiveHint=false` и `openWorldHint=false`, чтобы агент мог вызывать terminal/workflow операции без per-call confirmation. Это UI/consent-классификация; фактические side effects команд остаются частью контракта самих tools.

## Авторизация

Режимы интерфейсов задаются независимо:

```env
TERMINAL_MCP_MCP_AUTH_MODE="oauth"
TERMINAL_MCP_ACTIONS_AUTH_MODE="bearer"
```

Поддерживаемые режимы: `none`, `bearer`, `oauth`.

OAuth поддерживает Authorization Code, PKCE S256, Dynamic Client Registration, refresh token rotation и JWT access tokens.

Metadata:

- `/.well-known/oauth-authorization-server`
- `/.well-known/oauth-protected-resource`

Endpoints:

- `POST /oauth/register`
- `GET /oauth/authorize`
- `POST /oauth/authorize`
- `POST /oauth/token`

Agent-facing OAuth advertises `terminal:read` and the ChatGPT compatibility scope `terminal:execute`. All published terminal and workflow operations intentionally remain in one consent tier and are authorized by `terminal:read`; `terminal:execute` does not grant a separate privilege.

### Browser Console transport

`origin(TERMINAL_MCP_PUBLIC_BASE_URL)` is always allowed for browser Console requests. Additional static Console origins are configured as an exact comma-separated allowlist:

```env
TERMINAL_MCP_CONSOLE_ALLOWED_ORIGINS="https://console.example.invalid,https://ops.example.invalid"
```

Only explicit `http`/`https` origins are accepted; wildcard, path, query and fragment values fail closed. CORS/Origin enforcement covers `/connect`, `/pairing/exchange`, `/oauth/token`, `/actions/*` and `/console/*`. Non-browser clients without an `Origin` header remain supported.

The local `terminal-mcp pair` command is the only pairing-issuance surface. It places the one-time secret in the `/connect#...` URL fragment; `/connect` rejects query material and browser bootstrap/token responses are `no-store` with no-referrer and framing/content hardening.

## Admin UI

Admin UI: `/admin`.

Функции:

- настройка CWD;
- настройка пользователя терминала;
- настройка команды, выполняемой методом `health`;
- создание и удаление Bearer credentials;
- создание и удаление OAuth logins;
- просмотр полных Bearer-токенов, логинов и паролей;
- просмотр и отзыв OAuth clients.

Управляемые credentials сохраняются в env-файле с правами `0600`. Изменения применяются к новым запросам и командам.

Admin UI использует CSRF-токены для формы входа и всех изменяющих операций. Защита от перебора действует для Admin login, OAuth authorize, OAuth token и Dynamic Client Registration.

## Публичные ресурсы

- Privacy policy: `/privacy`.
- OpenAPI schema: `/openapi.json`.
- Liveness probe: `/health/live`.

## Защита хранилища

Durable SQLite содержит полные тексты команд, Agent Session/coordination state, managed tasks/claims/reviews/history, OAuth clients, коды авторизации и refresh tokens. Terminal output хранится отдельно в disposable output-cache. Оба файла могут содержать чувствительные данные.

- Каталог данных создаётся и восстанавливается с режимом `0700`.
- Durable SQLite и output-cache создаются с режимом `0600`.
- Каталог резервных копий имеет режим `0700`.
- Каждый SQLite backup получает режим `0600`; установщик также нормализует права существующих backup-файлов.
- Storage layer повторно применяет режимы при запуске, поэтому обновление и ручная замена файла не оставляют базу доступной другим локальным пользователям.

## Установка

```bash
sudo TERMINAL_MCP_ADMIN_USERNAME="operator" \
  TERMINAL_MCP_ADMIN_PASSWORD="change-me" \
  TERMINAL_MCP_PUBLIC_BASE_URL="https://server-a.example.invalid" \
  ./deploy/install.sh install
```

Проверка:

```bash
sudo ./deploy/install.sh doctor
```

`doctor` проверяет локальный liveness и публичный fleet ingress. Публичный origin обязан
проксировать `/internal/fleet/*` в Terminal MCP; unauthenticated probe
`/internal/fleet/identities` должен доходить до приложения и отвечать `401`. `404` означает,
что reverse proxy/path allowlist скрывает обязательный mesh route. Сам route остаётся
защищённым fleet signing/auth и не должен обходить application auth.

Обновление:

```bash
sudo ./deploy/install.sh update
```

Установщик создаёт версионированный virtualenv, env-файл, systemd unit, защищённое durable SQLite-хранилище и отдельный cache directory. Перед обновлением создаётся backup только durable SQLite; output-cache является disposable и в backup не входит. Staging считается завершённым только после успешной установки package и проверки executable `bin/terminal-mcp`; incomplete release удаляется до переключения `current`. Health check подтверждает активацию release. Ошибка health check активирует предыдущий release. Пути установки, systemd command и health URL поддерживают переопределение переменными `TERMINAL_MCP_INSTALL_ROOT`, `TERMINAL_MCP_ENV_DIR`, `TERMINAL_MCP_DATA_DIR`, `TERMINAL_MCP_CACHE_DIR`, `TERMINAL_MCP_BACKUP_DIR`, `TERMINAL_MCP_UNIT_FILE`, `TERMINAL_MCP_SYSTEMCTL`, `TERMINAL_MCP_HEALTH_URL` и `TERMINAL_MCP_ACTIVATION_HEALTH_TIMEOUT_SEC`. Activation health timeout по умолчанию 600 секунд, чтобы one-time storage migration успевала завершиться на больших legacy databases.

## Пути

- Приложение: `/opt/terminal-mcp`.
- Конфигурация: `/etc/terminal-mcp/terminal-mcp.env`.
- Durable data: `/var/lib/terminal-mcp/terminal-mcp.sqlite3`.
- Output cache: `/var/cache/terminal-mcp/output.sqlite3`.
- Backup: `/var/backups/terminal-mcp`.

## Repository privacy boundary

Public examples and fixtures are environment-neutral. Use server-a/server-b/server-c,
example.invalid origins and placeholder credentials in committed material. Real deployment
identity and operational evidence stay outside Git. Commit metadata is part of this boundary. See docs/REPOSITORY_PRIVACY.md.

Validate the public example boundary with:

    python3 scripts/check_repository_privacy.py

## Разработка

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[test]'
.venv/bin/ruff check src tests
.venv/bin/pytest -q
```

### Console transport smoke client

Create a one-time local pairing URL with terminal-mcp pair, then exercise pair -> snapshot -> WebSocket end-to-end:

    python scripts/console_transport_smoke.py --pair-url 'https://server-a.example.invalid/connect#ONE_TIME_SECRET'

The client exchanges the fragment secret, fetches /actions/console/snapshot, issues a short-lived single-use WebSocket ticket, and subscribes from snapshot high_water_seq. Pass --since 0 to exercise replay; if retention caused a journal gap, the client handles resync_required by fetching a fresh snapshot, issuing a new one-use ticket, and reconnecting. It never prints pairing, access, refresh, or WebSocket ticket secrets.
