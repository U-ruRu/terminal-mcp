# Android Access slots: единый кандидат и процедура проверки

## Исходники и изоляция

База — принятый remote `integration/M3-functional-candidate`,
`26ae8395bc6909de9313291272ff153483e8818e` (серверный релиз 0.25.1).
Восстановленная интеграция Victor-4 `624234db16bf817fb5b4498026337cf1fb3eb4a2`
включает Papa/Quebec/Tango и продолжение API, наследования и публичного number bridge.
Последняя UI-проверка/защита Whiskey-4 `7c83d610445c6dadba01de188e068497c5a85b5d`
добавлена отдельно. Полученное дерево до завершающих тестовых материалов точно
совпадает с `b33b372d5c171fbd05e9564e5e936d7ee18adf3c` Whiskey-4.

Zulu-4 сохраняет исходные HEAD, staged/unstaged diff и untracked source каждого
предшественника в `/workspace/terminal-mcp-artifacts/android-slots-zulu4-20261012/initial-snapshots`.
Оригинальные рабочие копии, canonical, runtime, production-доступ и политики не меняются.
Материалы независимой проверки Tango находятся в `android-slots-tango4-20261012`;
проверка Whiskey публичного пути A — в `android-slots-whiskey4-20261012/independent-A-final-public-tests.log`.
Свежие результаты и точный финальный SHA находятся в manifest/checkpoint/handoff
каталога Zulu; эта страница задаёт контракт и воспроизводимую процедуру.

## Матрица действующего контракта

Все native-операции проходят существующий operator API. Чтение требует
`terminal:read`; изменение — `terminal:execute` и operator admission.
Свежесть, контекст pairing и возможности сервера проверяются дополнительно в клиенте.

| Действие | Endpoint и запрос | Изменение / подтверждение |
|---|---|---|
| Список и Slot Details | GET `/actions/access/slots`, GET `/actions/access/slots/{slot_id}` | Полная пагинация и строгие модели; ошибка и неполный ответ не считаются пустым списком. |
| Создание | POST `/actions/access/mutate`, `action=create`, `mode`, стабильный `idempotency_key` | Один receipt; повтор неопределённого запроса использует прежний ключ, затем выполняется новое чтение. |
| Ротация кода | Тот же endpoint, `action=rotate`, `slot_id`, `expected_revision`, ключ | Меняется code generation; стабильная идентичность/текущее окно сохраняются. Чтение списка не раскрывает код. |
| Standalone/server defaults | GET `/actions/access/defaults`; mutation `action=defaults`, `expected_revision`, policy либо `inherit_policy=true` | Локальные defaults и наследуемые будущие политики; прочитать подтверждённые defaults и слоты. |
| Override слота | mutation `action=policy`, `slot_id`, `expected_revision`, policy | Только выбранный слот, следующий рабочий цикл; текущее окно и снимок политики сохраняются. |
| Сброс override | mutation `action=policy`, `inherit_policy=true`, `expected_revision`, `expected_parent_revision` | Серверное наследование; проверяются обе ревизии. Значения родителя не копируются клиентом. |
| Mesh policy | GET `/actions/fleet/control`; POST `/actions/fleet/control/policy`, `expected_revision` | Запись на существующем control node и штатная Fleet-репликация; перечитать authority и native defaults участников. |
| Явное продление окна | mutation `action=deadline`, `slot_id`, `expected_revision`, `deadline_at` | Отдельная подтверждаемая операция; сохраняет начало окна и будущую политику. Уменьшение shared max-deadline отклоняется. |

Политика слота использует `duration_seconds`, `cooldown_seconds`, `rearm_enabled`,
`warning_seconds`, `draining_seconds`, `release_on_end`. Длительность — положительное
целое число секунд; cooldown неотрицателен, warning/draining неотрицательны и меньше
длительности. Три часа передаются как 10800 секунд. Значения для будущего окна и
снимок действующего окна показываются раздельно. Неопределённый срок кода нельзя
подменять deadline рабочего окна; expiry отображается только из соответствующего
серверного поля receipt.

## Семантика совместимости и безопасности записи

Полное управление требует `management_contract_version=2` и поддерживаемых
inheritance/next-window capabilities. Исходный сервер 0.25.1 без серверной части
этого кандидата остаётся доступен для поддерживаемого чтения; опасные изменения
отключаются с объяснением о необходимости обновления. Номер версии сам по себе
не доказывает наличие безопасного контракта.

Native v2 показывает shared-number окно, реально используемое публичным MCP.
Для persistent provisioning локальный `SlotSnapshot.anchor` может быть пустым,
хотя выдача номера уже открыла shared окно. Это отдельные сущности: слот,
доступность рабочего окна и материализация локальной WorkSession при attach.
Старая проверка `idle` только по локальному anchor заменена более строгой проверкой
совпадения API с public shared window, его началом и deadline. Серверная семантика
выдачи ради прохождения этого теста не менялась.

Pairing-state включает соединение, устройство, scope и `pairedAt`. Новый контекст
не наследует права записи из успешного чтения старого. Ошибка обновления не меняет
время последнего успешного наблюдения. Истечение TTL, неизвестный control node,
недоступный authority и конфликт ревизий закрывают запись; конфликт вызывает
новое чтение, без перезаписи чужого изменения. Endpoint, категория и длительность
ошибки показываются без копирования приватного текста ответа.

## Воспроизведение двух исходных дефектов

Изолированный RED checkout: `07476b7f27c300b5ae1f20a039f7af0aad2623a1`.
HTTP-тест `test_http_next_policy_does_not_change_active_window_deadline` показывает
изменение deadline с 2030-01-01 00:23 на 03:00 при записи будущей политики.
В исправленном кандидате тот же тест сохраняет 00:23 и записывает 10800 секунд
для следующего окна.

Для re-pair в RED checkout перенесён только тест из `0d035ba`.
Два случая `pairedAt`/`scope` в `SlotAuthorityTransition.qa.test.tsx` воспроизводят
оставшуюся доступной кнопку Pause. В кандидате новые чтения обязательны, а старые
ответы не возвращают прежние строки и права. Полный файл дополнительно проверяет
device/authority смену, reconnect, double click, восстановление неопределённой
операции и отдельный expiry кода.

## Команды приёмки

Backend-запуск изолирует все `TERMINAL_MCP_*` пути дочернего процесса, включая
Unix socket, runtime/env, базы, логи и Fleet state. Короткий `/tmp`-путь нужен
из-за ограничения длины Unix socket. Production environment/state не используются.
В каталоге артефактов сохранены `run_backend_regressions.py`, RED/GREEN runners,
manifest с точным SHA/командами/временем и полные логи каждого запуска.

`scripts/capture_android_slot_fixtures.py` заново снимает семь GET-проекций с
изолированных HTTP-приложений, включая reset с проверкой parent revision,
проверяет management v2 и запрещённые поля. Коды и mutation receipts не сериализуются.
После регенерации проверяются `slotContracts`, `slotRuntimeParity.qa` и
`nativeInheritanceParity`, затем полный frontend test, TypeScript/Vite build и ESLint.

Android собирается только штатным `npm run android:build:release` с существующей
подписью. Проверка использует `verify-android-release.sh`, apksigner, aapt, ZIP
integrity и сравнение сертификата с опубликованной 0.2.36/code38. Ключи не извлекаются.
Планируемая версия 0.2.37/code39 должна повторно сверяться с опубликованными сборками.

Tokyo runtime и настройки здесь не затрагиваются. Изолированные сценарии разделяют
server availability, freshness ресурсов, Mesh authority и готовность конфигурации.
Причину наблюдаемого на телефоне Tokyo без согласованной authenticated live-проверки
не следует объявлять установленной; предположение о ping доказательством не является.
