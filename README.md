# terminal-mcp

`terminal-mcp` предоставляет MCP и OpenAPI-интерфейсы для управления Linux-терминалом.

Готовый skill для установки: [`dist/terminal-operations.skill`](dist/terminal-operations.skill).

## Runtime

- Пользователь процесса: `root`.
- Пользователь терминала: `root`.
- Рабочая директория: `/`.
- Планировщик: numbered FIFO lanes, SQLite-backed.
- Параллелизм: configurable workers, по одному процессу на lane.
- Хранилище: SQLite.
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

Endpoint: `/mcp`. Набор tools остаётся компактным: `agent_start`, `coordinate`, `message`, `agents`, `agent_finish`, `health`, `run`, `read`, `cancel`, `recovery`.

Agent Session по умолчанию имеет idle TTL 300 секунд, intent lease 180 секунд и жёсткий lifetime 1500 секунд. Последние 180 секунд каждый agent-bound ответ содержит warning с требованием дойти до безопасной точки, заранее запустить нужный долгий build/test и вернуться в чат с промежуточным отчётом. Terminal commands живут независимо от Agent Session.

`message` хранит состояния `delivered -> seen -> read -> replied`. Показ сообщения отмечает `seen`; `read` требует явного `message(agent_id, message_hash=...)`. Unacknowledged message продолжает показываться полным минимум три минуты и минимум пять ответов. `require_reply=true` удерживает `run` до связанного ответа. `alert=true` дополнительно блокирует normal work surface до reply, сохраняя `message`, `health`, `cancel`, `recovery` и `agent_finish`.

`run(agent_id, cmd, queue_id?)` использует numbered FIFO lanes. Первый run без номера выбирает least-loaded queue и запоминает affinity; следующие используют preferred queue. Явный `queue_id` выбирает lane и обновляет affinity. SQLite является источником queue state; workers используют atomic `queued -> running` claim и guarded terminal transitions.

`read` принимает `agent_id` и `cmd_hash` независимо. Agent-bound read показывает coordination/session context; scoped read возвращает queue metadata. Global stream имеет вид `HH:MM:SS <name> <hash> qN <output>`.

`agents()` работает без регистрации как observer. `target` показывает выбранную session вместе с `last_activity_tool`, message receipt journal (`delivered/seen/read/replied`) и coordination counters. `show_details`, `show_intents`, `show_commands`, `command_hash` и `since_minutes` добавляют план, intent journal, command journal и полную исходную команду без новых tools.

Подробный контракт: [`skills/terminal-operations/references/tool-contract.md`](skills/terminal-operations/references/tool-contract.md).

## OpenAPI Actions

Schema: `/openapi.json`. Actions используют тот же service layer и те же Agent Session semantics:

- `POST /actions/agent/start`
- `POST /actions/coordinate`
- `POST /actions/message`
- `POST /actions/agents`
- `POST /actions/agent/finish`
- `POST /actions/run`
- `POST /actions/recovery`
- `POST /actions/read`
- `POST /actions/cancel`
- `GET /actions/health`

Все published Actions содержат `x-openai-isConsequential: false`. MCP tools публикуют `destructiveHint=false` и `openWorldHint=false`; `agents`, `health` и `read` помечены read-only.

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

Scopes:

- `terminal:read`
- `terminal:execute`

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

SQLite содержит полные тексты команд, их вывод, OAuth clients, коды авторизации и refresh tokens. Это секретосодержащее хранилище.

- Каталог данных создаётся и восстанавливается с режимом `0700`.
- Файл SQLite создаётся и восстанавливается с режимом `0600`.
- Каталог резервных копий имеет режим `0700`.
- Каждый SQLite backup получает режим `0600`; установщик также нормализует права существующих backup-файлов.
- Storage layer повторно применяет режимы при запуске, поэтому обновление и ручная замена файла не оставляют базу доступной другим локальным пользователям.

## Установка

```bash
sudo TERMINAL_MCP_ADMIN_USERNAME="operator" \
  TERMINAL_MCP_ADMIN_PASSWORD="change-me" \
  TERMINAL_MCP_PUBLIC_BASE_URL="https://service.example" \
  ./deploy/install.sh install
```

Проверка:

```bash
sudo ./deploy/install.sh doctor
```

Обновление:

```bash
sudo ./deploy/install.sh update
```

Установщик создаёт версионированный virtualenv, env-файл, systemd unit и защищённое SQLite-хранилище. Перед обновлением создаётся SQLite backup. Health check подтверждает активацию release. Ошибка health check активирует предыдущий release. Пути установки, systemd command и health URL поддерживают переопределение переменными `TERMINAL_MCP_INSTALL_ROOT`, `TERMINAL_MCP_ENV_DIR`, `TERMINAL_MCP_DATA_DIR`, `TERMINAL_MCP_BACKUP_DIR`, `TERMINAL_MCP_UNIT_FILE`, `TERMINAL_MCP_SYSTEMCTL` и `TERMINAL_MCP_HEALTH_URL`.

## Пути

- Приложение: `/opt/terminal-mcp`.
- Конфигурация: `/etc/terminal-mcp/terminal-mcp.env`.
- Данные: `/var/lib/terminal-mcp/terminal-mcp.sqlite3`.
- Backup: `/var/backups/terminal-mcp`.

## Разработка

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[test]'
.venv/bin/ruff check src tests
.venv/bin/pytest -q
```
