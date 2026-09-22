# PersistentCoder

PersistentCoder — локальный coding-agent с долговременным состоянием,
проектно-изолированной SQLite и sandbox-копией исходников. Агент изменяет
только sandbox; перенос verified-изменений в исходный проект выполняется
фреймворком после явного подтверждения пользователя.

## Запуск

```powershell
.\.venv\Scripts\python.exe -m app.main [PROJECT_ROOT]
```

Без аргумента используется текущая папка. VS Code frontend передаёт корень
открытого workspace автоматически. Обычная строка без `/` запускает задачу. После
успешной проверки доступны:

- `/patch` — показать immutable manifest с операциями `ADD`, `MODIFY`,
  `DELETE`, а также заблокированные пути и причины;
- `/apply` — после явного подтверждения транзакционно применить ровно этот
  verified-manifest;
- `/discard` — явно удалить неприменённый sandbox-результат и начать с чистого
  baseline;
- новый запрос — только после Apply или явного Discard dirty-сессии.

По умолчанию используется `Qwen/Qwen2.5-Coder-1.5B-Instruct`. Для машины с
достаточной памятью модель можно переопределить перед запуском, например:

```powershell
$env:PERSISTENTCODER_MODEL_NAME = "Qwen/Qwen2.5-Coder-3B-Instruct"
```

Для проектного QLoRA-адаптера планировщика:

```powershell
$env:PERSISTENTCODER_MODEL_NAME = "F:\PersistentCoder\models\manual\Qwen2.5-Coder-3B-Instruct"
$env:PERSISTENTCODER_MODEL_ADAPTER = "models\adapters\planner-coder-debugger-qwen2.5-3b-v6"
$env:PERSISTENTCODER_LOAD_IN_4BIT = "1"
```

Путь адаптера намеренно ограничен каталогом проекта. Воспроизводимый датасет
и QLoRA-обучение создаются командами:

```powershell
.\.venv\Scripts\python.exe -m tools.training.build_planner_dataset
.\.venv\Scripts\python.exe -m tools.training.train_planner_qlora
```

Кэш остаётся в `models/huggingface`; модель по-прежнему загружается лениво при
первом реальном запросе, поэтому `/status` и восстановление сессии не требуют
загрузки весов.

Модель не передаёт произвольные shell-команды. Coding Agent может выбрать только
типизированные инструменты `py_compile`, сфокусированный `pytest`,
`python_file` и разрешённый `python_module`; приложение проверяет пути и опции,
само строит argv и запускает его в Docker без shell. Старое непустое поле
`commands` отклоняется до записи файлов.

`AgentRuntime` по умолчанию делит один LLM между Planner, Coder и Debugger, чтобы
не переполнять GPU. Для экспериментов можно передать отдельные `planner_llm`,
`coder_llm` и `debugger_llm`. Debugger возвращает короткий проверяемый диагноз,
а не скрытую цепочку рассуждений; объём генерации выбирается по сложности сбоя.
Отчёт и результаты локального A/B-набора находятся в
`docs/evaluations/2026-09-22-real-model-miniboard.md`.

В VS Code тот же manifest показывается карточкой с подтверждаемыми кнопками
Apply/Discard. Пока операция выполняется, кнопка отправки превращается в `■`:
она останавливает backend, после чего новый процесс восстанавливает durable
состояние сессии перед следующей операцией.

Исходный проект не меняется во время работы агента. Ручной Apply и Direct mode
используют один `ApplyService`; при конфликте source/workspace, устаревшем
manifest или небезопасном пути применяется ноль файлов. Verified-результат
остаётся доступен для preview и Apply после перезапуска процесса.
Для проекта на другом диске транзакционный staging автоматически размещается в
отдельном framework-owned каталоге рядом с проектом, чтобы публикация файлов и
rollback оставались атомарными на одном томе.

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
- Framework-проверки (`py_compile`, import и pytest) передаются контейнеру как
  массив argv. Имена файлов с shell-метасимволами остаются одним аргументом и не
  интерпретируются shell.
- Вывод контейнера читается потоково. Runtime удерживает не больше заданного
  лимита, завершает команду при output flood и ограничивает число одновременно
  исполняемых команд. Timeout, cancellation, tmpfs, memory, CPU и PID limits
  возвращают проверяемый отказ без запуска на host.

## Доверенная верификация

Каждый `VerificationResult` отделён от перехода Task/Step и содержит неизменяемый
`VerificationContext`: SHA-256 текущего workspace snapshot, канонических
`verification_specs` и framework-owned окружения контейнера. Контекст сохраняется
в истории проверок. PASS считается актуальным только пока совпадают все три
fingerprint; изменение исходника, `pytest.ini`/`pyproject.toml`, спецификации или
образа делает evidence устаревшим.

Отсутствующий Docker/image, исчерпанная ёмкость, timeout и output limit дают
`BLOCKED`, а не ложный FAIL/PASS. Код pytest 5 (`no tests collected`) остаётся
FAIL. Dependency manifests читаются целиком как строгий UTF-8: oversized,
malformed, nested `-r`, unsafe URL/path declarations, `setup.py` и превышение
числа зависимостей блокируют environment без усечения или fallback в `NONE`.
Если локальная модель отсутствует или не загружается, CLI/backend завершается с
сообщением `local model is unavailable`, именем модели и подсказкой проверить
project-local cache; незавершённый run при следующем запуске восстанавливается
как явное dirty/failed состояние, а не как успешный результат.

## Проверка

```powershell
.\.venv\Scripts\python.exe -m compileall app tests
.\.venv\Scripts\python.exe -m pytest -q -m "not real_llm"
```

Короткий end-to-end тест пути `run → manifest → preview → Apply` без сети и
настоящей модели:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests\acceptance\test_verified_project_flow.py
```

Текущий план доведения продукта до рабочего пользовательского потока находится
в `docs/plans/2026-09-21-usable-end-to-end-agent.md`.
