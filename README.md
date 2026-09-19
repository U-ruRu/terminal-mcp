# terminal-mcp

`terminal-mcp` предоставляет MCP и OpenAPI-интерфейсы для управления Linux-терминалом.

Готовый skill для установки: [`dist/terminal-operations.skill`](dist/terminal-operations.skill).

## Runtime

- Пользователь процесса: `root`.
- Пользователь терминала: `root`.
- Рабочая директория: `/`.
- Планировщик: FIFO.
- Параллелизм: одна команда.
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

Endpoint: `/mcp`. Каждый новый рабочий агент начинает с `agent_start`.

- `agent_start(task_summary, intent, details, work_scope?, agent_id?)`: новая регистрация требует summary, короткий intent и непустой `details`-план. `work_scope` — optional metadata.
- Новая регистрация создаёт внутренний NATO call sign вида `Foxtrot-7K2M`, открывает 180-секундный task lease и единственный раз возвращает полный `agent_id`.
- Повторный `agent_start(agent_id=...)` обновляет существующий план/описание/optional scope без новой identity и без повторной публикации suffix.
- Во всех остальных публичных выводах используется только короткое имя.
- `coordinate(agent_id, step?, intent?, show_details=false)` читает или обновляет текущий шаг. `step + intent` обновляет 180-секундный task lease; `show_details=true` показывает планы peers.
- `message(agent_id, text?, target?, message_hash?)` отправляет или подтверждает coordination message. Target — только короткое публичное имя; без target используется broadcast по snapshot текущих active peers.
- Unread coordination message блокирует только новую обычную `run`. `read`, `message`, `coordinate`, `health`, `cancel` и `recovery` остаются доступны.
- `health(agent_id?)` работает и без Agent Session; с живым ID продлевает Session TTL и показывает pending messages.
- `read` работает либо с парой `agent_id + cmd_hash`, либо без обоих параметров как global stream.

Agent Session TTL — 300 секунд и продлевается любым действием с живым `agent_id`. Task lease для `run` — 180 секунд и обновляется новой регистрацией, update плана через `agent_start(agent_id=...)` и `coordinate(step + intent)`. `finished` остаётся видимым 180 секунд.

`active_agents` — compact `string[]`:

```text
23:32:38 November 10de68b3 — Проверить regression suite
23:31:02 India started — Обновить storage
23:34:15 Juliett finished — Проверить HTTP contract
```

Pending messages тоже compact:

```text
23:35:10 a1b2c3d4 India → you: Storage правлю я, возьми MCP contract | ack: message(a1b2c3d4)
23:35:12 e5f6a7b8 Juliett → all: Не трогайте migration до проверки | ack: message(e5f6a7b8)
```

Terminal output:
- scoped `read`: `HH:MM:SS <output>`;
- global `read`: `HH:MM:SS <public-name|anonymous> <cmd_hash> <output>`.

Лимиты: `task_summary` — 120 символов, `intent` — 160, `details` — до 12 шагов по 160 символов, optional `work_scope` — до четырёх элементов по 80 символов.

Workflow: `agent_start` → `coordinate(step, intent)` → `run/read`; при pending message сначала `message(message_hash)` для ack и при необходимости новый `coordinate`; затем продолжение работы → `agent_finish`.

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
