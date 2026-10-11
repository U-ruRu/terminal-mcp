# Независимая приёмка final compact/chunk-кандидата — 778b2ee

Дата: 11 октября 2026 UTC. Ревизор November-4 — тот же исполнитель независимой
QA, ранее Lima-4. Исполнитель исправлений Juliett-4 — прежний Kilo-4.
Текущий relay dot: Oscar-4, роль подтверждена прямым native-сообщением.

## Точный SHA и вердикт

Проверен опубликованный `778b2ee51ae277bae4b2b8dc5f4ccf4196592c87`,
ветка `task/TMCP-COMPACT-CLOSE-kilo4-20261011`.
Родитель — `30eeb9d5d9f82d9a09ccaf825daac77088843332`;
chunk-база — `9456518991c31daf99b20a209e684a7ab3a201a6`;
предыдущий compact — `d96b682045744a9127140bf8b494afecd2a33184`.

**Целевая независимая приёмка: NON_BLOCKING.**
Два непересекающихся набора на одном точном SHA: **186 PASS / 0 FAIL / 2 SKIP**,
всего 188 случаев; оба pytest-процесса завершились exit 0.
**Общая приёмка: HOLD до согласованного итогового полного прогона.**

Remote canonical в этой работе оставлен `a1a2a45f0199a2e50fbc322f938068e00bf76823`.
Он не является предком отдельной compact-линии. Текущие Events/подписки/имена/intent
не объединялись; их интеграция и деплой являются отдельным согласованным этапом.

## Проверки и независимость

Чистый отдельный detached checkout:
`/workspace/terminal-mcp-worktrees/compact-close-accept-778b2ee-november4`.

Первый прогон: замороженные независимые фикстуры из QA
`7863260f30fb23e210d0c01c9c06c20eda810121` и согласованные инварианты:
**158 PASS / 2 SKIP**, exit 0, 55.34 секунды, native receipt `2e3c4c6c`.

Второй прогон: все шесть модулей, затронутых девятью локализованными падениями:
**28 PASS**, exit 0, 30.32 секунды, receipt `eb0a56bf`. Идентификаторы JUnit
сверены: пересечения с первым набором нет. Эти 28 включают все девять прежних
падений. Авторские прогоны в независимое число не входят.

Ruff, десять generated-artifacts, Events catalog, repository privacy и whitespace
проверены заново. Python-исходники `src/**/*.py` идентичны30eeb9d.
Изменились четыре файла тестов, два снимка контрактов и поясняющий документ.
Имена инструментов, маршруты и annotations сохранены. Digest входной схемы изменён
только для message обоих ролей: снимок догоняет существующие text_offset/text_limit.
Первоначальная дополнительная гипотеза ревизора «изменены только output-digests»
оказалась слишком узкой; сверка исходников и структуры установила точную причину.
Это не новая модификация request API в778b2ee.

## Матрица требований

| Область | Статус | Доказательство |
|---|---|---|
| H4-B1: дубли ошибок и старые алиасы | PASS | Root messages единственный; строгая схема отвергает старые поля |
| H4-B2: 501/16K Unicode и длинное имя слота | PASS | Исходные code/id/requirements сохранены; предупреждение и дочитывание доступны |
| L4-B3: output+incoming превышают общий JSON-budget | PASS | Сохранены cmd_hash и committed; ограниченный ответ28547байт |
| L4-B4: известный committed превращался в unknown | PASS | Outcome и ID сохранены в run/recovery |
| command_run двухэтапный, немедленный, фактические статусы | PASS | Нет inline output и ожидания; тест читает результат отдельным command_read |
| command_read/cancel/recovery, stderr/stdout/exit/cursor | PASS | Независимые сценарии и сохранённые integration assertions |
| Сообщения, preview, ACK/reply, Unicode и границы чтения | PASS | Байтовая реконструкция, неизменность хранилища и обязательств |
| task_claim/detail/snapshot/history, false/0/null, opaque JSON | PASS | Прежний независимый набор без ослабления ожиданий |
| История, связи, review gate, health | PASS | Целевые инварианты и проверки exact wire |
| Девять старых integration expectations/manifests | PASS | Причины воспроизведены отдельно; полные6модулей28PASS |
| Полные native naming/intent текущего canonical | NOTTESTED | Два intent SKIP; объединение запрещено в этой отдельной ветке |
| Окончательная совместимость подписок/деплой | NOTTESTED | Текущий Events rollout остаётся отдельным |
| Полный suite на778b2ee | NOTTESTED | Требуется явное согласование одного owner/SHA и свежего штатного окна |

## Что сохранено при обновлении тестов

Command receipt теперь читается из корня; ожидание завершения находится только
в тестовом клиенте через command_read. После минимального release сведения state
и revision проверяются отдельным task_get и сравнением с хранилищем; checkpoint
сравнивается как прежде. Health проверяет healthy вместо удалённого status.
Системное ACK и отказы ACK/alert проверяются по messages[].requirements и тому же
ID; коды отказов и запреты выполнения сохранены. Anonymous read подтверждается
вызовом Application API с agent_id=None вместо удалённого публичного ярлыка.
Снимки схем обновлены без снятия строгого сравнения реального контракта.

## Чтение больших сообщений

Сохранённые16 384 astral-символа (65 536 UTF-8 байт) восстановлены восемью частями.
Максимальный сериализованный CallToolResult18015байт при пределе64512байт.
Измерение включает structuredContent, его текстовую копию и envelope.
Границы code-point offsets, text_limit1…2048, пустой остаток и отказ past-end
проверены. ACK/reply чтением не заменяются. Обычная notify-отметка чтения остаётся
действующей семантикой. Новая слишком большая отправка отклоняется существующим
preflight до записи; историческое чтение проверено на отдельной тестовой SQLite.

## Полный прогон: достоверная граница

Первый full на30eeb9d (`a0f1aa25`, старт17:36:15UTC) завершён нативно
**cancelled, exit_code=-15, finished_at17:42:56.243UTC**. Финальные pytest-exit
и JUnit отсутствуют. Частичный лог содержал9F. Их причины независимо прочитаны
в целевых traceback:7обращений к удалённым полям и2снимка схем. После исправления
они проходят на778b2ee. Отменённый прогон не считается завершённой full-приёмкой.

Oscar получил NON_BLOCKING exact778b2ee и координирует единственный новый полный
прогон в свежем действующем окне. Ревизор полного прогона не запускает. Нельзя
перезаписывать предыдущий cancelled-result, удалять его atomic marker или выдавать
целевые186PASS за полный suite.

## Артефакты и продолжение

`/workspace/terminal-mcp-artifacts/compact-close-qa-lima4-20261011/`:
`candidate-778b2ee.{log,xml,exit}`,
`candidate-778b2ee-regression-modules.{log,xml,exit}`,
`candidate-778b2ee-provenance.txt`, `final-candidate-summary.json`,
`final-778b2ee-static.txt`, `final-778b2ee-snapshot-scope.log`,
`full-suite-native-verification.json`, `HANDOFF.md`.

Собственная новая QA-задача:
`terminal-mcp/TMCP-COMPACT-CLOSE-QA-NOVEMBER4-20261011`.
Старый claim Lima не перехватывался. Runtime/canonical/чужие worktrees неизменны.
Следующий безопасный шаг — получить согласование owner/SHA от Oscar/dot, затем
проверить артефакты ровно одного полного прогона исполнителя. До этого overall HOLD.

Публикация нейтральная, secret/privacy-check пройден. После отдельного принятого
release требуется обновить Executor/Coordination discovery и клиентские обращения:
корневые поля run/messages, command_read для вывода, task_get после claim,
checkpoint-объект, две коллекции связей, extended health и chunk-параметры сообщений.
