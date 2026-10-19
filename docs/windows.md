# Windows node

## Runtime

Поддерживаемая среда: Windows 10/11 x64, Python 3.11+, NTFS, Windows PowerShell 5.1
либо PowerShell 7. `Settings` выбирает платформенный `ExecutionPort` при `in_process`.
Linux topology описана в [ExecutionPort](architecture/execution-port.md).

PowerShell получает `-NoProfile -NonInteractive`. Доверенный bootstrap читает UTF-8
из stdin после назначения Job Object и durable admission. Начальное значение
`ErrorActionPreference` — `Stop`. Коды native-программ и явный `exit` сохраняются.
Каждая команда получает отдельный процесс и Job Object с `KILL_ON_JOB_CLOSE`.
Отмена завершает дерево процессов. Проверка чужого PID использует Win32 observation.
В health `uid` и `gid` равны `-1`; `user` содержит Windows account.

Ресурсы Console используют Win32 `GetSystemTimes`, `GlobalMemoryStatusEx`,
`GetTickCount64` и объём файловой системы рабочего диска. CPU usage вычисляется
по разнице последовательных выборок.

SCM запускает WinSW от LocalSystem в Session 0. Режим запуска — Automatic Delayed
Start; восстановление — restart через 10 секунд. Session 0 обслуживает команды и
фоновые программы. Интерактивные окна относятся к пользовательской desktop session.

## Installation

Источник — чистый Git checkout с полным SHA. Дистрибутив WinSW x64 берётся из
[официального релиза](https://github.com/winsw/winsw/releases/tag/v2.12.0);
`WinSWSha256` фиксирует проверенный SHA-256 файла. Команды выполняются в elevated PowerShell.

```powershell
.\deploy\windows\Manage-TerminalMCP.ps1 -Action Install `
  -Python 'C:\Python313\python.exe' -ExpectedCommit '<full-sha>' `
  -WinSWPath 'C:\Downloads\WinSW-x64.exe' -WinSWSha256 '<sha256>' `
  -PublicUrl 'https://terminal-win.example.org' -ListenHost '10.77.17.2'
```

Установщик архивирует указанный commit, применяет существующий release-version
builder, создаёт venv и выполняет изолированный app/SQLite/PowerShell probe.
Корень экземпляра — `%ProgramData%\TerminalMCP`; `-Root` задаёт отдельный каталог.
`config` содержит env и OAuth credentials; `data` — durable SQLite, Access key и
identity anchor; `releases` — версии; `backups` — снимки; `work` — cwd; `logs` — журналы.
ACL установки предоставляет FullControl SYSTEM и Administrators. Новые файлы наследуют
эту ACL. Runtime проверяет ACL открытых credential-файлов. OAuth credentials
создаются однократно в `config\credentials` и сохраняются при обновлении.

Установщик объединяет настроенные CORS origins с `https://localhost` для Android
WebView и адресом `console_public_base_url`. Исходный env сохраняется в
`backups\console-origins-<sha256>.env`; повторный запуск использует полученный список.

```powershell
.\deploy\windows\Manage-TerminalMCP.ps1 -Action Update -ExpectedCommit '<full-sha>'
.\deploy\windows\Manage-TerminalMCP.ps1 -Action Rollback
.\deploy\windows\Manage-TerminalMCP.ps1 -Action Status
.\deploy\windows\Manage-TerminalMCP.ps1 -Action Uninstall
```

Deployment lock сериализует операции. Update готовит релиз, останавливает службу,
сохраняет согласованные SQLite backup всех настроенных хранилищ вместе с config,
VPN-конфигурацией и identity sidecars. Затем выполняется offline migration с проверкой
сохранности существующих строк и защищённых файлов. Activation подтверждается
`/health/ready`, `ready=true` и ожидаемым release. Rollback переключает executable при совпадении текущих
схем со снимком. Durable data и identity сохраняют текущее состояние. Uninstall
снимает регистрацию службы; каталоги экземпляра сохраняются.

Начало миграции фиксируется в `migration.started.json`, завершение — в
`migration.completed.json` внутри backup. Completion receipt связывает проверку
с snapshot, путями хранилищ и версиями схем. `service\release.json` хранит pending
release. `-Action Resume` проверяет завершённую миграцию и продолжает activation.
Ошибка после начала миграции требует forward recovery с сохранением текущих данных.

Offline update включает одноразовую подготовку Access v25 для штатного перехода
v26 → v27. Поддерживаемый каталог v25 содержит состояния active и suspended.
Перенос идентичности в session storage проверяется по исходному журналу событий;
исходные коды и сроки входят в проверку сохранности.

## Console management

Pairing использует штатную одноразовую ссылку `terminal-mcp link --name PC`.
Console показывает узел в группе Standalone и управляет его локальными слотами и
политикой через общий API. Параметры членства описаны в
[Mesh contract](mesh-product-contract.md).

Установщик регистрирует SYSTEM-задачу `TerminalMCP-Maintenance`. Она работает при
питании от батареи и выполняет перезапуск отдельно от процесса API. ACL задачи и
операционных файлов предоставляет доступ SYSTEM и Administrators.

| API | Результат |
| --- | --- |
| `GET /actions/fleet/control/runtime` | Возможности, состояние служб, последняя операция |
| `POST /actions/fleet/control/runtime/restart` | HTTP 202; `target=terminal\|tunnel`, `request_id` |
| `GET /actions/fleet/control/runtime/operations/{request_id}` | Сохранённый результат операции |

Каждый вызов требует действующего pairing; перезапуск дополнительно требует
`terminal:execute`. `request_id` связывает повторное чтение с принятой операцией.
Результат проходит состояния pending, running, succeeded, failed либо unknown.
Console сохраняет ожидающий идентификатор на устройстве и восстанавливает наблюдение
после разрыва связи. Deployment lock разделяется с установщиком.

`terminal` соответствует службе TerminalMCP. `tunnel` соответствует штатной службе
WireGuard, имя которой задаётся при установке: `-TunnelName tmcp-laptop`.
Привязка хранится в `service\runtime.json`. Панель Console предлагает операции,
объявленные узлом. Выполняемые команды могут завершиться при перезапуске TerminalMCP.
Mesh transport API объявляет `provisioning_supported=false` для Windows; Console
направляет управление установленным туннелем в панель служб.

## Explicit clean installation

`-Action CleanInstall` accepts the installation parameters above and prepares a
fresh release directory, including when the requested commit is already installed.
After validating the new release and the state inventory, it stops and unregisters
the existing service, removes managed `config`, `data`, `cache`, `logs`, `work`,
backup and deployment-state directories, and initializes new local credentials and
pairing. Configured external database paths and identity sidecars are included.
The previous release is not an automatic rollback target. Ordinary `Install`
rejects retained managed state; `Update` preserves it.

## Network

Отдельный узел использует собственные Access, Executor и Coordinator с локальным
issuer и пустым списком Mesh peers. Конфигурация остальных issuer сохраняется.
Штатный WireGuard tunnel service поддерживает исходящее подключение к шлюзу.
Peer ноутбука содержит `PersistentKeepalive = 25`, `Endpoint` шлюза и его VPN `/32`.
Peer шлюза содержит VPN `/32` ноутбука. Приватные ключи хранятся на своих узлах.

```powershell
.\deploy\windows\Install-WireGuardTunnel.ps1 `
  -ConfigPath 'C:\ProgramData\TerminalMCP\network\tmcp-laptop.conf' `
  -GatewayAddress '10.77.17.1' -LocalAddress '10.77.17.2' `
  -MsiPath 'C:\Downloads\wireguard-amd64.msi'
```

MSI проходит Authenticode-проверку издателя WireGuard. Firewall rule разрешает API
для адреса шлюза через VPN. Caddy на шлюзе обслуживает отдельный HTTPS origin и
проксирует его на VPN-адрес ноутбука. `PublicUrl` соответствует этому origin.
Пути трёх MCP-ролей перечислены в [README](../README.md#public-mcp).

## Verification

Платформенный план и отчёт запускаются существующим [test workflow](testing.md).
`qa/windows` проверяет PowerShell, Job Objects, ACL, app/auth/SQLite и перезапуск.
Linux regression использует существующие component/integration/platform selectors.
Живая приёмка фиксирует commit, службы, OAuth PKCE, выполнение команды, сохранение
истории после restart и восстановление туннеля. Загрузка до пользовательского входа
проверяется отдельным reboot; Windows 10 и Windows 11 получают собственные прогоны.

Основания: [Microsoft Job Objects](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects),
[Python Windows process signals](https://docs.python.org/3/library/os.html#os.kill),
[WireGuard tunnel service](https://git.zx2c4.com/wireguard-windows/about/docs/enterprise.md),
[WinSW configuration](https://github.com/winsw/winsw/blob/v2.12.0/doc/xmlConfigFile.md).
