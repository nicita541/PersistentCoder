# PersistentCoder

PersistentCoder — локальный coding-agent с долговременным состоянием,
проектно-изолированной SQLite и sandbox-копией исходников. Агент изменяет
только sandbox; перенос verified-изменений в исходный проект выполняется
фреймворком после явного подтверждения пользователя.

## Запуск

```powershell
.\.venv\Scripts\python.exe -m app.main
```

Runtime канонизирует корень открытого проекта и вычисляет `project_id` как
SHA-256 от нормализованного абсолютного пути. Модель не может выбирать или
подменять эту идентичность.

## Проектная изоляция

Для каждого `project_id` PersistentCoder использует отдельные пространства:

- `data/projects/<project_id>/persistent_coder.db` — планы, задачи, шаги,
  попытки, проверки, прогоны, события, сессии и память проекта;
- `.sandbox/projects/<project_id>/sessions` — рабочие sandbox-копии;
- `.sandbox/projects/<project_id>/snapshots` — baseline и checkpoints;
- `.sandbox/projects/<project_id>/patches` — сформированные patch-файлы;
- `.sandbox/projects/<project_id>/logs` и `tmp` — служебные файлы проекта;
- `data/global/persistent_coder.db` — только явно сохранённые правила типа
  `USER_RULE`, общие для проектов.

`FACT`, `DECISION` и `EXPERIENCE` всегда принадлежат конкретному проекту.
Все запросы к task/runtime stores фильтруются одновременно по `project_id` и
каноническому корню. Поэтому даже общая SQLite не позволяет проекту B увидеть
или восстановить данные проекта A.

Старые строки без project binding сохраняются в базе, но новый project-bound
runtime их не принимает, не показывает и не восстанавливает. Legacy API,
открытый без `StoreContext`, видит только такие unscoped строки — это позволяет
явно мигрировать данные без их неявного присвоения случайному проекту.

## Жизненный цикл сессии

Durable `AgentSession` проходит только разрешённые переходы:

```text
CLEAN -> RUNNING -> CLEAN
                 -> DIRTY_VERIFIED -> APPLIED -> CLEAN
                                   -> DISCARDED -> CLEAN
                 -> DIRTY_FAILED   -> DISCARDED -> CLEAN
```

- `CLEAN` — sandbox совпадает с baseline и готов к новому прогону.
- `RUNNING` — сессия занята одним активным прогоном.
- `DIRTY_VERIFIED` — verified-изменения сохранены для просмотра/Apply.
- `DIRTY_FAILED` — неуспешные изменения сохранены для диагностики или Discard.
- `APPLIED` — изменения перенесены в source; затем runtime обновляет baseline.
- `DISCARDED` — изменения явно отклонены; затем sandbox пересоздаётся чистым.

Новый прогон не стартует поверх dirty-сессии. `new_session()` требует явного
`discard_dirty=True`, если неприменённые изменения ещё существуют. Успешный
Apply использует текущий source как новый baseline; Discard также пересоздаёт
baseline и workspace транзакционно.

## Границы файлов и команд

- Любой путь модели проходит через единый `ProjectPath`: абсолютные пути,
  `..`, выход через link/reparse point и неоднозначные варианты отклоняются.
- Snapshot заранее исключает защищённые, секретные и gitignored пути, проверяет
  лимиты количества/размера и материализуется только после проверки всех хешей.
- Planner назначает каждому Task точные `change_paths`; один файл принадлежит
  только одному Task, а Step может лишь сузить область. Записи и удаления вне
  точного списка запрещены, включая соседний файл в той же директории.
- Существующий файл перед изменением или удалением нужно прочитать в той же
  попытке. Знание из прежней попытки не даёт права на изменение.
- Удаление разрешено только для одного существующего обычного файла; директории,
  ссылки и reparse points не удаляются. Пакет файловых операций откатывается при
  ошибке любой операции.
- Команда модели получает read-only `/input` и отдельный tmpfs `/workspace` в
  Docker без сети, host socket и host environment. Записи команды исчезают после
  контейнера; persistent-изменения выполняет только `FileTools`.

## Проверка

```powershell
.\.venv\Scripts\python.exe -m compileall app tests
.\.venv\Scripts\python.exe -m pytest -q -m "not real_llm"
```

Полный MVP развивается по последовательным планам в
`docs/superpowers/plans/`; проектная идентичность и lifecycle сессии являются
первым завершённым фундаментальным этапом. Границы snapshot/path/command и
точные области изменений составляют второй этап; следующие планы закрывают
planner/verification/repair, apply backend и VSIX release.
