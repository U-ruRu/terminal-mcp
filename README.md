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

Endpoint: `/mcp`. Каждый рабочий проход начинается с `agent_start`.

- `agent_start(task_summary, intent, work_scope)` создаёт внутренний NATO call sign вида `Foxtrot-7K2M`, открывает 180-секундный task lease и единственный раз возвращает полный `agent_id`.
- Во всех последующих выводах показывается только публичное имя без suffix: `Foxtrot`, `India`, `Juliett`.
- `agent_task(agent_id, intent)` принимает только короткий `intent` до 160 символов. `work_scope`, объявленный при `agent_start`, сохраняется; `detail` отсутствует.
- `run(agent_id, cmd)` требует активную Agent Session и свежий task lease. `agent_start` и `agent_task` обновляют task lease.
- `health()` не требует Agent Session.
- `recovery(cmd, agent_id?)` и `cancel(cmd_hash, agent_id?)` доступны без Agent Session; отсутствие `agent_id` обозначается как `anonymous`.
- `read(agent_id?, cmd_hash?, ...)` работает либо с парой `agent_id + cmd_hash`, либо без обоих параметров как global terminal stream.

Agent Session TTL — 300 секунд и продлевается любым действием с живым `agent_id`. Отдельного active-awareness TTL нет: активный агент виден те же 300 секунд. Task lease для `run` — 180 секунд и обновляется только `agent_start`/`agent_task`. `finished` остаётся видимым 180 секунд.

`run` и `read` возвращают `active_agents` как компактный массив строк, а не массив объектов:

```text
23:32:38 November 10de68b3 — Проверить regression suite
23:31:02 India started — Обновить storage
23:34:15 Juliett finished — Проверить HTTP contract
```

Формат строки: `HH:MM:SS <public-name> <cmd_hash|started|finished> — <intent>`. Для активного агента используется hash последней команды; timestamp берётся из её завершения, запуска или регистрации. Полная команда в awareness не выводится.

Terminal output также компактный:
- scoped `read`: `HH:MM:SS <output>`;
- global `read`: `HH:MM:SS <public-name|anonymous> <cmd_hash> <output>`.

`task_summary` в `agent_start` ограничен 120 символами, `intent` — 160, `work_scope` — четырьмя элементами по 80 символов. Scope остаётся cooperative metadata для обнаружения конфликтов. Agent Session, task history, activity audit и command attribution сохраняются в SQLite.

Рекомендуемый workflow: `agent_start` → `run/read` → при смене ближайшей задачи или истечении task lease `agent_task` → `run/read` → `agent_finish`.

## OpenAPI Actions

Schema: `/openapi.json`. Actions используют тот же service layer и те же Agent Session semantics:

- `POST /actions/agent/start`
- `POST /actions/agent/task`
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
