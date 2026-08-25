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

Endpoint: `/mcp`.

- `run(cmd)` сохраняет команду, добавляет её в FIFO и возвращает только `ok`, восьмизначный `cmd_hash` и `error`. Операция ограничена 45 секундами; при неуспешном enqueue созданная запись удаляется.
- `read(cmd_hash?, lines_count=500, offset?)` возвращает до 1000 строк. Без `offset` возвращаются последние строки; отрицательный offset считается от конца. Только `read` возвращает статус команды.
- `cancel(cmd_hash)` физически удаляет queued-команду из очереди либо останавливает running-процесс. Через 45 секунд процесс принудительно завершается; окончательный статус проверяется через `read`.
- `recovery(cmd)` создаёт журналируемую команду с собственным `cmd_hash`, запускает её немедленно вне FIFO и ждёт завершения до 45 секунд. Ответ содержит последние 500 строк, а полный вывод доступен через `read`.
- `health()` возвращает состояние приложения, авторизации, очереди и terminal adapter. Если настроена `TERMINAL_MCP_HEALTH_COMMAND`, ответ также содержит её структурированный результат в `custom_command`. Health-команда выполняется отдельно от FIFO.

`read` возвращает `overall_lines_count` для конкретной команды и `null` для глобального журнала, а также `displayed_lines_count`. При scoped-чтении положительный offset — нулевой индекс строки, отрицательный — позиция от конца. Выход за границы мягко ограничивается существующим диапазоном. В глобальном журнале отрицательный offset также считается от конца, а неотрицательный offset является стабильным SQLite `seq`-курcором: следующий вызов с `next_offset` возвращает только более новые строки. Общее число строк глобального журнала намеренно не вычисляется.

Поле `error` содержит только ошибку самого плагина и этап в формате `<method>.<stage>: <reason>`. Ненулевой exit code shell-команды не считается ошибкой плагина: stderr остаётся в `lines`, `error=null`, а `exit_code` и статус доступны через `read`.

Каждый MCP tool публикует `outputSchema` и возвращает машинно-читаемый объект в `structuredContent`. Поле `content` содержит краткое текстовое резюме без повторной сериализации JSON.

Время появления новых строк сохраняется и отображается в UTC с суффиксом `Z`. Намеренно остановленная команда сохраняет статус `cancelled`, включая завершение процесса по `SIGTERM` или `SIGKILL`.

Статусы команды: `queued`, `running`, `completed`, `failed`, `cancelled`, `not_found`. Статус присутствует только в ответе `read`. Результаты `run` и `recovery` сохраняются в SQLite.

## OpenAPI Actions

Schema: `/openapi.json`.

- `POST /actions/run`
- `POST /actions/recovery`
- `POST /actions/read`
- `POST /actions/cancel`
- `GET /actions/health`

`POST /actions/read` принимает JSON-поля `cmd_hash`, `lines_count` и `offset`. Совместимый `GET /actions/read` доступен вне публикуемой OpenAPI-схемы.

Actions возвращают те же типизированные модели результатов напрямую как JSON. OpenAPI-схема описывает их через response schemas, совместимые с Custom GPT Actions.

Все опубликованные Actions содержат `x-openai-isConsequential: false`, поскольку сервис работает в выделенной управляемой среде с возможностью отката. MCP tools публикуют `destructiveHint=false` и `openWorldHint=false`; `health` и `read` дополнительно помечены как read-only.

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
