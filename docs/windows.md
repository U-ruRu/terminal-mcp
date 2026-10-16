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

```powershell
.\deploy\windows\Manage-TerminalMCP.ps1 -Action Update -ExpectedCommit '<full-sha>'
.\deploy\windows\Manage-TerminalMCP.ps1 -Action Rollback
.\deploy\windows\Manage-TerminalMCP.ps1 -Action Status
.\deploy\windows\Manage-TerminalMCP.ps1 -Action Uninstall
```

Deployment lock сериализует операции. Update готовит релиз, останавливает службу,
сохраняет согласованные SQLite backup вместе с config и identity sidecars, активирует
релиз и проверяет HTTP health. Rollback переключает executable при совпадении текущих
схем со снимком. Durable data и identity сохраняют текущее состояние. Uninstall
снимает регистрацию службы; каталоги экземпляра сохраняются.

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
